"""Tests for records (declaration, construction, field access, update)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.sema.check import check
from flx.syntax.parser import parse

POINT = "type Point = { x: I64, y: I64 }\n"


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


def test_record_example_checks() -> None:
    check(parse(Path("examples/records.flx").read_text(encoding="utf-8"), "examples/records.flx"))


def test_field_access_type() -> None:
    check(parse(POINT + "fn f(p: Point) -> I64 = { p.x }"))


def test_unknown_field_rejected() -> None:
    assert "TYPE015" in _codes(POINT + "fn f(p: Point) -> I64 = { p.z }")


def test_field_type_mismatch_rejected() -> None:
    assert "TYPE003" in _codes(POINT + "fn f() -> Point = { x = 1, y = true }")


def test_unknown_record_literal_rejected() -> None:
    assert "TYPE014" in _codes("fn f() -> I64 = { let r = { a = 1, b = 2 }\n 0 }")


def test_update_unknown_field_rejected() -> None:
    assert "TYPE015" in _codes(POINT + "fn f(p: Point) -> Point = { p with z = 1 }")


@native
def test_records_example_runs_and_tests() -> None:
    assert driver.cmd_run("examples/records.flx") == 142
    assert driver.cmd_test("examples/records.flx") == 0
