"""Milestone 1 from the blind study: Std.IO, monotonic time, string assert_eq,
the unit literal, and the diagnostics batch."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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


def _write(tmp_path: Path, src: str) -> str:
    flx = tmp_path / "main.flx"
    flx.write_text(src, encoding="utf-8")
    return str(flx)


def _diag(src: str) -> list:
    with pytest.raises(FlexError) as exc:
        check_and_monomorphize(expand(parse(src)))
    return exc.value.diagnostics


# --- Std.IO + Time --------------------------------------------------------------

IO_PROGRAM = (
    "module Main\nimport Std.IO\nimport Std.Time\n"
    "fn main() -> I64 uses { Log, Time } = {\n"
    '  print("a")\n  print("b")\n  println("c")\n'
    "  let t0 = monotonic_ms()\n"
    "  mut i = 0\n  while i < 10000 { i = i + 1 }\n"
    "  if monotonic_ms() - t0 >= 0 { 0 } else { 1 }\n}\n"
)


def test_io_and_time_interpret(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert driver.cmd_run(_write(tmp_path, IO_PROGRAM), interpret=True) == 0
    assert capfd.readouterr().out == "abc\n"


@native
def test_io_matches_native(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, IO_PROGRAM)
    native_code = driver.cmd_run(path, native=True)
    native_out = capfd.readouterr().out
    interp_code = driver.cmd_run(path, interpret=True)
    interp_out = capfd.readouterr().out
    assert (interp_code, interp_out) == (native_code, native_out) == (0, "abc\n")


def test_read_line_eof_is_empty(tmp_path: Path) -> None:
    # Closed stdin: read_line() yields "".
    src = (
        "module Main\nimport Std.IO\nimport Std.Str\n"
        "fn main() -> I64 uses { Fs } = { length(read_line()) }\n"
    )
    path = _write(tmp_path, src)
    proc = subprocess.run(
        [sys.executable, "-m", "flx", "run", path], input="", capture_output=True, text=True
    )
    assert proc.returncode == 0


def test_read_line_via_cli(tmp_path: Path) -> None:
    src = (
        "module Main\nimport Std.IO\nimport Std.Str\n"
        "fn main() -> I64 uses { Fs, Log } = { let l = read_line()\n"
        '  println("got: " ++ l)\n  length(l) }\n'
    )
    path = _write(tmp_path, src)
    proc = subprocess.run(
        [sys.executable, "-m", "flx", "run", path],
        input="hello\n",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 5
    assert "got: hello" in proc.stdout


def test_fn_named_print_does_not_collide(tmp_path: Path) -> None:
    # User functions live in flx_<name>; runtime symbols moved to __flx_*.
    src = (
        "fn log(n: I64) -> I64 = { n }\nfn print(n: I64) -> I64 = { n }\n"
        "fn main() -> I64 = { log(40) + print(2) }\n"
    )
    assert driver.cmd_run(_write(tmp_path, src), interpret=True) == 42


@native
def test_fn_named_print_does_not_collide_native(tmp_path: Path) -> None:
    src = (
        "fn log(n: I64) -> I64 = { n }\nfn print(n: I64) -> I64 = { n }\n"
        "fn main() -> I64 = { log(40) + print(2) }\n"
    )
    assert driver.cmd_run(_write(tmp_path, src), native=True) == 42


# --- string assert_eq -----------------------------------------------------------

STR_ASSERT = (
    "module Main\nimport Std.Str\nfn main() -> I64 = { 0 }\n"
    'test "pass" { assert_eq("flex", "flex")\n  assert_ne("a", "b") }\n'
    'test "fail" { assert_eq("flexx", "flex") }\n'
)


def test_string_assert_eq(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert driver.cmd_test(_write(tmp_path, STR_ASSERT), interpret=True) == 1
    out = capfd.readouterr().out
    assert "ok Main / pass" in out
    assert 'assert_eq failed: actual "flexx", expected "flex"' in out


@native
def test_string_assert_matches_native(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, STR_ASSERT)
    native_code = driver.cmd_test(path, native=True)
    native_out = capfd.readouterr().out
    interp_code = driver.cmd_test(path, interpret=True)
    interp_out = capfd.readouterr().out
    assert (interp_code, interp_out) == (native_code, native_out)


def test_string_assert_requires_std_str(tmp_path: Path) -> None:
    diags = _diag('fn main() -> I64 = { 0 }\ntest "t" { assert_eq("a", "a") }\n')
    assert any(d.code == "TYPE019" and "Std.Str" in (d.help or "") for d in diags)


# --- unit literal ---------------------------------------------------------------


def test_unit_literal(tmp_path: Path) -> None:
    src = "fn main() -> I64 = { if true { () } else { () }\n 7 }\n"
    assert driver.cmd_run(_write(tmp_path, src), interpret=True) == 7


@native
def test_unit_literal_native(tmp_path: Path) -> None:
    src = "fn main() -> I64 = { if true { () } else { () }\n 7 }\n"
    assert driver.cmd_run(_write(tmp_path, src), native=True) == 7


# --- diagnostics batch ----------------------------------------------------------


def _codes_and_text(src: str) -> str:
    diags = _diag(src)
    return " | ".join(f"{d.code}:{d.message}:{d.help or ''}" for d in diags)


def test_plus_on_strings_hints_concat() -> None:
    text = _codes_and_text('fn f() -> String = { "a" + "b" }\nfn main() -> I64 = { 0 }')
    assert "does not concatenate" in text and "++" in text


def test_float_literal_diagnostic() -> None:
    text = _codes_and_text("fn main() -> I64 = { let x = 12.5\n 0 }")
    assert "floating-point literals are not supported" in text


def test_hex_literal_diagnostic() -> None:
    text = _codes_and_text("fn main() -> I64 = { let x = 0xFF\n 0 }")
    assert "hexadecimal and binary literals are not supported" in text


def test_indexing_diagnostic() -> None:
    # Previously `s[0]` silently parsed as two statements and produced wrong values.
    text = _codes_and_text('fn main() -> I64 = { let s = "abc"\n let c = s[0]\n 0 }')
    assert "indexing" in text and "not supported" in text


def test_match_arm_blocks_work(tmp_path: Path) -> None:
    # M1 rejected block arm bodies with a diagnostic; M2 made them legal.
    src = (
        "type C = | A | B\nfn main() -> I64 = { match A { A => { let x = 3\n  x * 2 }  B => 2 } }\n"
    )
    assert driver.cmd_run(_write(tmp_path, src), interpret=True) == 6


def test_reserved_keyword_described() -> None:
    text = _codes_and_text("fn f(target: I64) -> I64 = { target }\nfn main() -> I64 = { 0 }")
    assert "reserved keyword" in text


def test_missing_std_module_message(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, "module Main\nimport Std.Fs\nfn main() -> I64 = { 0 }\n")
    assert driver.cmd_check(path) == 1
    assert "standard library has no module 'Std.Fs'" in capfd.readouterr().err


def test_recursive_types_check(tmp_path: Path) -> None:
    # M1 could only hint at this; M2 made recursive ADTs real.
    src = (
        "type N = | Zero | Succ(N)\n"
        "fn main() -> I64 = { match Succ(Zero) { Zero => 0  Succ(n) => 1 } }\n"
    )
    assert driver.cmd_check(_write(tmp_path, src)) == 0


def test_run_reports_exit_code_on_stderr(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, "fn main() -> I64 = { 7 }\n")
    assert driver.cmd_run(path, interpret=True) == 7
    captured = capfd.readouterr()
    assert captured.out == ""  # stdout stays parity-clean
    assert "flx: exited with code 7" in captured.err


# --- M1 review fixes --------------------------------------------------------------

UNIT_VALUES = (
    'fn side() -> Unit uses { Log } = { Log.info("side") }\n'
    "fn take_unit(u: Unit) -> I64 = { 21 }\n"
    "type Box = { tag: I64, u: Unit }\n"
    "fn main() -> I64 uses { Log } = {\n"
    "  if () == () { () } else { () }\n"
    "  mut m = ()\n  m = side()\n  let u = ()\n"
    "  let b = { tag = take_unit(u) + take_unit(side()) + take_unit(()), u = m }\n"
    "  b.tag\n}\n"
)

UNIT_ASSERTS = (
    "fn main() -> I64 = { 0 }\n"
    'test "unit eq" { assert_eq((), ()) }\n'
    'test "unit ne fails" { assert_ne((), ()) }\n'
)


def test_unit_values_interpret(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert driver.cmd_run(_write(tmp_path, UNIT_VALUES), interpret=True) == 63
    assert capfd.readouterr().out == "side\nside\n"


@native
def test_unit_values_native(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    # Unit params, record fields, mut slots, and () == () must not ICE natively.
    assert driver.cmd_run(_write(tmp_path, UNIT_VALUES), native=True) == 63
    assert capfd.readouterr().out == "side\nside\n"


def test_unit_assert_ne_message(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert driver.cmd_test(_write(tmp_path, UNIT_ASSERTS), interpret=True) == 1
    out = capfd.readouterr().out
    assert "ok Main / unit eq" in out
    assert "assert_ne failed: both are 0" in out  # unit prints as 0, like native


@native
def test_unit_asserts_match_native(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, UNIT_ASSERTS)
    native_code = driver.cmd_test(path, native=True)
    native_out = capfd.readouterr().out
    interp_code = driver.cmd_test(path, interpret=True)
    interp_out = capfd.readouterr().out
    assert (interp_code, interp_out) == (native_code, native_out)


def test_read_line_shares_libc_stdin_buffer(tmp_path: Path) -> None:
    # read_line (libc fgetc) and an extern getchar() consume ONE C stdio buffer,
    # exactly as the native getline/getchar pair does; two competing read-ahead
    # buffers over fd 0 would starve each other.
    src = (
        "module Main\nimport Std.IO\n"
        "extern fn getchar() -> I32 uses { Fs }\n"
        "fn main() -> I64 uses { Fs, Log } = {\n"
        "  let a = read_line()\n  let c1 = getchar()\n  let c2 = getchar()\n"
        "  let b = read_line()\n"
        '  println("a=[" ++ a ++ "] c1=" ++ to_str(c1) ++ " c2=" ++ to_str(c2)'
        ' ++ " b=[" ++ b ++ "]")\n  0\n}\n'
    )
    proc = subprocess.run(
        [sys.executable, "-m", "flx", "run", _write(tmp_path, src)],
        input="ab\ncd\nef\n",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout == "a=[ab] c1=99 c2=100 b=[]\n"  # native byte-sequential order


def test_build_does_not_leak_exit_banner(tmp_path: Path) -> None:
    (tmp_path / "hello.flx").write_text(
        'fn main() -> I64 uses { Log } = { Log.info("hi")\n 0 }\n', encoding="utf-8"
    )
    (tmp_path / "build.flx").write_text(
        'module Build\n\ntarget go uses { Fs } {\n  flx.run("hello.flx")?\n}\n',
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "flx", "build", "go"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "hi" in proc.stdout
    assert "exited with code" not in proc.stdout + proc.stderr


def test_runtime_error_prints_exit_line(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    # Native binaries print the runtime error and exit(1), then the driver reports
    # the code; the interpreter path must produce the same stderr shape.
    path = _write(tmp_path, "fn main() -> I64 = { let z = 0\n 1 / z }\n")
    assert driver.cmd_run(path, interpret=True) == 1
    err = capfd.readouterr().err
    assert "flx: runtime error: division by zero" in err
    assert "flx: exited with code 1" in err


RECORD_UPDATE_ARMS = (
    "type P = { x: I64, y: I64 }\ntype C = | A | B\n"
    "fn mk(n: I64) -> P = { { x = n, y = n } }\n"
    "type Holder = { p: P }\n"
    "fn main() -> I64 = {\n"
    "  let h = { p = mk(3) }\n"
    "  let a = match A { A => { mk(2) with x = 10 }  B => { h.p with y = 9 } }\n"
    "  let b = match B { A => { mk(2) with x = 10 }  B => { h.p with y = 9 } }\n"
    "  a.x + a.y + b.x + b.y\n}\n"
)


def test_record_update_arm_bodies(tmp_path: Path) -> None:
    # `{ <expr> with ... }` arm bodies (call/member bases) are records, not blocks.
    assert driver.cmd_run(_write(tmp_path, RECORD_UPDATE_ARMS), interpret=True) == 24


@native
def test_record_update_arm_bodies_native(tmp_path: Path) -> None:
    assert driver.cmd_run(_write(tmp_path, RECORD_UPDATE_ARMS), native=True) == 24


def test_paren_multiline_postfix(tmp_path: Path) -> None:
    # The span of a parenthesized expression reaches the `)`, so a postfix `.x`
    # on the closing-paren line continues the expression.
    src = (
        "type P = { x: I64, y: I64 }\n"
        "fn main() -> I64 = {\n  let p = { x = 6, y = 1 }\n  let v = (\n    p\n  ).x\n  v\n}\n"
    )
    assert driver.cmd_run(_write(tmp_path, src), interpret=True) == 6


@native
def test_list_native_message(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, "fn main() -> I64 = { let xs = [1, 2, 3]\n 0 }\n")
    assert driver.cmd_run(path, native=True) == 1
    assert "list literals are not supported in native builds yet" in capfd.readouterr().err
