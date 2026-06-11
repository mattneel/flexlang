"""Milestone 4 from the blind study: numerics + abstraction — F64 end-to-end,
hex/binary literals, bitwise operations, and pure function values."""

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


def _write(tmp_path: Path, src: str) -> str:
    flx = tmp_path / "main.flx"
    flx.write_text(src, encoding="utf-8")
    return str(flx)


def _diag(src: str) -> list:
    with pytest.raises(FlexError) as exc:
        check_and_monomorphize(expand(parse(src)))
    return exc.value.diagnostics


def _both(tmp_path: Path, src: str, code: int, out: str | None = None, capfd=None) -> None:
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == code
    interp_out = capfd.readouterr().out if capfd is not None else None
    if out is not None:
        assert interp_out == out
    if _tools_available():
        assert driver.cmd_run(path, native=True) == code
        if capfd is not None:
            assert capfd.readouterr().out == interp_out


# --- F64 -------------------------------------------------------------------------


def test_float_arithmetic_and_formatting(tmp_path: Path, capfd) -> None:
    _both(
        tmp_path,
        "module Main\nimport Std.IO\n"
        "fn main() -> I64 uses { Log } = {\n"
        "  println(to_str(1.5 + 0.25))\n"
        "  println(to_str(0.1 + 0.2))\n"
        "  println(to_str(1.0e16))\n"
        "  println(to_str(7.5 % 2.0))\n"
        "  println(to_str(-1.5))\n"
        "  0\n}\n",
        0,
        out="1.75\n0.30000000000000004\n1e+16\n1.5\n-1.5\n",
        capfd=capfd,
    )


def test_float_ieee_edges(tmp_path: Path, capfd) -> None:
    # Division does not trap; NaN compares false; both backends agree.
    _both(
        tmp_path,
        "module Main\nimport Std.IO\n"
        "fn main() -> I64 uses { Log } = {\n"
        "  println(to_str(1.0 / 0.0))\n"
        "  println(to_str(-1.0 / 0.0))\n"
        "  println(to_str(0.0 / 0.0))\n"
        '  if 0.0 / 0.0 == 0.0 / 0.0 { println("BAD") } else { println("nan ok") }\n'
        "  if 1.5 > 1.4 { 0 } else { 1 }\n}\n",
        0,
        out="inf\n-inf\nnan\nnan ok\n",
        capfd=capfd,
    )


def test_conversions(tmp_path: Path) -> None:
    _both(
        tmp_path,
        "fn main() -> I64 = { to_i64(to_f64(7) / 2.0) + to_i64(-3.99) }\n",
        0,  # 3 + (-3)
    )


def test_to_i64_of_nan_panics(tmp_path: Path, capfd) -> None:
    path = _write(tmp_path, "fn main() -> I64 = { to_i64(0.0 / 0.0) }\n")
    assert driver.cmd_run(path, interpret=True) == 1
    assert "cannot convert nan to I64" in capfd.readouterr().err
    if _tools_available():
        assert driver.cmd_run(path, native=True) == 1
        assert "cannot convert nan to I64" in capfd.readouterr().err


def test_libm_through_ffi(tmp_path: Path, capfd) -> None:
    _both(
        tmp_path,
        "module Main\nimport Std.IO\nimport Std.Math\n"
        "fn main() -> I64 uses { Log } = {\n"
        "  println(to_str(sqrt(2.0)))\n"
        "  println(to_str(floor(2.7) + ceil(0.2)))\n"
        "  println(to_str(fabs(-3.5)))\n"
        "  to_i64(sqrt(25.0))\n}\n",
        5,
        out="1.4142135623730951\n3\n3.5\n",
        capfd=capfd,
    )


def test_float_assert_reporters(tmp_path: Path, capfd) -> None:
    src = (
        "fn main() -> I64 = { 0 }\n"
        'test "feq" { assert_eq(1.5, 2.5) }\n'
        'test "fne" { assert_ne(1.5, 1.5) }\n'
    )
    path = _write(tmp_path, src)
    assert driver.cmd_test(path, interpret=True) == 1
    out = capfd.readouterr().out
    assert "assert_eq failed: actual 1.5, expected 2.5" in out
    assert "assert_ne failed: both are 1.5" in out
    if _tools_available():
        assert driver.cmd_test(path, native=True) == 1
        assert capfd.readouterr().out == out


def test_mixed_numeric_arithmetic_rejected() -> None:
    diags = _diag("fn main() -> I64 = { let x = 1 + 2.5\n 0 }\n")
    assert any("to_f64" in (d.help or "") for d in diags)


def test_float_literal_patterns_rejected() -> None:
    with pytest.raises(FlexError) as exc:
        parse("fn f(o: Option<F64>) -> I64 = { match o { Some(1.5) => 1  _ => 0 } }")
    assert any("float literal patterns" in d.message for d in exc.value.diagnostics)


def test_f64_in_records_lists_payloads(tmp_path: Path) -> None:
    _both(
        tmp_path,
        "type P = { x: F64, y: F64 }\n"
        "type M = | Val(F64) | Nope\n"
        "fn main() -> I64 = {\n"
        "  let p = { x = 1.5, y = 2.5 }\n"
        "  let xs = [0.5, 1.5]\n"
        "  let m = Val(4.0)\n"
        "  let got = match m { Val(v) => v  Nope => 0.0 }\n"
        "  to_i64(p.x + p.y + xs[0] + xs[1] + got)\n}\n",
        10,
    )


# --- hex / binary / bitwise --------------------------------------------------------


def test_hex_bin_and_bitwise(tmp_path: Path, capfd) -> None:
    _both(
        tmp_path,
        "module Main\nimport Std.IO\n"
        "fn main() -> I64 uses { Log } = {\n"
        "  println(to_str(0xFF & 0x0F))\n"
        "  println(to_str(0b1010 | 0b0101))\n"
        "  println(to_str(0xF0 ^ 0xFF))\n"
        "  println(to_str(1 << 10))\n"
        "  println(to_str(-16 >> 2))\n"
        "  println(to_str(0xFFFFFFFFFFFFFFFF))\n"
        "  println(to_str(1 << 100))\n"
        "  0\n}\n",
        0,
        out="15\n15\n15\n1024\n-4\n-1\n68719476736\n",
        capfd=capfd,
    )


def test_bitwise_precedence(tmp_path: Path) -> None:
    # Rust-style: & binds tighter than ==, so this is (0xFF & 0x0F) == 0x0F.
    _both(
        tmp_path,
        "fn main() -> I64 = { if 0xFF & 0x0F == 0x0F { 0 } else { 1 } }\n",
        0,
    )


def test_oversized_hex_rejected() -> None:
    with pytest.raises(FlexError) as exc:
        parse("fn main() -> I64 = { 0x1FFFFFFFFFFFFFFFF }")
    assert any("does not fit in 64 bits" in d.message for d in exc.value.diagnostics)


def test_nested_generics_close_with_shr(tmp_path: Path) -> None:
    # `Bad<Option<T>>` ends in what now lexes as `>>`; the parser splits it.
    src = (
        "type Wrap<T> = | W(T)\n"
        "fn main() -> I64 = {\n"
        "  let w: Wrap<Option<I64>> = W(Some(6))\n"
        "  match w { W(o) => match o { Some(n) => n  None => 0 } }\n}\n"
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 6


# --- function values -----------------------------------------------------------------


HOF = (
    "module Main\nimport Std.List\n"
    "fn double(x: I64) -> I64 = { x * 2 }\n"
    "fn is_even(x: I64) -> Bool = { x % 2 == 0 }\n"
    "fn add(a: I64, b: I64) -> I64 = { a + b }\n"
    "fn apply2(f: (I64) -> I64, v: I64) -> I64 = { f(f(v)) }\n"
)


def test_function_params_and_let(tmp_path: Path) -> None:
    _both(
        tmp_path,
        HOF + "fn main() -> I64 = { let f = double\n apply2(f, 5) + apply2(double, 1) }\n",
        24,
    )


def test_map_filter_fold(tmp_path: Path) -> None:
    _both(
        tmp_path,
        HOF + "fn main() -> I64 = {\n"
        "  let total = fold(range(1, 11), 0, add)\n"
        "  total + List.len(filter(range(1, 10), is_even)) + map([3], double)[0]\n}\n",
        65,  # 55 + 4 + 6
    )


def test_map_over_strings(tmp_path: Path, capfd) -> None:
    _both(
        tmp_path,
        "module Main\nimport Std.IO\nimport Std.List\n"
        'fn shout(s: String) -> String = { s ++ "!" }\n'
        "fn main() -> I64 uses { Log } = {\n"
        '  for s in map(["hi", "yo"], shout) { println(s) }\n  0\n}\n',
        0,
        out="hi!\nyo!\n",
        capfd=capfd,
    )


def test_param_shadows_global_fn(tmp_path: Path) -> None:
    # A fn-typed parameter named like a global calls the PARAMETER.
    _both(
        tmp_path,
        "fn double(x: I64) -> I64 = { x * 2 }\n"
        "fn triple(x: I64) -> I64 = { x * 3 }\n"
        "fn run(double: (I64) -> I64, v: I64) -> I64 = { double(v) }\n"
        "fn main() -> I64 = { run(triple, 5) }\n",
        15,
    )


def test_effectful_fn_value_rejected() -> None:
    diags = _diag(
        'fn shouty() -> I64 uses { Log } = { Log.info("x")\n 41 }\n'
        "fn main() -> I64 uses { Log } = { let g = shouty\n g() }\n"
    )
    assert any(d.code == "NAME003" and "pure" in d.message for d in diags)


def test_generic_fn_value_rejected() -> None:
    diags = _diag("fn id<T>(x: T) -> T = { x }\nfn main() -> I64 = { let g = id\n 0 }\n")
    assert any(d.code == "NAME003" and "generic" in d.message for d in diags)


def test_fn_storage_rejected() -> None:
    diags = _diag("fn d(x: I64) -> I64 = { x }\nfn main() -> I64 = { mut f = d\n 0 }\n")
    assert any(d.code == "TYPE025" for d in diags)
    diags = _diag("type R = { f: (I64) -> I64 }\nfn main() -> I64 = { 0 }\n")
    assert any(d.code == "TYPE025" for d in diags)
    diags = _diag(
        "fn d(x: I64) -> I64 = { x }\nfn main() -> I64 = { let xs: List<(I64) -> I64> = []\n 0 }\n"
    )
    assert any(d.code == "TYPE025" for d in diags)


def test_effectful_fn_type_syntax_rejected() -> None:
    with pytest.raises(FlexError) as exc:
        parse("fn run(f: (I64) -> I64 uses { Log }) -> I64 = { f(1) }")
    assert any("must be pure" in d.message for d in exc.value.diagnostics)
