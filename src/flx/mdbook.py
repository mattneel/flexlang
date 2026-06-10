"""An mdBook preprocessor that highlights Flex code blocks.

mdBook ships highlight.js, which doesn't know Flex, so ```flex / ```flx fenced
blocks would render uncolored. This preprocessor rewrites those blocks to
inline-styled HTML produced by the project's own Pygments lexer
(:class:`flx.highlight.lexer.FlexLexer`), keeping a single source of truth for
Flex highlighting across the terminal and the docs.

mdBook calls this program two ways (see the preprocessor protocol):

* ``flx-mdbook supports <renderer>`` -> exit 0 if the renderer is supported.
* ``flx-mdbook`` with ``[context, book]`` JSON on stdin -> the modified ``book``
  JSON on stdout.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

# Fenced ```flex / ```flx blocks (the spec uses ```flx, examples use ```flex).
_FENCE = re.compile(
    r"^```(?:flex|flx)\b[^\n]*\n(.*?)\n```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

# Self-contained dark card (monokai), so it needs no external CSS and renders
# the same under any mdBook theme.
_WRAP_STYLE = (
    "background:#272822;color:#f8f8f2;padding:1rem 1.1rem;"
    "border-radius:6px;overflow:auto;line-height:1.45"
)


def _highlight_block(match: re.Match[str]) -> str:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter

    from flx.highlight.lexer import FlexLexer

    inner = highlight(
        match.group(1),
        FlexLexer(),
        HtmlFormatter(noclasses=True, nowrap=True, style="monokai"),
    )
    return f'\n<pre class="flex-highlight" style="{_WRAP_STYLE}"><code>{inner}</code></pre>\n'


def highlight_markdown(content: str) -> str:
    """Replace Flex fenced code blocks in ``content`` with highlighted HTML."""
    return _FENCE.sub(_highlight_block, content)


def _walk(items: list[Any]) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        chapter = item.get("Chapter")
        if not isinstance(chapter, dict):
            continue
        content = chapter.get("content")
        if isinstance(content, str):
            chapter["content"] = highlight_markdown(content)
        sub_items = chapter.get("sub_items")
        if isinstance(sub_items, list):
            _walk(sub_items)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)

    # `supports <renderer>`: we only rewrite HTML output.
    if args and args[0] == "supports":
        renderer = args[1] if len(args) > 1 else ""
        return 0 if renderer == "html" else 1

    _context, book = json.load(sys.stdin)
    # mdBook 0.4+ uses "items"; older versions used "sections".
    items = book.get("items")
    if not isinstance(items, list):
        items = book.get("sections")
    if isinstance(items, list):
        _walk(items)
    json.dump(book, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
