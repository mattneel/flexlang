"""Tests for region blocks (shallow MVP)."""

from __future__ import annotations

import os
import shutil

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.sema.check import check
from flx.syntax import ast
from flx.syntax.parser import parse


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"

    def has(tool: str) -> bool:
        return bool(shutil.which(tool)) or os.path.exists(os.path.join(bindir, tool))

    return all(has(t) for t in ("mlir-opt", "mlir-translate", "clang"))


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


def test_region_value_typechecks() -> None:
    module = parse("fn f() -> I64 = { region scratch { 40 + 2 } }")
    result = check(module)
    region = module.functions[0].body.tail
    assert isinstance(region, ast.RegionExpr)
    # the region expression's type is its body's value type (I64)
    assert str(result.expr_types[id(region)]) == "I64"


def test_region_name_is_scoped_to_body() -> None:
    # `scratch` is not visible outside the region body.
    with pytest.raises(FlexError):
        check(parse("fn f() -> Region = { region scratch { 0 }\n scratch }"))


@native
def test_region_example_runs_and_tests() -> None:
    assert driver.cmd_run("examples/regions.flx") == 42
    assert driver.cmd_test("examples/regions.flx") == 0
