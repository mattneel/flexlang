"""Command implementations behind the `flx` CLI.

Each `cmd_*` returns a process exit code and renders diagnostics to stderr.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from flx import interp
from flx import package as pkg
from flx.backend import toolchain
from flx.backend.harness import generate_harness
from flx.backend.mlir import BackendError, emit_module, emit_program
from flx.backend.runtime import BASE_RUNTIME_C
from flx.backend.toolchain import build_executable, run_executable
from flx.diagnostics import Diagnostic, FlexError
from flx.macro import expand
from flx.modules import ProgramInfo, load_program
from flx.sema.check import CheckResult
from flx.sema.specialize import check_and_monomorphize
from flx.syntax.dump import dump_module
from flx.syntax.parser import parse
from flx.types import I64, UNIT, Type


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"flx: {path}: {exc}", file=sys.stderr)
        return None


def _report(err: FlexError, sources: dict[str, str]) -> None:
    for diag in err.diagnostics:
        src = ""
        if diag.span is not None:
            looked_up = sources.get(diag.span.file)
            if looked_up is None:
                try:
                    looked_up = Path(diag.span.file).read_text(encoding="utf-8")
                except OSError:
                    looked_up = ""
            src = looked_up
        print(diag.render(src), file=sys.stderr)
        print(file=sys.stderr)


def _resolve_entry(path: str | None) -> tuple[str, tuple[Path, ...]] | None:
    """The (entry file, extra import roots) for a command.

    An explicit path is used as-is, gaining the dependency roots of any
    `package.flx` beside it. With no path, the manifest in the current directory
    supplies both the entry and the roots."""
    if path is not None:
        manifest_file = pkg.find_package(Path(path).resolve().parent)
        if manifest_file is not None and Path(path).name != pkg.MANIFEST_FILE:
            try:
                manifest = pkg.load_manifest(manifest_file)
                return path, pkg.dependency_roots(manifest)
            except FlexError as err:
                _report(err, {})
                return None
        return path, ()
    manifest_file = pkg.find_package()
    if manifest_file is None:
        print(
            "flx: no path given and no package.flx in the current directory",
            file=sys.stderr,
        )
        return None
    try:
        manifest = pkg.load_manifest(manifest_file)
        roots = pkg.dependency_roots(manifest)
    except FlexError as err:
        _report(err, {})
        return None
    return str(manifest.dir / manifest.entry), roots


def _load(path: str, roots: tuple[Path, ...] = ()) -> ProgramInfo | FlexError:
    try:
        return load_program(path, roots)
    except FlexError as err:
        return err
    except RecursionError:
        return FlexError(
            [
                Diagnostic(
                    "PAR003",
                    "input is too deeply nested",
                    None,
                    help="this can be caused by a recursive type definition, "
                    "which is not supported yet",
                )
            ]
        )


def _frontend(
    path: str, roots: tuple[Path, ...] = ()
) -> tuple[CheckResult | FlexError, dict[str, str]]:
    loaded = _load(path, roots)
    if isinstance(loaded, FlexError):
        return loaded, {}
    try:
        module = expand(loaded.module)
        if module.targets and Path(path).name != "build.flx":
            first = module.targets[0]
            return (
                FlexError(
                    [
                        Diagnostic(
                            "BUILD004",
                            "target declarations are only allowed in build.flx",
                            first.span,
                            help="run targets with `flx build`",
                        )
                    ]
                ),
                loaded.sources,
            )
        result = check_and_monomorphize(
            module, loaded.decl_module, loaded.public, loaded.file_module
        )
        return result, loaded.sources
    except FlexError as err:
        return err, loaded.sources
    except RecursionError:
        return FlexError(
            [
                Diagnostic(
                    "PAR003",
                    "input is too deeply nested",
                    None,
                    help="this can be caused by a recursive type definition, "
                    "which is not supported yet",
                )
            ]
        ), loaded.sources


def _use_native(interpret: bool, native: bool) -> bool | None:
    """Choose a backend: explicit `--native` (errors if the toolchain is missing),
    explicit `--interpret`, or — the default — native when the toolchain is present
    and the interpreter otherwise. Returns None if `--native` was requested but the
    toolchain is unavailable (after printing guidance). The `flx` CLI defaults to
    the interpreter; this auto-default keeps `cmd_run`/`cmd_test` native-backed for
    the test suite when LLVM is present."""
    if native and not toolchain.available():
        print(
            "flx: --native requires an MLIR/LLVM 22 toolchain (run `flx doctor`)",
            file=sys.stderr,
        )
        return None
    return native or (toolchain.available() and not interpret)


def cmd_parse(path: str) -> int:
    # `parse` shows one file's AST verbatim — imports are not followed.
    source = _read(path)
    if source is None:
        return 1
    try:
        module = parse(source, path)
    except FlexError as err:
        _report(err, {path: source})
        return 1
    except RecursionError:
        print("flx: input is too deeply nested to parse", file=sys.stderr)
        return 1
    print(dump_module(module))
    return 0


def cmd_expand(path: str) -> int:
    loaded = _load(path)
    if isinstance(loaded, FlexError):
        _report(loaded, {})
        return 1
    try:
        module = expand(loaded.module)
    except FlexError as err:
        _report(err, loaded.sources)
        return 1
    except RecursionError:
        print("flx: input is too deeply nested", file=sys.stderr)
        return 1
    print(dump_module(module))
    return 0


def cmd_check(path: str | None = None) -> int:
    if path is not None and Path(path).name == pkg.MANIFEST_FILE:
        try:
            manifest = pkg.load_manifest(Path(path))
        except FlexError as err:
            _report(err, {})
            return 1
        print(f"ok: {path} is a valid manifest ({manifest.name} {manifest.version})")
        return 0
    resolved = _resolve_entry(path)
    if resolved is None:
        return 1
    entry, roots = resolved
    result, sources = _frontend(entry, roots)
    if isinstance(result, FlexError):
        _report(result, sources)
        return 1
    print(f"ok: {entry} type-checks")
    return 0


def _run_shim(main_ret: Type) -> str:
    if main_ret is UNIT:
        main = "extern void flx_main(void);\nint main(void){ flx_main(); return 0; }\n"
    elif main_ret is I64:
        main = "extern long long flx_main(void);\nint main(void){ return (int)flx_main(); }\n"
    else:
        raise FlexError([Diagnostic("RUN002", "main must return I64 or Unit")])
    return BASE_RUNTIME_C + main


def cmd_emit_mlir(path: str) -> int:
    resolved = _resolve_entry(path)
    if resolved is None:
        return 1
    entry, roots = resolved
    result, sources = _frontend(entry, roots)
    if isinstance(result, FlexError):
        _report(result, sources)
        return 1
    try:
        print(emit_module(result), end="")
    except BackendError as exc:
        print(f"flx: backend error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_run(path: str | None = None, interpret: bool = False, native: bool = False) -> int:
    resolved = _resolve_entry(path)
    if resolved is None:
        return 1
    entry, roots = resolved
    result, sources = _frontend(entry, roots)
    if isinstance(result, FlexError):
        _report(result, sources)
        return 1

    main = result.functions.get("main")
    if main is None:
        print(f"flx: {entry} has no `main` function to run", file=sys.stderr)
        return 1
    if main.params:
        print("flx: `main` must take no arguments", file=sys.stderr)
        return 1

    choice = _use_native(interpret, native)
    if choice is None:
        return 1
    if not choice:
        try:
            code = interp.run_main(result)
        except interp.FlexRuntimeError as exc:
            sys.stdout.flush()  # emit buffered output before the error (match native)
            print(f"flx: runtime error: {exc}", file=sys.stderr)
            return 1
        print(f"flx: exited with code {code}", file=sys.stderr)
        return code

    try:
        mlir_text = emit_module(result)
        shim = _run_shim(main.ret)
        with tempfile.TemporaryDirectory() as tmp:
            exe = build_executable(mlir_text, shim, Path(tmp) / "program", Path(tmp))
            code = run_executable(exe)
            print(f"flx: exited with code {code}", file=sys.stderr)
            return code
    except BackendError as exc:
        print(f"flx: backend error: {exc}", file=sys.stderr)
        return 1
    except FlexError as err:
        _report(err, sources)
        return 1


def cmd_test(
    path: str | None = None,
    test_filter: str | None = None,
    interpret: bool = False,
    native: bool = False,
) -> int:
    resolved = _resolve_entry(path)
    if resolved is None:
        return 1
    entry, roots = resolved
    result, sources = _frontend(entry, roots)
    if isinstance(result, FlexError):
        _report(result, sources)
        return 1

    choice = _use_native(interpret, native)
    if choice is None:
        return 1
    if not choice:
        try:
            return interp.run_tests(result, test_filter)
        except interp.FlexRuntimeError as exc:
            sys.stdout.flush()  # emit buffered output before the error (match native)
            print(f"flx: runtime error: {exc}", file=sys.stderr)
            return 1

    module = result.module
    selected = [
        (i, f"{result.file_module.get(t.span.file, module.name)} / {t.name}")
        for i, t in enumerate(module.tests)
        if test_filter is None or test_filter in t.name
    ]
    if not selected:
        print("running 0 tests\n")
        print("0 passed, 0 failed")
        return 0

    try:
        mlir_text = emit_program(result, with_tests=True)
        harness = generate_harness(selected)
        with tempfile.TemporaryDirectory() as tmp:
            exe = build_executable(mlir_text, harness, Path(tmp) / "tests", Path(tmp))
            return run_executable(exe)
    except BackendError as exc:
        print(f"flx: backend error: {exc}", file=sys.stderr)
        return 1
    except FlexError as err:
        _report(err, sources)
        return 1


def cmd_build(path: str | None = None, output: str | None = None) -> int:
    resolved = _resolve_entry(path)
    if resolved is None:
        return 1
    entry, roots = resolved
    result, sources = _frontend(entry, roots)
    if isinstance(result, FlexError):
        _report(result, sources)
        return 1

    main = result.functions.get("main")
    if main is None:
        print(f"flx: {entry} has no `main` function to build", file=sys.stderr)
        return 1
    if main.params:
        print("flx: `main` must take no arguments", file=sys.stderr)
        return 1

    out_path = Path(output) if output else Path(entry).with_suffix("")
    try:
        mlir_text = emit_module(result)
        shim = _run_shim(main.ret)
        with tempfile.TemporaryDirectory() as tmp:
            exe = build_executable(mlir_text, shim, out_path, Path(tmp))
    except BackendError as exc:
        print(f"flx: backend error: {exc}", file=sys.stderr)
        return 1
    except FlexError as err:
        _report(err, sources)
        return 1
    print(f"wrote {exe}")
    return 0


def cmd_doctor() -> int:
    """Report whether the optional native backend toolchain is available.

    Pure-Python commands (parse/check/expand/highlight) never need these tools;
    only run/test/emit-mlir/build do. Exit 0 iff the native backend is ready.
    """
    import subprocess

    from flx.backend.toolchain import LLVM_BIN, REQUIRED_TOOLS, find_tool

    print("flx doctor — native backend toolchain\n")
    missing: list[str] = []
    for name in REQUIRED_TOOLS:
        resolved = find_tool(name)
        if resolved is None:
            missing.append(name)
            print(f"  [x] {name:<16} not found")
            continue
        version = ""
        try:
            out = subprocess.run(
                [resolved, "--version"], capture_output=True, text=True, timeout=10
            )
            version = (
                (out.stdout or out.stderr).splitlines()[0].strip()
                if out.stdout or out.stderr
                else ""
            )
        except OSError, subprocess.SubprocessError:
            version = ""
        print(f"  [ok] {name:<16} {resolved}")
        if version:
            print(f"       {version}")

    print()
    if not missing:
        print("Native backend ready: `flx run` / `test` / `emit-mlir` / `build` will work.")
        return 0
    print(f"Missing: {', '.join(missing)}")
    print(
        "The native backend needs an MLIR/LLVM 22 toolchain on PATH or in "
        f"{LLVM_BIN}.\n"
        "  Debian/Ubuntu: see https://apt.llvm.org (install clang-22, llvm-22, mlir-22).\n"
        "Pure-Python commands work without it: "
        "`flx parse|check|expand|highlight` (and `flx doctor`)."
    )
    return 1
