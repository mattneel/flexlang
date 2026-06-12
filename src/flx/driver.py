"""Command implementations behind the `flx` CLI.

Each `cmd_*` returns a process exit code and renders diagnostics to stderr.
"""

from __future__ import annotations

import os
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
from flx.diagnostics import Diagnostic, FlexError, Span
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


def _discover_test_files(path: Path) -> list[Path]:
    """Flex source files under a directory, excluding project/build metadata."""
    return [
        file
        for file in sorted(path.rglob("*.flx"))
        if file.name not in {pkg.MANIFEST_FILE, "build.flx"}
    ]


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
                    help="simplify the expression or split it into intermediate `let`s",
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
            module,
            loaded.decl_module,
            loaded.public,
            loaded.file_module,
            loaded.module_spans,
            loaded.module_imports,
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
                    help="simplify the expression or split it into intermediate `let`s",
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


def _discover_fmt_files(paths: list[str]) -> tuple[list[Path], bool]:
    files: list[Path] = []
    ok = True
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.flx") if p.is_file()))
        elif path.is_file():
            files.append(path)
        else:
            print(f"flx fmt: cannot find {raw}", file=sys.stderr)
            ok = False
    return files, ok


def cmd_fmt(paths: list[str], *, check: bool = False, stdout: bool = False) -> int:
    files, ok = _discover_fmt_files(paths)
    if not ok:
        return 1
    if not files:
        print("flx fmt: no .flx files found", file=sys.stderr)
        return 1
    if stdout and (check or len(files) != 1):
        print(
            "flx fmt: --stdout requires exactly one file and cannot be combined with --check",
            file=sys.stderr,
        )
        return 2

    from flx.syntax.formatter import format_source

    changed = False
    for file in files:
        source = _read(str(file))
        if source is None:
            return 1
        try:
            formatted = format_source(source, str(file))
        except FlexError as err:
            _report(err, {str(file): source})
            return 1
        except RecursionError:
            print(f"flx fmt: {file}: input is too deeply nested", file=sys.stderr)
            return 1
        if stdout:
            sys.stdout.write(formatted)
            return 0
        if formatted == source:
            continue
        changed = True
        if check:
            print(f"flx fmt: would reformat {file}", file=sys.stderr)
        else:
            try:
                file.write_text(formatted, encoding="utf-8")
            except OSError as exc:
                print(f"flx fmt: {file}: {exc}", file=sys.stderr)
                return 1
    return 1 if check and changed else 0


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
    # The shim main captures argc/argv for Env.argv before entering Flex.
    if main_ret is UNIT:
        main = (
            "extern void flx_main(void);\n"
            "void __flx_set_args(int, char **);\n"
            "int main(int argc, char **argv){ __flx_set_args(argc, argv); "
            "flx_main(); return 0; }\n"
        )
    elif main_ret is I64:
        main = (
            "extern long long flx_main(void);\n"
            "void __flx_set_args(int, char **);\n"
            "int main(int argc, char **argv){ __flx_set_args(argc, argv); "
            "return (int)flx_main(); }\n"
        )
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


def cmd_run(
    path: str | None = None,
    interpret: bool = False,
    native: bool = False,
    announce: bool = True,
    args: tuple[str, ...] = (),
) -> int:
    """Run a program. `announce` controls the `flx: exited with code N` stderr
    line — a CLI affordance that in-process callers (build targets) suppress.
    `args` are the program's own arguments, surfaced through Env.argv."""
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
    if main.ret is not I64 and main.ret is not UNIT:
        # One rule for both backends (the native shim can only return an int;
        # the interpreter must not silently accept what native rejects).
        print(
            f"flx: `main` must return I64 or Unit, not {main.ret}",
            file=sys.stderr,
        )
        return 1

    choice = _use_native(interpret, native)
    if choice is None:
        return 1
    if not choice:
        try:
            code = interp.run_main(result, args)
        except BrokenPipeError:
            # The reader went away (e.g. `flx run ... | head`). A native binary
            # dies of SIGPIPE and reports 128+13; match it, and point stdout at
            # the void so interpreter shutdown doesn't print a second traceback.
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            if announce:
                print("flx: exited with code 141", file=sys.stderr)
            return 141
        except interp.FlexRuntimeError as exc:
            sys.stdout.flush()  # emit buffered output before the error (match native)
            print(f"flx: runtime error: {exc}", file=sys.stderr)
            # Native binaries print their runtime error and exit(1); the driver
            # then reports the code. Match that stderr shape exactly.
            if announce:
                print("flx: exited with code 1", file=sys.stderr)
            return 1
        if announce and code != 0:
            print(f"flx: exited with code {code}", file=sys.stderr)
        return code

    try:
        mlir_text = emit_module(result)
        shim = _run_shim(main.ret)
        with tempfile.TemporaryDirectory() as tmp:
            exe = build_executable(mlir_text, shim, Path(tmp) / "program", Path(tmp))
            code = run_executable(exe, args)
            if announce and code != 0:
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
    fmt: str = "pretty",
) -> int:
    if path is not None:
        candidate = Path(path)
        if candidate.is_dir():
            if fmt != "pretty":
                print("flx test: structured output requires a single .flx file", file=sys.stderr)
                return 2
            files = _discover_test_files(candidate)
            status = 0
            for file in files:
                code = cmd_test(str(file), test_filter, interpret=interpret, native=native, fmt=fmt)
                if code != 0:
                    status = code
            if status == 0 and not files:
                print("running 0 tests\n")
                print("0 passed, 0 failed")
            return status

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
            if fmt != "pretty":
                return interp.run_tests_structured(result, test_filter, fmt)
            return interp.run_tests(result, test_filter)
        except interp.FlexRuntimeError as exc:
            sys.stdout.flush()  # emit buffered output before the error (match native)
            print(f"flx: runtime error: {exc}", file=sys.stderr)
            return 1
    if fmt != "pretty":
        print("flx test: structured output is only available on the interpreter", file=sys.stderr)
        return 2

    module = result.module

    def _module_of_test(test_span: Span | None) -> str:
        if test_span is None:
            return module.name
        for name, module_span in result.module_spans:
            if (
                module_span.file == test_span.file
                and module_span.start.offset <= test_span.start.offset
                and test_span.end.offset <= module_span.end.offset
            ):
                return name
        return result.file_module.get(test_span.file, module.name)

    selected = [
        (i, f"{_module_of_test(t.span)} / {t.name}")
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
