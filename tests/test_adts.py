"""Tests for ADTs, Result/Option, match, and the `?` operator."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.sema.check import check
from flx.syntax.parser import parse

ENUM = """\
module Main
type Color = | Red | Green | Blue
fn code(c: Color) -> I64 =
{
  match c {
    Red => 1
    Green => 2
    Blue => 3
  }
}
fn main() -> I64 = { code(Green) + code(Blue) }
test "colors" {
  assert_eq(code(Red), 1)
  assert_eq(code(Blue), 3)
}
"""

OPTION = """\
module Main
fn first(x: I64) -> Option<I64> = { if x > 0 { Some(x) } else { None } }
fn unwrap_or(o: Option<I64>, d: I64) -> I64 =
{
  match o {
    Some(v) => v
    None => d
  }
}
fn main() -> I64 = { unwrap_or(first(7), 0) + unwrap_or(first(0 - 1), 99) }
"""


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


def test_result_example_checks() -> None:
    check(parse(Path("examples/result.flx").read_text(encoding="utf-8"), "examples/result.flx"))


def test_nonexhaustive_match_rejected() -> None:
    src = "type C = | A | B\nfn f(c: C) -> I64 = { match c { A => 1 } }"
    assert "MATCH001" in _codes(src)


def test_wildcard_makes_match_exhaustive() -> None:
    check(parse("type C = | A | B\nfn f(c: C) -> I64 = { match c { A => 1\n _ => 0 } }"))


def test_unknown_variant_pattern_rejected() -> None:
    src = "type C = | A | B\nfn f(c: C) -> I64 = { match c { A => 1\n Zed => 0 } }"
    assert "MATCH003" in _codes(src)


def test_question_outside_result_rejected() -> None:
    src = "fn g() -> Result<I64, I64> = { Ok(1) }\nfn f() -> I64 = { g()? }"
    assert "QUEST001" in _codes(src)


def test_match_arm_type_mismatch_rejected() -> None:
    src = "type C = | A | B\nfn f(c: C) -> I64 = { match c { A => 1\n B => true } }"
    assert "TYPE008" in _codes(src)


@native
def test_result_example_runs_and_tests() -> None:
    assert driver.cmd_run("examples/result.flx") == 5
    assert driver.cmd_test("examples/result.flx") == 0


@native
def test_enum_runs_and_tests(tmp_path: Path) -> None:
    flx = tmp_path / "enum.flx"
    flx.write_text(ENUM, encoding="utf-8")
    assert driver.cmd_run(str(flx)) == 5
    assert driver.cmd_test(str(flx)) == 0


@native
def test_option_runs(tmp_path: Path) -> None:
    flx = tmp_path / "opt.flx"
    flx.write_text(OPTION, encoding="utf-8")
    assert driver.cmd_run(str(flx)) == 106


@native
def test_failing_question_test_reports(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    src = (
        "fn div(a: I64, b: I64) -> Result<I64, I64> = "
        "{ if b == 0 { Err(1) } else { Ok(a / b) } }\n"
        'test "q" { let x = div(1, 0)?\n assert_eq(x, 0) }'
    )
    flx = tmp_path / "q.flx"
    flx.write_text(src, encoding="utf-8")
    rc = driver.cmd_test(str(flx))
    assert rc == 1
    assert "1 failed" in capfd.readouterr().out
