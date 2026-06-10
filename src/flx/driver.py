"""Command implementations behind the `flx` CLI.

Each `cmd_*` returns a process exit code and renders diagnostics to stderr.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from flx.backend.harness import generate_harness
from flx.backend.mlir import BackendError, emit_module, emit_program
from flx.backend.toolchain import build_executable, run_executable
from flx.diagnostics import Diagnostic, FlexError
from flx.sema.check import CheckResult, check
from flx.syntax.dump import dump_module
from flx.syntax.parser import parse
from flx.types import I64, UNIT, Type


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"flx: {exc}", file=sys.stderr)
        return None


def _report(err: FlexError, source: str) -> None:
    for diag in err.diagnostics:
        print(diag.render(source), file=sys.stderr)
        print(file=sys.stderr)


def cmd_parse(path: str) -> int:
    source = _read(path)
    if source is None:
        return 1
    try:
        module = parse(source, path)
    except FlexError as err:
        _report(err, source)
        return 1
    print(dump_module(module))
    return 0


def _parse_and_check(path: str, source: str) -> CheckResult | FlexError:
    try:
        module = parse(source, path)
        return check(module)
    except FlexError as err:
        return err


def cmd_check(path: str) -> int:
    source = _read(path)
    if source is None:
        return 1
    result = _parse_and_check(path, source)
    if isinstance(result, FlexError):
        _report(result, source)
        return 1
    print(f"ok: {path} type-checks")
    return 0


def _run_shim(main_ret: Type) -> str:
    if main_ret is UNIT:
        return "extern void flx_main(void);\nint main(void){ flx_main(); return 0; }\n"
    if main_ret is I64:
        return "extern long long flx_main(void);\nint main(void){ return (int)flx_main(); }\n"
    raise FlexError([Diagnostic("RUN002", "main must return I64 or Unit")])


def cmd_emit_mlir(path: str) -> int:
    source = _read(path)
    if source is None:
        return 1
    result = _parse_and_check(path, source)
    if isinstance(result, FlexError):
        _report(result, source)
        return 1
    try:
        print(emit_module(result), end="")
    except BackendError as exc:
        print(f"flx: backend error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_run(path: str) -> int:
    source = _read(path)
    if source is None:
        return 1
    result = _parse_and_check(path, source)
    if isinstance(result, FlexError):
        _report(result, source)
        return 1

    main = result.functions.get("main")
    if main is None:
        print(f"flx: {path} has no `main` function to run", file=sys.stderr)
        return 1
    if main.params:
        print("flx: `main` must take no arguments", file=sys.stderr)
        return 1

    try:
        mlir_text = emit_module(result)
        shim = _run_shim(main.ret)
        with tempfile.TemporaryDirectory() as tmp:
            exe = build_executable(mlir_text, shim, Path(tmp) / "program", Path(tmp))
            return run_executable(exe)
    except BackendError as exc:
        print(f"flx: backend error: {exc}", file=sys.stderr)
        return 1
    except FlexError as err:
        _report(err, source)
        return 1


def cmd_test(path: str, test_filter: str | None = None) -> int:
    source = _read(path)
    if source is None:
        return 1
    result = _parse_and_check(path, source)
    if isinstance(result, FlexError):
        _report(result, source)
        return 1

    module = result.module
    selected = [
        (i, t.name)
        for i, t in enumerate(module.tests)
        if test_filter is None or test_filter in t.name
    ]
    if not selected:
        print("running 0 tests\n")
        print("0 passed, 0 failed")
        return 0

    try:
        mlir_text = emit_program(result, with_tests=True)
        harness = generate_harness(module.name, selected)
        with tempfile.TemporaryDirectory() as tmp:
            exe = build_executable(mlir_text, harness, Path(tmp) / "tests", Path(tmp))
            return run_executable(exe)
    except BackendError as exc:
        print(f"flx: backend error: {exc}", file=sys.stderr)
        return 1
    except FlexError as err:
        _report(err, source)
        return 1
