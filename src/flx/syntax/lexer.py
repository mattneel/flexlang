"""Hand-written lexer for Flex.

Produces a flat token stream with source spans. Errors (unterminated strings,
unknown characters) are raised as :class:`~flx.diagnostics.FlexError`.
"""

from __future__ import annotations

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
                chars.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(esc, esc))
            elif ch == "\n":
                raise self._error("unterminated string literal", start)
            else:
                chars.append(ch)
        return Token(TokenKind.STRING, "".join(chars), self._span(start))

    def _number(self) -> Token:
        start = self._pos()
        digits: list[str] = []
        while self.i < len(self.source) and (self._peek().isdigit() or self._peek() == "_"):
            ch = self._advance()
            if ch != "_":
                digits.append(ch)
        if digits == ["0"] and self.i < len(self.source) and self._peek() in ("x", "X", "b", "B"):
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
