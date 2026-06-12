"""Dependency lockfile and vendoring workflow."""

from __future__ import annotations

import json
from pathlib import Path

from flx.cli import main

APP_MANIFEST = """module Package

fn manifest() -> Manifest = {
  {
    name = "app",
    version = "0.1.0",
    entry = "main.flx",
    dependencies = [ { name = "Mathlib", path = "../mathlib" } ]
  }
}
"""

LIB_MANIFEST = """module Package

fn manifest() -> Manifest = {
  { name = "mathlib", version = "0.2.0", entry = "Mathlib.flx", dependencies = [] }
}
"""


def _write(base: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src, encoding="utf-8")


def _two_packages(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        {
            "app/package.flx": APP_MANIFEST,
            "app/main.flx": "module Main\nimport Mathlib\nfn main() -> I64 = { square(7) }\n",
            "mathlib/package.flx": LIB_MANIFEST,
            "mathlib/Mathlib.flx": ("module Mathlib\npub fn square(n: I64) -> I64 = { n * n }\n"),
        },
    )
    return tmp_path / "app"


def test_deps_lock_and_verify_detects_path_tampering(tmp_path: Path, monkeypatch, capsys) -> None:
    app = _two_packages(tmp_path)
    monkeypatch.chdir(app)

    assert main(["deps", "lock"]) == 0
    lock = json.loads((app / "flex.lock").read_text(encoding="utf-8"))
    assert lock["version"] == 1
    assert lock["packages"][0]["dependency"] == "Mathlib"
    assert lock["packages"][0]["package"] == "mathlib"
    assert lock["packages"][0]["version"] == "0.2.0"
    assert len(lock["packages"][0]["sha256"]) == 64
    assert main(["deps", "verify"]) == 0

    (tmp_path / "mathlib/Mathlib.flx").write_text(
        "module Mathlib\npub fn square(n: I64) -> I64 = { n + n }\n", encoding="utf-8"
    )
    assert main(["deps", "verify"]) == 1
    assert "hash mismatch" in capsys.readouterr().err


def test_deps_vendor_supplies_locked_dependency_when_original_path_is_absent(
    tmp_path: Path, monkeypatch
) -> None:
    app = _two_packages(tmp_path)
    monkeypatch.chdir(app)

    assert main(["deps", "vendor"]) == 0
    assert (app / "vendor/Mathlib/Mathlib.flx").is_file()

    (tmp_path / "mathlib").rename(tmp_path / "mathlib-away")
    assert main(["run", "--interpret"]) == 49
