"""Smoke tests for the Flex CLI scaffold."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
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


def test_test_command_discovers_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "one.flx").write_text(
        'module One\ntest "one passes" { assert(true) }\n', encoding="utf-8"
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "two.flx").write_text(
        'module Two\ntest "two passes" { assert(true) }\n', encoding="utf-8"
    )
    (tmp_path / "package.flx").write_text(
        """module Package

fn manifest() -> Manifest = {
  { name = "test-dir", version = "0.1.0", entry = "one.flx", dependencies = [] }
}
""",
        encoding="utf-8",
    )
    (tmp_path / "build.flx").write_text("not a test entry\n", encoding="utf-8")

    assert main(["test", str(tmp_path), "--interpret"]) == 0
    out = capsys.readouterr().out
    assert "ok One / one passes" in out
    assert "ok Two / two passes" in out


def test_test_command_json_format(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "tests.flx"
    path.write_text(
        'module Main\ntest "pass" { assert(true) }\ntest "fail" { assert_eq(1, 2) }\n',
        encoding="utf-8",
    )

    assert main(["test", str(path), "--interpret", "--format", "json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"] == {"total": 2, "passed": 1, "failed": 1}
    assert payload["tests"][0]["label"] == "Main / pass"
    assert payload["tests"][0]["status"] == "passed"
    assert payload["tests"][1]["label"] == "Main / fail"
    assert payload["tests"][1]["status"] == "failed"
    assert "assert_eq failed" in payload["tests"][1]["message"]


def test_test_command_junit_format(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "tests.flx"
    path.write_text(
        'module Main\ntest "pass" { assert(true) }\ntest "fail" { assert_eq(1, 2) }\n',
        encoding="utf-8",
    )

    assert main(["test", str(path), "--interpret", "--format", "junit"]) == 1
    root = ET.fromstring(capsys.readouterr().out)
    assert root.tag == "testsuite"
    assert root.attrib["tests"] == "2"
    assert root.attrib["failures"] == "1"
    cases = root.findall("testcase")
    assert [case.attrib["name"] for case in cases] == ["pass", "fail"]
    failure = cases[1].find("failure")
    assert failure is not None
    assert "assert_eq failed" in (failure.text or "")


@native
def test_build_produces_runnable_binary(tmp_path: Path) -> None:
    out = tmp_path / "addbin"
    assert main(["build", "examples/add.flx", "-o", str(out)]) == 0
    assert out.exists()
    assert subprocess.run([str(out)]).returncode == 42
