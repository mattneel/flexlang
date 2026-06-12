"""Repository hardening checks for release, CI, and CLI contracts."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from flx import __version__, docsengine
from flx.cli import main

ROOT = Path(__file__).resolve().parents[1]


def _toml(path: str) -> dict:
    return tomllib.loads((ROOT / path).read_text(encoding="utf-8"))


def test_docs_build_requires_mdbook_for_repo_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    docs = tmp_path / "docs"
    shutil.copytree(ROOT / "docs", docs)
    monkeypatch.setattr(docsengine, "_mdbook_available", lambda: False)

    assert docsengine.cmd_docs_build(check_only=False, docs_dir=docs) == 1
    assert "mdbook is required" in capsys.readouterr().err


def test_test_format_rejects_structured_output_on_native_backend(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["test", "--native", "--format", "json", "examples/add.flx"]) == 2
    assert "only the interpreter supports structured output" in capsys.readouterr().err


def test_version_is_read_from_package_metadata() -> None:
    init_source = (ROOT / "src/flx/__init__.py").read_text(encoding="utf-8")

    assert __version__
    assert '"0.0.1"' not in init_source


def test_optimized_python_mode_is_rejected() -> None:
    proc = subprocess.run(
        [sys.executable, "-O", "-m", "flx", "--version"],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1
    assert "Python optimized mode (-O) is not supported" in proc.stderr


def test_stale_llvmlite_extra_is_not_advertised() -> None:
    project = _toml("pyproject.toml")["project"]

    assert "llvmlite" not in project.get("optional-dependencies", {})


def test_sdist_has_explicit_release_boundary() -> None:
    hatch = _toml("pyproject.toml")["tool"]["hatch"]["build"]["targets"]
    sdist = hatch["sdist"]

    assert "README.md" in sdist["include"]
    assert "src/flx/**" in sdist["include"]
    assert "tests/**" in sdist["include"]
    assert "STUDY-*.md" in sdist["exclude"]
    assert "ADVERSARIAL_REVIEW.md" in sdist["exclude"]


def test_wheel_exposes_examples_for_source_blind_docs_kits() -> None:
    hatch = _toml("pyproject.toml")["tool"]["hatch"]["build"]["targets"]
    wheel = hatch["wheel"]

    assert wheel["force-include"]["examples"] == "examples"


def test_mise_docs_task_regenerates_generated_pages() -> None:
    mise = _toml("mise.toml")

    assert mise["tasks"]["docs"]["run"] == "uv run flx docs build"


def test_github_actions_are_pinned_to_commit_shas() -> None:
    workflow_dir = ROOT / ".github/workflows"
    uses_lines = [
        line.strip()
        for workflow in sorted(workflow_dir.glob("*.yml"))
        for line in workflow.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("uses:")
    ]

    assert uses_lines
    assert all(re.search(r"@[0-9a-f]{40}(?:\s|$)", line) for line in uses_lines)


def test_llvm_installer_verifies_apt_key_fingerprint() -> None:
    script = (ROOT / "scripts/install-llvm.sh").read_text(encoding="utf-8")

    assert "6084 F3CF 814B 57C1 CF12  EFD5 15CF 4D18 AF4F 7421" in script
    assert "gpg --show-keys --with-fingerprint" in script


def test_docs_state_trust_model_and_hex_like_package_direction() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    packages = (ROOT / "docs/packages.md").read_text(encoding="utf-8")

    assert "Flex is not a sandbox" in readme
    assert "`flx run`, `flx test`, `flx build`, and `flx docs check` execute trusted code" in readme
    assert "immutable published versions" in packages
    assert "signed registry metadata" in packages
    assert "lockfiles that pin exact package versions and content hashes" in packages
    assert "first-class vendor workflow" in packages
