"""Tests for comptime, macros, hygiene, reflect, and derive."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.macro import expand
from flx.sema.check import check
from flx.syntax.dump import dump_module
from flx.syntax.parser import parse


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"

    def has(tool: str) -> bool:
        return bool(shutil.which(tool)) or os.path.exists(os.path.join(bindir, tool))

    return all(has(t) for t in ("mlir-opt", "mlir-translate", "clang"))


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


def _expand(src: str) -> str:
    return dump_module(expand(parse(src)))


def _codes(src: str) -> list[str]:
    with pytest.raises(FlexError) as exc:
        check(expand(parse(src)))
    return [d.code for d in exc.value.diagnostics]


# --- comptime (always runs) ---------------------------------------------------


def test_comptime_folds_arithmetic() -> None:
    assert "Expr 14" in _expand("fn f() -> I64 = { comptime { 2 + 3 * 4 } }")


def test_comptime_folds_bool_and_string() -> None:
    assert "Expr true" in _expand("fn f() -> Bool = { comptime { 3 < 5 } }")
    assert "Expr 'ab'" in _expand('fn f() -> String = { comptime { "a" ++ "b" } }')


def test_comptime_calls_pure_function() -> None:
    src = "fn sq(n: I64) -> I64 = { n * n }\nfn f() -> I64 = { comptime { sq(3) + 1 } }"
    assert "Expr 10" in _expand(src)


def test_comptime_div_by_zero() -> None:
    assert "CT003" in _codes("fn f() -> I64 = { comptime { 1 / 0 } }")


def test_comptime_free_name() -> None:
    assert "CT002" in _codes("fn f(x: I64) -> I64 = { comptime { x } }")


# --- macros (always runs) -----------------------------------------------------


def test_expression_macro_expands() -> None:
    src = "macro square(x) = quote { unquote(x) * unquote(x) }\nfn f() -> I64 = { square(n + 1) }"
    assert "Expr ((n + 1) * (n + 1))" in _expand(src)


def test_macro_arity_error() -> None:
    src = "macro m(x) = quote { unquote(x) }\nfn f() -> I64 = { m(1, 2) }"
    assert "MAC001" in _codes(src)


def test_quote_outside_macro_rejected() -> None:
    assert "MAC004" in _codes("fn f() -> I64 = { quote { 1 } }")


def test_nested_macro_expands() -> None:
    src = (
        "macro inc(x) = quote { unquote(x) + 1 }\n"
        "macro inc2(x) = quote { inc(inc(unquote(x))) }\n"
        "fn f() -> I64 = { inc2(0) }"
    )
    assert "((0 + 1) + 1)" in _expand(src)


def test_hygiene_renames_introduced_binder() -> None:
    src = (
        "macro emit(x) = quote { let tmp = unquote(x)\n total = total + tmp }\n"
        "fn f() -> I64 = { mut total = 0\n let tmp = 5\n emit(tmp)\n total }"
    )
    dump = _expand(src)
    assert "tmp$1" in dump  # introduced binder gensym'd
    assert "Assign total = (total + tmp$1)" in dump


# --- reflect + comptime for + unquote_splice ----------------------------------


def test_reflect_for_splice_generates_per_field() -> None:
    src = (
        "type Point = { x: I64, y: I64 }\n"
        "macro names() = quote {\n"
        "  unquote_splice(comptime { for f in reflect.fields(Point) {\n"
        "    quote { Log.info(unquote(f.name)) }\n  } })\n}\n"
        "fn describe() -> Unit uses { Log } = { names() }"
    )
    dump = _expand(src)
    assert "Log.info('x')" in dump
    assert "Log.info('y')" in dump


# --- derive (check + native) --------------------------------------------------


def test_derive_eq_show_generates_functions() -> None:
    dump = _expand("derive(Eq, Show) type P = { x: I64 }")
    assert "Fn eq_P" in dump
    assert "Fn show_P" in dump


def test_derive_generic_rejected() -> None:
    assert "DER004" in _codes("derive(Eq) type Box<T> = { value: T }")


@native
def test_macros_example_runs_and_tests() -> None:
    assert driver.cmd_run("examples/macros.flx") == 67  # 42 + 25
    assert driver.cmd_test("examples/macros.flx") == 0


@native
def test_derive_show_prints(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    src = (
        "derive(Show) type Point = { x: I64, y: I64 }\n"
        'test "t" uses { Log } { Log.info(show_Point({ x = 1, y = 2 }))\n assert(true) }'
    )
    flx = tmp_path / "s.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_test(str(flx)) == 0
    assert "Point { x = 1, y = 2 }" in capfd.readouterr().out


@native
def test_hygiene_runs_correctly(tmp_path: Path) -> None:
    src = (
        "macro emit(x) = quote { let tmp = unquote(x)\n total = total + tmp }\n"
        "fn f() -> I64 = { mut total = 0\n let tmp = 5\n emit(tmp)\n total }\n"
        "fn main() -> I64 = { f() }"
    )
    flx = tmp_path / "h.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_run(str(flx)) == 5
