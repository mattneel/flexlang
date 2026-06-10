"""A Pygments lexer for the Flex programming language.

The token surface tracks ``docs/MVP.md``: declaration/control keywords,
``uses { ... }`` effect sets, ``region`` blocks, function contracts, the pipe
(``|>``) and result-propagation (``?``) operators, primitive types, ADT
constructors, and ``//`` line comments. It is intentionally regex-based and
standalone; once the real compiler tokenizer lands it becomes the single
source of truth and this grammar can be cross-checked against it.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pygments.lexer import RegexLexer, bygroups, default, words
from pygments.token import (
    Comment,
    Keyword,
    Name,
    Number,
    Operator,
    Punctuation,
    String,
    Whitespace,
)

__all__ = ["FlexLexer"]

# Built-in primitive types (everything else uppercase is treated as a type or
# constructor via the generic Uppercase rule).
_PRIMITIVE_TYPES = (
    "I8",
    "I16",
    "I32",
    "I64",
    "U8",
    "U16",
    "U32",
    "U64",
    "F32",
    "F64",
    "Bool",
    "String",
    "Char",
    "Unit",
)

# Functions provided by the test/prelude surface.
_BUILTINS = ("assert", "assert_eq", "assert_ne", "fail", "panic")

# Declaration-introducing keywords.
_DECLARATIONS = (
    "fn",
    "let",
    "mut",
    "type",
    "test",
    "macro",
    "bench",
    "property",
    "trait",
    "impl",
    "comptime",
)

# Control flow and other reserved words.
_KEYWORDS = (
    "if",
    "else",
    "while",
    "for",
    "in",
    "match",
    "return",
    "with",
    "region",
    "unsafe",
    "as",
    "quote",
    "unquote",
    "unquote_splice",
    "repr",
    "await",
    "spawn",
)

# Zero-cost function contracts (docs/MVP.md §9).
_CONTRACTS = ("pure", "no_alloc", "no_panic")


class FlexLexer(RegexLexer):
    """Lexer for Flex (``.flx``) source files."""

    name = "Flex"
    # NB: the `flx` alias and `*.flx` filename are also claimed by Pygments'
    # built-in Felix lexer. We expose only the unambiguous `flex` alias and set
    # a higher priority so filename-based lookup of `.flx` resolves to Flex.
    aliases: ClassVar[list[str]] = ["flex"]
    filenames: ClassVar[list[str]] = ["*.flx"]
    mimetypes: ClassVar[list[str]] = ["text/x-flex"]
    url = "https://github.com/threephasetechnology/flexlang"
    priority = 1.0

    tokens: ClassVar[dict[str, list[Any]]] = {
        "root": [
            (r"\s+", Whitespace),
            (r"//[^\n]*", Comment.Single),
            # `module Foo.Bar` / `import Core.Result`
            (
                r"\b(module|import)\b(\s+)([A-Za-z_][\w.]*)",
                bygroups(Keyword.Namespace, Whitespace, Name.Namespace),
            ),
            # `uses { Fs, Alloc }` effect sets.
            (r"\buses\b", Keyword, "effectset"),
            (words(_DECLARATIONS, suffix=r"\b"), Keyword.Declaration),
            (words(_KEYWORDS, suffix=r"\b"), Keyword),
            (words(_CONTRACTS, suffix=r"\b"), Keyword.Pseudo),
            (words(("true", "false", "null"), suffix=r"\b"), Keyword.Constant),
            (words(_BUILTINS, suffix=r"\b"), Name.Builtin),
            (words(_PRIMITIVE_TYPES, suffix=r"\b"), Keyword.Type),
            # Numbers.
            (r"0[xX][0-9a-fA-F_]+", Number.Hex),
            (r"0[bB][01_]+", Number.Bin),
            (r"0[oO][0-7_]+", Number.Oct),
            (r"\d[\d_]*\.[\d_]+(?:[eE][+-]?\d[\d_]*)?", Number.Float),
            (r"\d[\d_]*", Number.Integer),
            # Strings.
            (r'"', String, "string"),
            # `name(` — a call.
            (r"([a-z_]\w*)(\s*)(\()", bygroups(Name.Function, Whitespace, Punctuation)),
            # Uppercase-leading: types and ADT constructors.
            (r"\b[A-Z]\w*\b", Name.Class),
            # Lowercase identifiers.
            (r"\b[a-z_]\w*\b", Name),
            # Operators, longest first (pipe, arrows, comparisons).
            (r"\|>|->|=>|==|!=|<=|>=|&&|\|\||[-+*/%<>=!&|?.]", Operator),
            # Punctuation / delimiters.
            (r"[{}()\[\],;:]", Punctuation),
        ],
        "effectset": [
            (r"\s+", Whitespace),
            (r"\{", Punctuation),
            (r",", Punctuation),
            (r"[A-Z]\w*", Name.Builtin),
            (r"\}", Punctuation, "#pop"),
            default("#pop"),
        ],
        "string": [
            (r'[^"\\]+', String),
            (r"\\.", String.Escape),
            (r'"', String, "#pop"),
        ],
    }
