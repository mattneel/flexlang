"""Release preflight checks."""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

from flx import release
from flx.cli import main


def test_release_preflight_rejects_dirty_git_tree(tmp_path: Path, capsys) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    assert release.preflight(root=tmp_path, allow_dirty=False, build=False) == 1
    assert "working tree is not clean" in capsys.readouterr().err


def test_release_preflight_rejects_report_artifacts_in_sdist(tmp_path: Path) -> None:
    sdist = tmp_path / "flexlang-0.0.1.tar.gz"
    bad = tmp_path / "STUDY-local.md"
    bad.write_text("local notes\n", encoding="utf-8")
    with tarfile.open(sdist, "w:gz") as tar:
        tar.add(bad, arcname="flexlang-0.0.1/STUDY-local.md")

    assert release.forbidden_sdist_members(sdist) == ["flexlang-0.0.1/STUDY-local.md"]


def test_release_preflight_cli_dispatches(monkeypatch) -> None:
    called = {}

    def fake_preflight(*, allow_dirty: bool = False) -> int:
        called["allow_dirty"] = allow_dirty
        return 0

    monkeypatch.setattr(release, "cmd_preflight", fake_preflight)
    assert main(["release", "preflight", "--allow-dirty"]) == 0
    assert called == {"allow_dirty": True}
