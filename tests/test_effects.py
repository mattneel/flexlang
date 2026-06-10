"""Tests for the effect checker (`uses { ... }`)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.sema.check import check
from flx.syntax.parser import parse

LG = 'fn lg() -> Unit uses { Log } = { Log.info("x") }\n'


def _codes(src: str) -> list[str]:
    with pytest.raises(FlexError) as exc:
        check(parse(src))
    return [d.code for d in exc.value.diagnostics]


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"

    def has(tool: str) -> bool:
        return bool(shutil.which(tool)) or os.path.exists(os.path.join(bindir, tool))

    return all(has(t) for t in ("mlir-opt", "mlir-translate", "clang"))


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


def test_effect_example_checks() -> None:
    check(parse(Path("examples/effects.flx").read_text(encoding="utf-8"), "examples/effects.flx"))


def test_missing_effect_on_intrinsic() -> None:
    assert "EFFECT001" in _codes('fn bad() -> Unit = { Log.info("x") }')


def test_pure_function_calling_effectful_fails() -> None:
    assert "EFFECT001" in _codes(LG + "fn p() -> I64 = { lg()\n 0 }")


def test_effectful_caller_accepts_callee_effects() -> None:
    check(parse(LG + "fn p() -> I64 uses { Log } = { lg()\n 0 }"))


def test_test_missing_effect_fails() -> None:
    assert "EFFECT001" in _codes(LG + 'test "t" { lg()\n assert(true) }')


def test_test_with_declared_effect_passes() -> None:
    check(parse(LG + 'test "t" uses { Log } { lg()\n assert(true) }'))


def test_unknown_intrinsic() -> None:
    assert "TYPE010" in _codes('fn f() -> Unit uses { Log } = { Log.bogus("x") }')


@native
def test_effects_example_runs_and_tests() -> None:
    assert driver.cmd_run("examples/effects.flx") == 42
    assert driver.cmd_test("examples/effects.flx") == 0
