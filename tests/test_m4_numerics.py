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


def test_hex_literal_patterns(tmp_path: Path) -> None:
    # `match n { 0x10 => ... }` ICEd: the pattern path parsed with int(),
    # not int(text, 0). Hex bit patterns work in patterns like in expressions.
    _both(
        tmp_path,
        "type O = | G(I64) | N\nfn main() -> I64 = { match G(16) { G(0x10) => 1  _ => 0 } }\n",
        1,
    )


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


# --- review findings -----------------------------------------------------------------


def test_runtime_nan_prints_unsigned(tmp_path: Path, capfd) -> None:
    # x86 produces SIGN-SET NaNs at runtime; glibc %g would print "-nan" while
    # Python never signs one. Both backends canonicalize to "nan". The zero is
    # runtime-derived so LLVM cannot constant-fold the NaN away.
    _both(
        tmp_path,
        "module Main\nimport Std.IO\n"
        "fn main() -> I64 uses { Process, Log } = {\n"
        "  let zero = to_f64(List.len(Env.argv()))\n"
        "  println(to_str(zero / zero))\n  0\n}\n",
        0,
        out="nan\n",
        capfd=capfd,
    )


def test_libm_links_on_runtime_values(tmp_path: Path, capfd) -> None:
    # The native link was missing -lm: sqrt/sin/fmod of non-constant operands
    # failed at link time while the interpreter ran them.
    _both(
        tmp_path,
        "module Main\nimport Std.IO\nimport Std.Math\n"
        "fn main() -> I64 uses { Process, Log } = {\n"
        "  let zero = to_f64(List.len(Env.argv()))\n"
        "  println(to_str(sqrt(2.0 + zero)))\n"
        "  println(to_str((7.5 + zero) % 2.0))\n"
        "  println(to_str(sin(zero)))\n  0\n}\n",
        0,
        out="1.4142135623730951\n1.5\n0\n",
        capfd=capfd,
    )


def test_denormal_shortest_repr(tmp_path: Path, capfd) -> None:
    _both(
        tmp_path,
        "module Main\nimport Std.IO\n"
        "fn main() -> I64 uses { Log } = { println(to_str(5e-324))\n 0 }\n",
        0,
        out="5e-324\n",
        capfd=capfd,
    )


def test_record_eq_with_nan_field(tmp_path: Path) -> None:
    # Python container equality identity-shortcuts elements; the interpreter
    # now compares structurally, so a NaN field makes a record unequal to
    # ITSELF — exactly like the native field-wise cmpf.
    _both(
        tmp_path,
        "type P = { x: F64 }\n"
        "fn main() -> I64 uses { Process } = {\n"
        "  let zero = to_f64(List.len(Env.argv()))\n"
        "  let a = { x = zero / zero }\n"
        "  if a == a { 1 } else { 0 }\n}\n",
        0,
    )


def test_fn_values_in_inferred_list_literal_rejected() -> None:
    # TYPE025 fired only on annotated List<fn> — the inferred literal [d]
    # slipped through to an MLIR verifier error.
    diags = _diag("fn d(x: I64) -> I64 = { x }\nfn main() -> I64 = { let fs = [d]\n 0 }\n")
    assert any(d.code == "TYPE025" for d in diags)


def test_fn_return_type_rejected() -> None:
    diags = _diag(
        "fn d(x: I64) -> I64 = { x }\nfn pick() -> (I64) -> I64 = { d }\nfn main() -> I64 = { 0 }\n"
    )
    assert any(d.code == "TYPE025" and "return" in d.message for d in diags)


def test_builtin_as_value_gets_name003() -> None:
    diags = _diag("fn main() -> I64 = { let f = to_str\n 0 }\n")
    assert any(d.code == "NAME003" and "builtin" in d.message for d in diags)


def test_underscore_prefix_not_hex() -> None:
    # `0_x10` must not lex as a hex literal.
    diags = _diag("fn main() -> I64 = { 0_x10 }\n")
    assert any(d.code == "NAME001" for d in diags)


def test_comptime_bitwise(tmp_path: Path) -> None:
    _both(
        tmp_path,
        "fn main() -> I64 = { comptime { (0xF0 | 0x0F) & 0xFF ^ 0x0F } }\n",
        240,
    )


def test_negative_hex_int64_min_pattern(tmp_path: Path) -> None:
    _both(
        tmp_path,
        "type O = | G(I64) | N\n"
        "fn main() -> I64 = "
        "{ match G(-9223372036854775808) { G(-0x8000000000000000) => 1  _ => 0 } }\n",
        1,
    )


def test_std_generic_at_private_type(tmp_path: Path) -> None:
    # Monomorphizing Std.List.map at a caller-private type must not VIS001:
    # the caller already passed visibility at the call site.
    (tmp_path / "Helper.flx").write_text(
        "module Helper\nimport Std.List\n"
        "type Secret = { v: I64 }\n"
        "fn unwrap(s: Secret) -> I64 = { s.v }\n"
        "pub fn use_it() -> I64 = {\n"
        "  let xs = map([{ v = 6 }], unwrap)\n  xs[0]\n}\n",
        encoding="utf-8",
    )
    src = "module Main\nimport Std.List\nimport Helper\nfn main() -> I64 = { use_it() }\n"
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 6
    if _tools_available():
        assert driver.cmd_run(path, native=True) == 6
