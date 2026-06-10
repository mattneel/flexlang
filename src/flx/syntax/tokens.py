"""Token kinds for the Flex lexer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from flx.diagnostics import Span


class TokenKind(Enum):
    # literals / identifiers
    INT = auto()
    STRING = auto()
    IDENT = auto()

    # keywords
    KW_MODULE = auto()
    KW_IMPORT = auto()
    KW_FN = auto()
    KW_LET = auto()
    KW_MUT = auto()
    KW_IF = auto()
    KW_ELSE = auto()
    KW_WHILE = auto()
    KW_RETURN = auto()
    KW_TEST = auto()
    KW_USES = auto()
    KW_TRUE = auto()
    KW_FALSE = auto()
    KW_TYPE = auto()
    KW_MATCH = auto()
    KW_REGION = auto()
    KW_WITH = auto()
    KW_COMPTIME = auto()
    KW_QUOTE = auto()
    KW_UNQUOTE = auto()
    KW_UNQUOTE_SPLICE = auto()
    KW_MACRO = auto()
    KW_DERIVE = auto()
    KW_FOR = auto()
    KW_IN = auto()

    # delimiters
    LPAREN = auto()
    RPAREN = auto()
    LBRACE = auto()
    RBRACE = auto()
    LBRACKET = auto()
    RBRACKET = auto()
    COMMA = auto()
    COLON = auto()
    SEMI = auto()
    DOT = auto()

    # operators
    ARROW = auto()  # ->
    FAT_ARROW = auto()  # =>
    PIPE_GT = auto()  # |>
    EQ = auto()  # =
    EQ_EQ = auto()  # ==
    BANG_EQ = auto()  # !=
    LT = auto()
    LE = auto()
    GT = auto()
    GE = auto()
    PLUS = auto()
    PLUS_PLUS = auto()  # ++ (string concatenation)
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    PERCENT = auto()
    AMP_AMP = auto()  # &&
    PIPE_PIPE = auto()  # ||
    BANG = auto()  # !
    PIPE = auto()  # |
    QUESTION = auto()  # ?

    EOF = auto()


KEYWORDS: dict[str, TokenKind] = {
    "module": TokenKind.KW_MODULE,
    "import": TokenKind.KW_IMPORT,
    "fn": TokenKind.KW_FN,
    "let": TokenKind.KW_LET,
    "mut": TokenKind.KW_MUT,
    "if": TokenKind.KW_IF,
    "else": TokenKind.KW_ELSE,
    "while": TokenKind.KW_WHILE,
    "return": TokenKind.KW_RETURN,
    "test": TokenKind.KW_TEST,
    "uses": TokenKind.KW_USES,
    "true": TokenKind.KW_TRUE,
    "false": TokenKind.KW_FALSE,
    "type": TokenKind.KW_TYPE,
    "match": TokenKind.KW_MATCH,
    "region": TokenKind.KW_REGION,
    "with": TokenKind.KW_WITH,
    "comptime": TokenKind.KW_COMPTIME,
    "quote": TokenKind.KW_QUOTE,
    "unquote": TokenKind.KW_UNQUOTE,
    "unquote_splice": TokenKind.KW_UNQUOTE_SPLICE,
    "macro": TokenKind.KW_MACRO,
    "derive": TokenKind.KW_DERIVE,
    "for": TokenKind.KW_FOR,
    "in": TokenKind.KW_IN,
}


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    text: str
    span: Span

    def __repr__(self) -> str:
        return f"Token({self.kind.name}, {self.text!r})"
