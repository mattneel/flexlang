"""Milestone 7: float round-trip — parse_float (strict grammar, libc strtod on
both backends), to_str_fixed (%.*f), repeat/pad_left/pad_right, and the
float-aware ADT equality that assert_eq on Option<F64> needs. Differential:
both backends byte-identical."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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


def _test_cmd(path: str, backend: str = "interp") -> tuple[int, bytes]:
    args = [sys.executable, "-m", "flx", "test", path]
    if backend == "native":
        args.insert(4, "--native")
    proc = subprocess.run(args, capture_output=True)
    return proc.returncode, proc.stdout


def _run(path: str, backend: str = "interp") -> tuple[int, bytes, bytes]:
    args = [sys.executable, "-m", "flx", "run", path]
    if backend == "native":
        args.insert(4, "--native")
    proc = subprocess.run(args, capture_output=True)
    return proc.returncode, proc.stdout, proc.stderr


# --- parse_float -------------------------------------------------------------------------

GRAMMAR_TESTS = """\
module Main
import Std.Str

test "accepts the grammar" {
  assert_eq(parse_float("1.5"), Some(1.5))
  assert_eq(parse_float("-1.5"), Some(-1.5))
  assert_eq(parse_float("42"), Some(42.0))
  assert_eq(parse_float("-0"), Some(0.0))
  assert_eq(parse_float("1e9"), Some(1000000000.0))
  assert_eq(parse_float("2.5E-3"), Some(0.0025))
  assert_eq(parse_float("1e+3"), Some(1000.0))
  assert_eq(parse_float("0.000001"), Some(1.0e-6))
}

test "rejects everything else" {
  assert_eq(parse_float(""), None)
  assert_eq(parse_float("-"), None)
  assert_eq(parse_float("1."), None)
  assert_eq(parse_float(".5"), None)
  assert_eq(parse_float("+1"), None)
  assert_eq(parse_float("1e"), None)
  assert_eq(parse_float("1e+"), None)
  assert_eq(parse_float(" 1"), None)
  assert_eq(parse_float("1 "), None)
  assert_eq(parse_float("1.5x"), None)
  assert_eq(parse_float("1.5.5"), None)
  assert_eq(parse_float("0x10"), None)
  assert_eq(parse_float("Infinity"), None)
  assert_eq(parse_float("NAN"), None)
  assert_eq(parse_float("--1"), None)
  assert_eq(parse_float("1e5x"), None)
}

test "hard conversion cases are bit-exact" {
  assert_eq(parse_float("0.1"), Some(0.1))
  assert_eq(parse_float("5e-324"), Some(5.0e-324))
  assert_eq(parse_float("2.2250738585072011e-308"), Some(2.2250738585072011e-308))
  assert_eq(parse_float("1.7976931348623157e308"), Some(1.7976931348623157e308))
  assert_eq(parse_float("9007199254740993"), Some(9007199254740992.0))
}

test "overflow saturates and special spellings parse" {
  assert_eq(parse_float("1e999"), Some(1.0 / 0.0))
  assert_eq(parse_float("-1e999"), Some(-1.0 / 0.0))
  assert_eq(parse_float("1e-999"), Some(0.0))
  assert_eq(parse_float("inf"), Some(1.0 / 0.0))
  assert_eq(parse_float("-inf"), Some(-1.0 / 0.0))
  match parse_float("nan") {
    Some(x) => { assert(x != x) }
    None => { fail("nan did not parse") }
  }
}

test "round-trips to_str" {
  assert_eq(parse_float(to_str(0.1)), Some(0.1))
  assert_eq(parse_float(to_str(1.0 / 3.0)), Some(1.0 / 3.0))
  assert_eq(parse_float(to_str(6.02214076e23)), Some(6.02214076e23))
  assert_eq(parse_float(to_str(5.0e-324)), Some(5.0e-324))
  assert_eq(parse_float(to_str(-0.0)), Some(-0.0))
}
"""


def test_parse_float_grammar_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, GRAMMAR_TESTS)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_parse_float_grammar_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, GRAMMAR_TESTS)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


# --- to_str_fixed ------------------------------------------------------------------------

FIXED_TESTS = """\
module Main
import Std.Str

test "fixed-point basics" {
  assert_eq(to_str_fixed(3.14159, 2), "3.14")
  assert_eq(to_str_fixed(2.0, 0), "2")
  assert_eq(to_str_fixed(1.0, 3), "1.000")
  assert_eq(to_str_fixed(-0.5, 1), "-0.5")
  assert_eq(to_str_fixed(-0.0, 2), "-0.00")
  assert_eq(to_str_fixed(0.5, 0), "0")
  assert_eq(to_str_fixed(1.5, 0), "2")
}

test "correct rounding at the decimal" {
  assert_eq(to_str_fixed(2.675, 2), "2.67")
  assert_eq(to_str_fixed(0.125, 2), "0.12")
  assert_eq(to_str_fixed(0.375, 2), "0.38")
}

test "specials and extremes" {
  assert_eq(to_str_fixed(1.0 / 0.0, 2), "inf")
  assert_eq(to_str_fixed(-1.0 / 0.0, 2), "-inf")
  assert_eq(to_str_fixed(0.0 / 0.0, 2), "nan")
  assert_eq(length(to_str_fixed(1.0e308, 0)), 309)
  assert_eq(length(to_str_fixed(5.0e-324, 100)), 102)
  assert_eq(to_str_fixed(0.1, 17), "0.10000000000000001")
}
"""


def test_to_str_fixed_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, FIXED_TESTS)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_to_str_fixed_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, FIXED_TESTS)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


def test_to_str_fixed_panic_message(tmp_path: Path) -> None:
    src = "module Main\nimport Std.Str\nfn main() -> I64 = { length(to_str_fixed(1.0, 101)) }\n"
    path = _write(tmp_path, src)
    code, _, err = _run(path)
    assert code == 1
    assert b"flx: runtime error: decimals 101 is outside 0..100" in err


@native
def test_to_str_fixed_panic_parity(tmp_path: Path) -> None:
    for d in ("-1", "101"):
        src = (
            "module Main\nimport Std.Str\n"
            f"fn main() -> I64 = {{ length(to_str_fixed(1.0, {d})) }}\n"
        )
        path = _write(tmp_path, src)
        assert _run(path, "interp") == _run(path, "native")


# --- the raw strtod intrinsic is total under misuse ---------------------------------------


@native
def test_raw_parse_f64_prefix_semantics_parity(tmp_path: Path) -> None:
    # Direct Str.parse_f64 misuse: strtod prefix semantics, identical bytes.
    src = (
        "module Main\nimport Std.IO\nimport Std.Str\n"
        "fn main() -> I64 uses { Log } = {\n"
        '  println(to_str(Str.parse_f64("xyz")))\n'
        '  println(to_str(Str.parse_f64("1.5junk")))\n'
        '  println(to_str(Str.parse_f64("  2.5")))\n'
        "  0\n}\n"
    )
    path = _write(tmp_path, src)
    interp = _run(path, "interp")
    nat = _run(path, "native")
    assert interp == nat
    assert interp[1] == b"0\n1.5\n2.5\n"


# --- padding -------------------------------------------------------------------------------

PAD_TESTS = """\
module Main
import Std.Str

test "repeat" {
  assert_eq(repeat("ab", 3), "ababab")
  assert_eq(repeat("", 5), "")
  assert_eq(repeat("x", 0), "")
  assert_eq(repeat("x", -1), "")
}

test "pads count bytes" {
  assert_eq(pad_left("7", 3), "  7")
  assert_eq(pad_right("7", 3), "7  ")
  assert_eq(pad_left("123", 3), "123")
  assert_eq(pad_left("1234", 3), "1234")
  assert_eq(pad_left("é", 3), " é")
  assert_eq(pad_left("", 2), "  ")
}

test "a numeric column" {
  assert_eq(pad_left(to_str_fixed(9.5, 2), 8), "    9.50")
  assert_eq(pad_left(to_str_fixed(-273.15, 2), 8), " -273.15")
}
"""


def test_padding_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, PAD_TESTS)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_padding_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, PAD_TESTS)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


# --- float-aware ADT equality ----------------------------------------------------------------

ADT_EQ_TESTS = """\
module Main
import Std.Str

type Reading = | Missing | Value(F64)

test "option f64 equality is float equality" {
  assert_eq(Some(0.0), Some(-0.0))
  assert(Some(0.0 / 0.0) != Some(0.0 / 0.0))
  assert_eq(Value(2.5), Value(2.5))
  assert(Value(0.0 / 0.0) != Value(0.0 / 0.0))
  assert(Some(1.5) != None)
  assert_eq(parse_float("2.5"), Some(2.5))
}
"""


def test_adt_float_equality_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, ADT_EQ_TESTS)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_adt_float_equality_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, ADT_EQ_TESTS)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


# --- hints updated -----------------------------------------------------------------------------


def test_parse_float_hint_is_an_import_now() -> None:
    with pytest.raises(FlexError) as exc:
        check_and_monomorphize(
            expand(parse('fn main() -> I64 = { let x = parse_float("1.5")\n 0 }'))
        )
    diags = exc.value.diagnostics
    assert any("`import Std.Str` provides parse_float" in (d.help or "") for d in diags)
    assert not any("does not exist yet" in (d.help or "") for d in diags)
