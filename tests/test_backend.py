"""Tests for MLIR emission and native execution.

Native build+run tests are skipped where the LLVM/MLIR 22 toolchain is absent
(e.g. CI without the system install); the text-emission tests always run.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver
from flx.backend.mlir import emit_module
from flx.sema.check import check
from flx.syntax.parser import parse


def _emit(src: str) -> str:
    return emit_module(check(parse(src)))


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"

    def has(tool: str) -> bool:
        return bool(shutil.which(tool)) or os.path.exists(os.path.join(bindir, tool))

    return all(has(t) for t in ("mlir-opt", "mlir-translate", "clang"))


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


# --- text emission (always runs) ----------------------------------------------


def test_emit_arithmetic_and_call() -> None:
    out = _emit(Path("examples/add.flx").read_text(encoding="utf-8"))
    assert "func.func @flx_add(%arg0: i64, %arg1: i64) -> i64" in out
    assert "arith.addi %arg0, %arg1 : i64" in out
    assert "func.call @flx_add(" in out


def test_emit_while_uses_memref_and_cf() -> None:
    out = _emit("fn f(n: I64) -> I64 = { mut t = 0\n while t < n { t = t + 1 }\n t }")
    assert "memref.alloca() : memref<i64>" in out
    assert "cf.cond_br" in out
    assert "arith.cmpi slt" in out


def test_emit_if_uses_branches() -> None:
    out = _emit("fn f(x: I64) -> I64 = { if x < 0 { 0 - x } else { x } }")
    assert "cf.cond_br" in out
    assert "memref.load" in out  # result slot


# --- native execution (skipped without toolchain) -----------------------------


def _run_source(tmp_path: Path, src: str) -> int:
    flx = tmp_path / "prog.flx"
    flx.write_text(src, encoding="utf-8")
    return driver.cmd_run(str(flx))


@native
def test_run_hello_exit_code(tmp_path: Path) -> None:
    assert _run_source(tmp_path, "module Main\nfn main() -> I64 = { 42 }") == 42


@native
def test_run_add_example() -> None:
    assert driver.cmd_run("examples/add.flx") == 42


@native
def test_run_while_loop(tmp_path: Path) -> None:
    src = (
        "module Main\n"
        "fn sum_to(n: I64) -> I64 = {\n"
        "  mut i = 0\n  mut total = 0\n"
        "  while i <= n { total = total + i\n i = i + 1 }\n  total\n}\n"
        "fn main() -> I64 = { sum_to(10) }"
    )
    assert _run_source(tmp_path, src) == 55


@native
def test_run_if_else(tmp_path: Path) -> None:
    src = (
        "module Main\n"
        "fn abs(x: I64) -> I64 = { if x < 0 { 0 - x } else { x } }\n"
        "fn main() -> I64 = { abs(0 - 17) }"
    )
    assert _run_source(tmp_path, src) == 17
