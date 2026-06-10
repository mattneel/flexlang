"""Tests for the Flex lexer."""

from __future__ import annotations

import pytest

from flx.diagnostics import FlexError
from flx.syntax.lexer import tokenize
from flx.syntax.tokens import TokenKind


def _kinds(src: str) -> list[TokenKind]:
    return [t.kind for t in tokenize(src)]


def test_keywords_and_idents() -> None:
    toks = tokenize("fn add let mut x")
    assert [t.kind for t in toks] == [
        TokenKind.KW_FN,
        TokenKind.IDENT,
        TokenKind.KW_LET,
        TokenKind.KW_MUT,
        TokenKind.IDENT,
        TokenKind.EOF,
    ]


def test_integers_with_underscores() -> None:
    toks = tokenize("1_000 42")
    assert toks[0].kind is TokenKind.INT and toks[0].text == "1000"
    assert toks[1].text == "42"


def test_multichar_operators() -> None:
    assert _kinds("-> => |> == != <= >= && ||")[:-1] == [
        TokenKind.ARROW,
        TokenKind.FAT_ARROW,
        TokenKind.PIPE_GT,
        TokenKind.EQ_EQ,
        TokenKind.BANG_EQ,
        TokenKind.LE,
        TokenKind.GE,
        TokenKind.AMP_AMP,
        TokenKind.PIPE_PIPE,
    ]


def test_string_with_escapes() -> None:
    toks = tokenize(r'"add\twork\"s"')
    assert toks[0].kind is TokenKind.STRING
    assert toks[0].text == 'add\twork"s'


def test_line_comment_skipped() -> None:
    toks = tokenize("let x = 1 // a comment\nlet y = 2")
    assert TokenKind.KW_LET in _kinds("let x = 1 // c\n")
    assert all("comment" not in t.text for t in toks)


def test_spans_track_line_and_col() -> None:
    toks = tokenize("fn\n  add")
    add = toks[1]
    assert add.text == "add"
    assert add.span.start.line == 2
    assert add.span.start.col == 3


def test_unterminated_string_errors() -> None:
    with pytest.raises(FlexError):
        tokenize('"oops')


def test_unknown_char_errors() -> None:
    with pytest.raises(FlexError):
        tokenize("let x = @")
