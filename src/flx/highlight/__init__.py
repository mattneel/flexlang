"""Syntax highlighting for Flex source.

Exposes :class:`~flx.highlight.lexer.FlexLexer` and a small :func:`render`
helper that turns Flex source into colorized terminal text or HTML. The lexer
is also registered as a Pygments plugin (see ``pyproject.toml``) so that
``pygmentize -l flex``, Rich, and Sphinx/mkdocs pick it up automatically.
"""

from __future__ import annotations

import os
from typing import Any

from flx.highlight.lexer import FlexLexer

__all__ = ["FlexLexer", "render"]

DEFAULT_STYLE = "monokai"

# Output formats accepted by `render` / `flx highlight`.
FORMATS = ("auto", "ansi", "ansi256", "truecolor", "html")


def _supports_truecolor() -> bool:
    return os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}


def _make_formatter(fmt: str, style: str, tty: bool) -> Any:
    from pygments.formatters import (
        HtmlFormatter,
        Terminal256Formatter,
        TerminalFormatter,
        TerminalTrueColorFormatter,
    )

    if fmt == "auto":
        if not tty:
            return None
        fmt = "truecolor" if _supports_truecolor() else "ansi256"

    if fmt == "ansi":
        return TerminalFormatter()
    if fmt == "ansi256":
        return Terminal256Formatter(style=style)
    if fmt == "truecolor":
        return TerminalTrueColorFormatter(style=style)
    if fmt == "html":
        return HtmlFormatter(style=style)
    raise ValueError(f"unknown highlight format: {fmt!r}")


def render(
    source: str,
    *,
    fmt: str = "auto",
    style: str = DEFAULT_STYLE,
    tty: bool = True,
) -> str:
    """Return ``source`` highlighted in the requested format.

    ``fmt="auto"`` returns colorized terminal text when ``tty`` is true
    (truecolor if ``$COLORTERM`` advertises it, otherwise 256-color) and the
    plain source otherwise, so piping stays clean.
    """
    formatter = _make_formatter(fmt, style, tty)
    if formatter is None:
        return source

    from pygments import highlight

    result: str = highlight(source, FlexLexer(), formatter)
    return result
