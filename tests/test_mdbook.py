"""Tests for the `flx-mdbook` Flex-highlighting preprocessor."""

from __future__ import annotations

import io
import json

import pytest

from flx.mdbook import highlight_markdown, main


def test_supports_html_only() -> None:
    assert main(["supports", "html"]) == 0
    assert main(["supports", "linkcheck"]) == 1


def test_highlight_markdown_replaces_flex_block() -> None:
    md = "intro\n\n```flex\nfn main() -> I64 = { 42 }\n```\n\noutro\n"
    out = highlight_markdown(md)
    assert "```flex" not in out
    assert 'class="flex-highlight"' in out
    assert "style=" in out  # inline (noclasses) colors
    assert "intro" in out and "outro" in out


def test_highlight_markdown_handles_flx_alias_and_skips_other_langs() -> None:
    md = "```flx\nlet x = 1\n```\n\n```sh\necho hi\n```\n"
    out = highlight_markdown(md)
    assert "flex-highlight" in out  # ```flx highlighted
    assert "```sh\necho hi\n```" in out  # other languages left untouched


def test_main_transforms_book_stream(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    book = {
        "items": [
            {
                "Chapter": {
                    "content": "```flex\nfn main() -> I64 = { 42 }\n```\n",
                    "sub_items": [
                        {"Chapter": {"content": "```flx\nlet y = 2\n```\n", "sub_items": []}}
                    ],
                }
            }
        ]
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([{"root": "."}, book])))

    assert main([]) == 0

    result = json.loads(capsys.readouterr().out)
    top = result["items"][0]["Chapter"]
    assert "flex-highlight" in top["content"]
    assert "flex-highlight" in top["sub_items"][0]["Chapter"]["content"]
