"""Hand-written lexer for Flex.

Produces a flat token stream with source spans. Errors (unterminated strings,
unknown characters) are raised as :class:`~flx.diagnostics.FlexError`.
"""

from __future__ import annotations

import textwrap

from flx.diagnostics import Diagnostic, FlexError, Pos, Span
from flx.syntax.tokens import KEYWORDS, Token, TokenKind

# Multi-character operators, checked longest-first.
_OPERATORS: list[tuple[str, TokenKind]] = [
    ("->", TokenKind.ARROW),
    ("=>", TokenKind.FAT_ARROW),
    ("|>", TokenKind.PIPE_GT),
    ("==", TokenKind.EQ_EQ),
    ("!=", TokenKind.BANG_EQ),
    ("<=", TokenKind.LE),
    (">=", TokenKind.GE),
    ("&&", TokenKind.AMP_AMP),
    ("||", TokenKind.PIPE_PIPE),
    ("++", TokenKind.PLUS_PLUS),
    ("<<", TokenKind.SHL),
    (">>", TokenKind.SHR),
    ("(", TokenKind.LPAREN),
    (")", TokenKind.RPAREN),
    ("{", TokenKind.LBRACE),
    ("}", TokenKind.RBRACE),
    ("[", TokenKind.LBRACKET),
    ("]", TokenKind.RBRACKET),
    (",", TokenKind.COMMA),
    (":", TokenKind.COLON),
    (";", TokenKind.SEMI),
    (".", TokenKind.DOT),
    ("=", TokenKind.EQ),
    ("<", TokenKind.LT),
    (">", TokenKind.GT),
    ("+", TokenKind.PLUS),
    ("-", TokenKind.MINUS),
    ("*", TokenKind.STAR),
    ("/", TokenKind.SLASH),
    ("%", TokenKind.PERCENT),
    ("!", TokenKind.BANG),
    ("&", TokenKind.AMP),
    ("^", TokenKind.CARET),
    ("|", TokenKind.PIPE),
    ("?", TokenKind.QUESTION),
]


def _is_ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_"


def _is_ident_continue(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


class Lexer:
    def __init__(self, source: str, file: str = "<input>") -> None:
        self.source = source
        self.file = file
        self.i = 0
        self.line = 1
        self.col = 1

    def _pos(self) -> Pos:
        return Pos(self.i, self.line, self.col)

    def _advance(self) -> str:
        ch = self.source[self.i]
        self.i += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def _peek(self, ahead: int = 0) -> str:
        j = self.i + ahead
        return self.source[j] if j < len(self.source) else ""

    def _span(self, start: Pos) -> Span:
        return Span(self.file, start, self._pos())

    def _error(self, message: str, start: Pos) -> FlexError:
        return FlexError([Diagnostic("LEX001", message, self._span(start))])

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while self.i < len(self.source):
            ch = self._peek()
            if ch in " \t\r\n":
                self._advance()
                continue
            if ch == "/" and self._peek(1) == "/":
                while self.i < len(self.source) and self._peek() != "\n":
                    self._advance()
                continue
            if ch == '"':
                tokens.append(self._string())
                continue
            if ch.isdigit():
                tokens.append(self._number())
                continue
            if _is_ident_start(ch):
                tokens.append(self._ident())
                continue
            tokens.append(self._operator())

        tokens.append(Token(TokenKind.EOF, "", self._span(self._pos())))
        return tokens

    def _string(self) -> Token:
        start = self._pos()
        if self.source.startswith('"""', self.i):
            return self._triple_string(start)
        self._advance()  # opening quote
        chars: list[str] = []
        while True:
            if self.i >= len(self.source):
                raise self._error("unterminated string literal", start)
            ch = self._advance()
            if ch == '"':
                break
            if ch == "\\":
                if self.i >= len(self.source):
                    raise self._error("unterminated string literal", start)
                esc = self._advance()
                if esc == "x":
                    chars.append(self._hex_escape(start))
                    continue
                # NB: no \0 — strings are NUL-terminated, so an embedded NUL
                # would split the string's strlen extent from its stored length.
                known = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
                if esc not in known:
                    # An unknown escape used to silently drop the backslash
                    # ("\q" became "q") — mangling data is worse than an error.
                    raise self._error(
                        f'unknown string escape "\\{esc}"; '
                        'the escapes are \\n \\t \\r \\" \\\\ \\xNN',
                        start,
                    )
                chars.append(known[esc])
            elif ch == "\n":
                raise self._error("unterminated string literal", start)
            else:
                chars.append(ch)
        text = "".join(chars)
        if not text.isascii():
            # One canonical representation per byte sequence: \xNN bytes cook to
            # surrogates, and adjacent escapes can complete a valid UTF-8
            # sequence ("\xc3\xa9" IS "é" on the wire) — re-decode so equal byte
            # strings are equal Python strings, the form read_line/argv produce.
            text = text.encode("utf-8", "surrogateescape").decode("utf-8", "surrogateescape")
        return Token(TokenKind.STRING, text, self._span(start))

    def _hex_escape(self, start: Pos) -> str:
        """A \\xNN byte escape: exactly two hex digits, any byte except NUL.
        Bytes >= 0x80 cook to their surrogateescape form — the same lossless
        representation bytes from stdin, argv, and extern calls carry."""
        digits = ""
        for _ in range(2):
            if self.i < len(self.source) and self._peek() in "0123456789abcdefABCDEF":
                digits += self._advance()
        if len(digits) != 2:
            raise self._error(
                '\\x needs exactly two hex digits, e.g. "\\x41"',
                start,
            )
        value = int(digits, 16)
        if value == 0:
            raise self._error(
                "\\x00 is not allowed: strings are NUL-terminated, so a string cannot carry byte 0",
                start,
            )
        return chr(value) if value < 0x80 else chr(0xDC00 + value)

    def _triple_string(self, start: Pos) -> Token:
        """A \"\"\"...\"\"\" block: raw text (no escapes — doc prose keeps its
        backslashes), with the leading newline dropped and common indentation
        stripped so blocks can sit at any nesting depth."""
        self._advance()
        self._advance()
        self._advance()  # the opening quotes, keeping line/col tracking honest
        end = self.source.find('"""', self.i)
        if end == -1:
            raise self._error('unterminated """ block', start)
        raw = self.source[self.i : end]
        while self.i < end + 3:
            self._advance()
        # Drop the opening line when it holds nothing but whitespace — an
        # invisible trailing space after `"""` must not inject a leading "\n".
        nl = raw.find("\n")
        text = raw[nl + 1 :] if nl != -1 and not raw[:nl].strip() else raw
        text = textwrap.dedent(text).rstrip()
        return Token(TokenKind.STRING, text, self._span(start))

    def _number(self) -> Token:
        start = self._pos()
        digits: list[str] = []
        saw_underscore = False
        while self.i < len(self.source) and (self._peek().isdigit() or self._peek() == "_"):
            ch = self._advance()
            if ch != "_":
                digits.append(ch)
            else:
                saw_underscore = True
        if (
            digits == ["0"]
            and not saw_underscore  # `0_x10` is not a prefix
            and self.i < len(self.source)
            and self._peek() in ("x", "X", "b", "B")
        ):
            base = self._advance().lower()
            allowed = "0123456789abcdefABCDEF" if base == "x" else "01"
            body: list[str] = []
            while self.i < len(self.source) and (self._peek() in allowed or self._peek() == "_"):
                ch = self._advance()
                if ch != "_":
                    body.append(ch)
            if not body:
                raise self._error(f"0{base} must be followed by digits", start)
            return Token(TokenKind.INT, f"0{base}{''.join(body)}", self._span(start))
        # A '.' followed by a digit makes a float (a '.' followed by a name is
        # member access on an integer); so does a bare exponent (1e9).
        is_float = False
        if (
            self.i + 1 < len(self.source)
            and self._peek() == "."
            and self.source[self.i + 1].isdigit()
        ):
            is_float = True
            digits.append(self._advance())  # '.'
            while self.i < len(self.source) and (self._peek().isdigit() or self._peek() == "_"):
                ch = self._advance()
                if ch != "_":
                    digits.append(ch)
        if (
            self.i < len(self.source)
            and self._peek() in ("e", "E")
            and (
                (self.i + 1 < len(self.source) and self.source[self.i + 1].isdigit())
                or (
                    self.i + 2 < len(self.source)
                    and self.source[self.i + 1] in ("+", "-")
                    and self.source[self.i + 2].isdigit()
                )
            )
        ):
            is_float = True
            digits.append(self._advance())  # 'e'
            if self._peek() in ("+", "-"):
                digits.append(self._advance())
            while self.i < len(self.source) and self._peek().isdigit():
                digits.append(self._advance())
        kind = TokenKind.FLOAT if is_float else TokenKind.INT
        return Token(kind, "".join(digits), self._span(start))

    def _ident(self) -> Token:
        start = self._pos()
        chars: list[str] = []
        while self.i < len(self.source) and _is_ident_continue(self._peek()):
            chars.append(self._advance())
        text = "".join(chars)
        kind = KEYWORDS.get(text, TokenKind.IDENT)
        return Token(kind, text, self._span(start))

    def _operator(self) -> Token:
        start = self._pos()
        for text, kind in _OPERATORS:
            if self.source.startswith(text, self.i):
                for _ in text:
                    self._advance()
                return Token(kind, text, self._span(start))
        ch = self._advance()
        raise self._error(f"unexpected character {ch!r}", start)


def tokenize(source: str, file: str = "<input>") -> list[Token]:
    return Lexer(source, file).tokenize()
