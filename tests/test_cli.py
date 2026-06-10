"""Smoke tests for the Flex CLI scaffold."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flx import __version__
from flx.cli import main


def _toolchain_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"
    return all(
        bool(shutil.which(t)) or os.path.exists(os.path.join(bindir, t))
        for t in ("mlir-opt", "mlir-translate", "clang")
    )


native = pytest.mark.skipif(not _toolchain_available(), reason="LLVM/MLIR toolchain not available")


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help() -> None:
    assert main([]) == 0


def test_stub_command_returns_nonzero() -> None:
    # `emit-hir` is still a scaffolded stub (HIR is not implemented yet).
    assert main(["emit-hir", "examples/add.flx"]) == 2


def test_module_entrypoint() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "flx", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert __version__ in result.stdout


def test_doctor_runs() -> None:
    # `doctor` is pure-Python; exit code reflects toolchain presence, but it must
    # never raise or hang.
    assert main(["doctor"]) in (0, 1)


@native
def test_build_produces_runnable_binary(tmp_path: Path) -> None:
    out = tmp_path / "addbin"
    assert main(["build", "examples/add.flx", "-o", str(out)]) == 0
    assert out.exists()
    assert subprocess.run([str(out)]).returncode == 42
