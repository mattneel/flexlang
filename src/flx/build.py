"""`build.flx` — a typed, effect-checked build graph, not a shell script.

Build logic is ordinary Flex: `target` declarations whose signatures declare
their effects, checked by the same checker as everything else,

    module Build

    target default = test

    target check uses { Process } {
      sh("uv run ruff check src tests")?
    }

    target test uses { Fs, Process } {
      check()?
      sh("uv run pytest")?
    }

A target that shells out without declaring `Process` fails type-checking
(EFFECT001); calling another target demands that target's effects, so effects
propagate up the build graph; `?` propagates a failed step out of the target.
`flx build --explain` reports each target's declared effects.

Targets execute on the tree-walking interpreter (the same engine that runs
`flx run`/`test` and evaluates `package.flx` manifests), extended with two
intrinsics: `sh(cmd)` (requires Process) and `flx.check/test/run/expand/build`
on a file glob (require Fs). Each target runs at most once per invocation, in
dependency order. The surface language is real, so a future self-hosted build
runner changes nothing for users.
"""

from __future__ import annotations

import glob as globmod
import subprocess
import sys
from pathlib import Path

from flx import driver, interp
from flx.diagnostics import FlexError
from flx.macro import expand
from flx.modules import load_program
from flx.sema.check import CheckResult
from flx.sema.specialize import check_and_monomorphize
from flx.syntax import ast

BUILD_FILE = "build.flx"

_OK = interp.Variant("Ok", None)


def _err(message: str) -> interp.Variant:
    return interp.Variant("Err", message)


class BuildInterpreter(interp.Interpreter):
    """The runtime interpreter plus the build intrinsics and target memoization."""

    def __init__(self, checked: CheckResult) -> None:
        super().__init__(checked)
        self.targets = {t.name: t for t in checked.module.targets}
        self.memo: dict[str, interp.Variant] = {}

    def run_target(self, name: str) -> interp.Variant:
        cached = self.memo.get(name)
        if cached is not None:
            return cached
        print(f"-> target {name}")
        target = self.targets[name]
        try:
            self.exec_block(target.body, interp._Env())
            result = _OK
        except interp._Return as ret:
            # `?` propagated a failed step (an Err) out of the target body.
            value = ret.value
            result = value if isinstance(value, interp.Variant) else _OK
        except interp._TestFail as fail:
            result = _err(fail.reason.strip() or "target failed")
        self.memo[name] = result
        return result

    def _call(self, expr: ast.CallExpr, env: interp._Env) -> object:
        callee = expr.callee
        if isinstance(callee, ast.NameExpr):
            if callee.name in self.targets:
                return self.run_target(callee.name)
            if callee.name == "sh":
                command = self.eval(expr.args[0], env)
                return self._sh(str(command))
        if (
            isinstance(callee, ast.MemberExpr)
            and isinstance(callee.obj, ast.NameExpr)
            and callee.obj.name == "flx"
            and not env.has("flx")
        ):
            pattern = self.eval(expr.args[0], env)
            return self._flx_op(callee.name, str(pattern))
        return super()._call(expr, env)

    def _sh(self, command: str) -> interp.Variant:
        print(f"$ {command}")
        sys.stdout.flush()
        code = subprocess.run(command, shell=True).returncode
        return _OK if code == 0 else _err(f"command exited with code {code}")

    def _flx_op(self, op: str, pattern: str) -> interp.Variant:
        files = sorted(globmod.glob(pattern, recursive=True))
        if not files:
            return _err(f"no files match {pattern!r}")
        commands = {
            "check": driver.cmd_check,
            "test": driver.cmd_test,
            "run": driver.cmd_run,
            "expand": driver.cmd_expand,
            "build": driver.cmd_build,
        }
        run = commands[op]
        for file in files:
            print(f"flx {op} {file}")
            sys.stdout.flush()
            code = run(file)
            if code != 0:
                return _err(f"flx {op} {file} exited with code {code}")
        return _OK


def run_build(target_name: str | None = None, explain: bool = False) -> int:
    """Run a target from ./build.flx (the default target if none is named)."""
    build_file = Path(BUILD_FILE)
    if not build_file.is_file():
        print("flx build: no build.flx in the current directory", file=sys.stderr)
        return 1

    from flx import package as pkg

    roots: tuple[Path, ...] = ()
    manifest_file = pkg.find_package()
    try:
        if manifest_file is not None:
            roots = pkg.dependency_roots(pkg.load_manifest(manifest_file))
        loaded = load_program(str(build_file), roots)
        module = expand(loaded.module)
        result = check_and_monomorphize(
            module, loaded.decl_module, loaded.public, loaded.file_module
        )
    except FlexError as err:
        driver._report(err, {})
        return 1

    targets = result.module.targets
    if not targets:
        print("flx build: build.flx declares no targets", file=sys.stderr)
        return 1
    default = result.module.default_target

    if explain:
        for target in targets:
            marker = "  (default)" if target.name == default else ""
            uses = ", ".join(sorted(target.effects)) if target.effects else "-"
            print(f"target {target.name}{marker}")
            print(f"  uses: {uses}")
        return 0

    name = target_name or default
    if name is None:
        names = ", ".join(t.name for t in targets)
        print(f"flx build: no target given and no default set (targets: {names})", file=sys.stderr)
        return 2
    if name not in {t.name for t in targets}:
        names = ", ".join(t.name for t in targets)
        print(f"flx build: no target {name!r} (targets: {names})", file=sys.stderr)
        return 2

    runner = BuildInterpreter(result)
    try:
        outcome = runner.run_target(name)
    except interp.FlexRuntimeError as exc:
        sys.stdout.flush()
        print(f"flx build: runtime error: {exc}", file=sys.stderr)
        return 1
    if outcome.tag == "Ok":
        print(f"build ok: {name}")
        return 0
    print(f"build failed: {name}: {outcome.payload}", file=sys.stderr)
    return 1
