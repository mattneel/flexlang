"""Smoke checks for the bundled VS Code extension scaffold."""

from __future__ import annotations

import json
from pathlib import Path


def test_vscode_extension_declares_flex_language_and_lsp() -> None:
    root = Path("editors/vscode")
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    language = package["contributes"]["languages"][0]
    assert language["id"] == "flex"
    assert ".flx" in language["extensions"]
    extension = (root / "extension.js").read_text(encoding="utf-8")
    assert 'args: ["lsp"]' in extension
    assert "vscode-languageclient" in package["dependencies"]
