"""Tests for runtime-backed string literals (Log output, fail messages)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"

    def has(tool: str) -> bool:
        return bool(shutil.which(tool)) or os.path.exists(os.path.join(bindir, tool))

    return all(has(t) for t in ("mlir-opt", "mlir-translate", "clang"))


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


@native
def test_log_info_prints(capfd: pytest.CaptureFixture[str]) -> None:
    rc = driver.cmd_run("examples/effects.flx")
    out = capfd.readouterr().out
    assert rc == 42
    assert "answer" in out


@native
def test_fail_prints_message(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    flx = tmp_path / "f.flx"
    flx.write_text('test "boom" { fail("not implemented yet") }', encoding="utf-8")
    rc = driver.cmd_test(str(flx))
    out = capfd.readouterr().out
    assert rc == 1
    assert "not implemented yet" in out
    assert "fail Main / boom" in out


@native
def test_string_with_escape(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    # The .flx string contains an escaped quote: a"b
    src = 'fn greet() -> Unit uses { Log } = { Log.info("a\\"b") }\n'
    src += 'test "t" uses { Log } { greet()\n assert(true) }'
    flx = tmp_path / "s.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_test(str(flx)) == 0
    assert 'a"b' in capfd.readouterr().out
