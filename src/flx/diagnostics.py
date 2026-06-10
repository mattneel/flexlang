"""Source positions, spans, and compiler diagnostics.

Diagnostics render in the compiler-grade style described in ``docs/MVP.md`` §19:
a code, a message, a ``file:line:col`` locator, and a source line with a caret
underline.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Pos:
    """A 0-based byte offset plus 1-based line/column."""

    offset: int
    line: int
    col: int


@dataclass(frozen=True)
class Span:
    file: str
    start: Pos
    end: Pos

    def to(self, other: Span) -> Span:
        return Span(self.file, self.start, other.end)


class FlexError(Exception):
    """A compilation error carrying one or more diagnostics."""

    def __init__(self, diagnostics: list[Diagnostic]):
        self.diagnostics = diagnostics
        super().__init__("\n\n".join(d.render_plain() for d in diagnostics))


@dataclass
class Diagnostic:
    code: str
    message: str
    span: Span | None = None
    help: str | None = None
    notes: list[str] = field(default_factory=list)

    def render_plain(self) -> str:
        return f"error[{self.code}]: {self.message}"

    def render(self, source: str) -> str:
        """Render with a source-line caret underline, given the file's text."""
        out = [f"error[{self.code}]: {self.message}"]
        if self.span is not None:
            out.append(_render_span(self.span, source))
        for note in self.notes:
            out.append(f"note: {note}")
        if self.help is not None:
            out.append(f"help: {self.help}")
        return "\n".join(out)


def _render_span(span: Span, source: str) -> str:
    lines = source.splitlines()
    line_no = span.start.line
    if not (1 <= line_no <= len(lines)):
        return f"  {span.file}:{span.start.line}:{span.start.col}"
    src_line = lines[line_no - 1]
    gutter = str(line_no)
    pad = " " * len(gutter)
    caret_col = span.start.col - 1
    if span.end.line == span.start.line:
        width = max(1, span.end.col - span.start.col)
    else:
        width = max(1, len(src_line) - caret_col)
    underline = " " * caret_col + "^" * width
    return (
        f"  {span.file}:{span.start.line}:{span.start.col}\n"
        f"  {pad} |\n"
        f"  {gutter} | {src_line}\n"
        f"  {pad} | {underline}"
    )
