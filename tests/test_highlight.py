"""Tests for the Flex Pygments lexer and `flx highlight`."""

from __future__ import annotations

import pytest
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.token import Comment, Error, Keyword, Name, Operator

from flx.cli import main
from flx.highlight import render
from flx.highlight.lexer import FlexLexer

SAMPLE = """\
module Main

fn add(a: I64, b: I64) -> I64 =
{
  // returns the sum
  a + b
}

test "add works" uses { Log } {
  assert_eq(add(20, 22), 42)
}
"""


def _tokens(src: str) -> list[tuple[object, str]]:
    return list(FlexLexer().get_tokens(src))


def test_no_error_tokens() -> None:
    assert not any(ttype is Error for ttype, _ in _tokens(SAMPLE))


def test_classification() -> None:
    toks = _tokens(SAMPLE)
    assert (Keyword.Declaration, "fn") in toks
    assert (Keyword.Type, "I64") in toks
    assert (Name.Builtin, "assert_eq") in toks
    assert (Name.Builtin, "Log") in toks  # effect name inside `uses { ... }`
    assert (Name.Function, "add") in toks  # `add(` is a call site
    assert any(ttype in Operator and val == "->" for ttype, val in toks)
    assert any(ttype in Comment and "returns the sum" in val for ttype, val in toks)


def test_entry_point_registered() -> None:
    # `flex` is our canonical alias; `.flx` files resolve to Flex (not the
    # built-in Felix lexer that also claims the extension) via our priority.
    assert isinstance(get_lexer_by_name("flex"), FlexLexer)
    assert isinstance(get_lexer_for_filename("example.flx"), FlexLexer)


def test_render_truecolor_emits_ansi() -> None:
    out = render(SAMPLE, fmt="truecolor", tty=True)
    assert "\x1b[" in out


def test_render_auto_non_tty_is_plain() -> None:
    assert render(SAMPLE, fmt="auto", tty=False) == SAMPLE


def test_highlight_command_html(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["highlight", "--format", "html", "examples/hello.flx"]) == 0
    assert "highlight" in capsys.readouterr().out  # HtmlFormatter wraps in div.highlight
