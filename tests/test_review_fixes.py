"""Regression tests for bugs found by the adversarial MVP review."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.sema.check import check
from flx.syntax.dump import dump_module
from flx.syntax.parser import parse


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"

    def has(tool: str) -> bool:
        return bool(shutil.which(tool)) or os.path.exists(os.path.join(bindir, tool))

    return all(has(t) for t in ("mlir-opt", "mlir-translate", "clang"))


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


def _codes(src: str) -> list[str]:
    with pytest.raises(FlexError) as exc:
        check(parse(src))
    return [d.code for d in exc.value.diagnostics]


# --- checker / parser (always run) --------------------------------------------


def test_missing_return_value_is_rejected() -> None:
    assert "TYPE009" in _codes("fn f() -> I64 = { let x = 5 }")


def test_if_both_branches_return_is_accepted() -> None:
    check(parse("fn f(x: I64) -> I64 = { if x > 0 { return 1 } else { return 2 } }"))


def test_duplicate_param_is_rejected() -> None:
    assert "NAME002" in _codes("fn dup(a: I64, a: I64) -> I64 = { a }")


def test_assert_outside_test_is_rejected() -> None:
    assert "TEST001" in _codes("fn f() -> Unit = { assert(true) }")


def test_assert_inside_test_is_fine() -> None:
    check(parse('test "t" { assert(true) }'))


def test_out_of_range_int_literal_is_rejected() -> None:
    assert "TYPE011" in _codes("fn main() -> I64 = 99999999999999999999999999")


def test_max_i64_is_accepted() -> None:
    check(parse("fn main() -> I64 = 9223372036854775807"))


def test_deep_nesting_raises_clean_error_not_recursionerror() -> None:
    src = "fn main() -> I64 = " + "(" * 800 + "1" + ")" * 800
    with pytest.raises(FlexError):
        parse(src)


def test_statement_boundary_does_not_glue_paren_call() -> None:
    # `foo` then `(99)` on separate lines are two statements, not `foo(99)`.
    module = parse("fn main() -> I64 = {\n  let foo = 5\n  foo\n  (99)\n}")
    dump = dump_module(module)
    assert "foo(99)" not in dump
    assert "Expr 99" in dump


def test_same_line_call_still_parses() -> None:
    module = parse("fn id(x: I64) -> I64 = { x }\nfn f() -> I64 = { id(7) }")
    assert "id(7)" in dump_module(module)


# --- native codegen (skipped without toolchain) -------------------------------


def _run(tmp_path: Path, src: str) -> int:
    flx = tmp_path / "p.flx"
    flx.write_text(src, encoding="utf-8")
    return driver.cmd_run(str(flx))


@native
def test_and_short_circuits_avoiding_divide_by_zero(tmp_path: Path) -> None:
    # If `&&` evaluated the RHS eagerly, 100/0 would trap.
    src = "fn main() -> I64 = { if 0 != 0 && 100 / 0 > 5 { 1 } else { 7 } }"
    assert _run(tmp_path, src) == 7


@native
def test_or_short_circuits(tmp_path: Path) -> None:
    src = "fn main() -> I64 = { if 1 == 1 || 100 / 0 > 5 { 9 } else { 0 } }"
    assert _run(tmp_path, src) == 9


@native
def test_and_still_computes_correctly(tmp_path: Path) -> None:
    src = "fn main() -> I64 = { if 3 > 2 && 5 < 10 { 1 } else { 0 } }"
    assert _run(tmp_path, src) == 1


@native
def test_percent_in_test_name_is_safe(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    flx = tmp_path / "p.flx"
    flx.write_text('test "pct %s %d here" { assert(true) }', encoding="utf-8")
    rc = driver.cmd_test(str(flx))
    out = capfd.readouterr().out
    assert rc == 0
    assert "ok Main / pct %s %d here" in out


@native
def test_control_chars_in_test_name_still_builds(tmp_path: Path) -> None:
    flx = tmp_path / "p.flx"
    flx.write_text('test "tab\\tnewline\\nend" { assert(true) }', encoding="utf-8")
    assert driver.cmd_test(str(flx)) == 0  # builds and runs, no clang failure
