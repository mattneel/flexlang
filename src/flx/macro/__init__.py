"""Compile-time metaprogramming: macro expansion, comptime evaluation, derive.

Runs as a pure AST -> AST pass between parsing and type-checking
(:func:`expand`), so everything it produces — expanded macros, folded
`comptime` blocks, generated `derive` functions — flows through the normal
checker and backend unchanged.
"""

from __future__ import annotations

from flx.macro.expand import expand

__all__ = ["expand"]
