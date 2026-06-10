"""Smoke tests for the Flex CLI scaffold."""

from __future__ import annotations

import subprocess
import sys

import pytest

from flx import __version__
from flx.cli import main


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help() -> None:
    assert main([]) == 0


def test_stub_command_returns_nonzero() -> None:
    assert main(["check", "examples/add.flx"]) == 2


def test_module_entrypoint() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "flx", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert __version__ in result.stdout
