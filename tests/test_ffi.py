"""FFI / C ABI: `extern fn` declarations, both backends."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.macro import expand
from flx.sema.specialize import check_and_monomorphize
from flx.syntax.parser import parse


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"
    return all(
        bool(shutil.which(t)) or os.path.exists(os.path.join(bindir, t))
        for t in ("mlir-opt", "mlir-translate", "clang")
    )


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


def _codes(src: str) -> list[str]:
    with pytest.raises(FlexError) as exc:
        check_and_monomorphize(expand(parse(src)))
    return [d.code for d in exc.value.diagnostics]


def _write(tmp_path: Path, src: str, name: str = "f.flx") -> str:
    flx = tmp_path / name
    flx.write_text(src, encoding="utf-8")
    return str(flx)


FFI_PROGRAM = """module Main
extern fn llabs(n: I64) -> I64
extern fn strlen(s: String) -> I64
extern fn getenv(name: String) -> String
fn main() -> I64 = { llabs(0 - 40) + strlen("ab") }
test "calls" {
  assert_eq(llabs(0 - 7), 7)
  assert_eq(strlen("hello"), 5)
  assert_eq(strlen(getenv("FLX_NO_SUCH_VAR_XYZ")), 0)
}
"""


# --- checking ------------------------------------------------------------------


def test_unsupported_param_type_rejected() -> None:
    assert "FFI002" in _codes("extern fn f(b: Bool) -> I64\nfn main() -> I64 = { 0 }")
    assert "FFI002" in _codes(
        "type P = { x: I64 }\nextern fn g(p: P) -> I64\nfn main() -> I64 = { 0 }"
    )


def test_unsupported_return_type_rejected() -> None:
    assert "FFI002" in _codes("extern fn f() -> Bool\nfn main() -> I64 = { 0 }")


def test_extern_effects_propagate_to_callers() -> None:
    src = (
        "extern fn puts(s: String) -> I64 uses { Process }\n"
        'fn main() -> I64 = { let r = puts("hi")\n 0 }\n'
    )
    assert "EFFECT001" in _codes(src)


def test_extern_shares_function_namespace() -> None:
    src = "extern fn f() -> I64\nfn f() -> I64 = { 0 }\nfn main() -> I64 = { 0 }"
    assert "TYPE002" in _codes(src)


def test_extern_arity_checked() -> None:
    src = "extern fn llabs(n: I64) -> I64\nfn main() -> I64 = { llabs(1, 2) }"
    assert "TYPE005" in _codes(src)


def test_bare_function_reference_rejected() -> None:
    # No first-class function values: an alias would also sidestep effect checks.
    src = (
        "extern fn puts(s: String) -> I64 uses { Process }\n"
        'fn main() -> I64 = { let f = puts\n let r = f("hi")\n 0 }\n'
    )
    assert "NAME003" in _codes(src)
    assert "NAME003" in _codes(
        "fn helper() -> I64 = { 41 }\nfn main() -> I64 = { let g = helper\n g() }\n"
    )


@pytest.mark.parametrize(
    "decl",
    [
        "extern fn flx_log(s: String) -> I64",  # runtime namespace
        "extern fn __secret() -> I64",
        "extern fn main() -> I64",
        "extern fn to_str(n: I64) -> String",  # builtin
        "extern fn Some(n: I64) -> I64",  # constructor
    ],
)
def test_reserved_extern_names_rejected(decl: str) -> None:
    assert "FFI003" in _codes(f"{decl}\nfn main() -> I64 = {{ 0 }}")


def test_non_ascii_extern_name_rejected() -> None:
    assert "FFI003" in _codes("extern fn übercall(n: I64) -> I64\nfn main() -> I64 = { 0 }")


# --- running (interpreter; no toolchain needed) ---------------------------------


def test_ffi_runs_on_interpreter(tmp_path: Path) -> None:
    path = _write(tmp_path, FFI_PROGRAM)
    assert driver.cmd_run(path, interpret=True) == 42
    assert driver.cmd_test(path, interpret=True) == 0


def test_missing_symbol_is_clean(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    src = (
        "extern fn no_such_sym_xyz_12345() -> I64\nfn main() -> I64 = { no_such_sym_xyz_12345() }\n"
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 1
    assert "extern symbol 'no_such_sym_xyz_12345' not found" in capfd.readouterr().err


def test_pub_extern_across_modules(tmp_path: Path) -> None:
    lib = tmp_path / "Lib"
    lib.mkdir()
    (lib / "C.flx").write_text(
        "module Lib.C\npub extern fn llabs(n: I64) -> I64\nextern fn getpid() -> I64\n",
        encoding="utf-8",
    )
    main = _write(
        tmp_path, "module Main\nimport Lib.C\nfn main() -> I64 = { llabs(0 - 42) }\n", "main.flx"
    )
    assert driver.cmd_run(main, interpret=True) == 42


def test_private_extern_hidden(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    lib = tmp_path / "Lib"
    lib.mkdir()
    (lib / "C.flx").write_text(
        "module Lib.C\nextern fn getpid() -> I64\n",
        encoding="utf-8",
    )
    main = _write(
        tmp_path, "module Main\nimport Lib.C\nfn main() -> I64 = { getpid() }\n", "main.flx"
    )
    assert driver.cmd_check(main) == 1
    assert "VIS001" in capfd.readouterr().err


def test_extern_usable_in_build_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    from flx.build import run_build

    (tmp_path / "build.flx").write_text(
        "module Build\n"
        "extern fn llabs(n: I64) -> I64\n"
        "target default = t\n"
        "target t uses { Process } "
        '{ if llabs(0 - 1) == 1 { sh("true")? } else { sh("false")? } }\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert run_build() == 0


def test_non_utf8_roundtrip_byte_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Bytes from C survive the Flex String round-trip losslessly: strlen counts
    # the original bytes (5 here: c a f \xe9 \xff), matching native.
    monkeypatch.setenv("FLX_WEIRD_BYTES", "caf\udce9\udcff")  # surrogateescape of \xe9\xff
    src = (
        "extern fn strlen(s: String) -> I64\n"
        "extern fn getenv(name: String) -> String\n"
        'fn main() -> I64 = { strlen(getenv("FLX_WEIRD_BYTES")) }\n'
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 5


# --- native parity ---------------------------------------------------------------


@native
def test_ffi_matches_native(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, FFI_PROGRAM)
    native_code = driver.cmd_run(path, native=True)
    native_out = capfd.readouterr().out
    interp_code = driver.cmd_run(path, interpret=True)
    interp_out = capfd.readouterr().out
    assert (interp_code, interp_out) == (native_code, native_out)
    assert native_code == 42

    native_test = driver.cmd_test(path, native=True)
    native_test_out = capfd.readouterr().out
    interp_test = driver.cmd_test(path, interpret=True)
    interp_test_out = capfd.readouterr().out
    assert (interp_test, interp_test_out) == (native_test, native_test_out)
    assert native_test == 0


@native
def test_abort_parity(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    # A signal-terminating extern: prior Flex output survives (flx_log flushes
    # per line) and the exit code is shell-style 128+sig on the native path.
    src = (
        "extern fn abort() uses { Process }\n"
        "fn main() -> I64 uses { Log, Process } = {\n"
        '  Log.info("before abort")\n  abort()\n  0\n}\n'
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, native=True) == 134  # 128 + SIGABRT
    assert "before abort" in capfd.readouterr().out


@native
def test_extern_with_effect_prints_via_c(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    src = (
        "extern fn puts(s: String) -> I64 uses { Process }\n"
        'fn main() -> I64 uses { Process } = { let r = puts("hi from C")\n 0 }\n'
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, native=True) == 0
    assert "hi from C" in capfd.readouterr().out
