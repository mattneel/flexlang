"""Generic bottom-up AST transformation over the frozen AST.

`map_children` rebuilds a node with its direct Expr/Stmt/Block/Pattern children
(and the FieldInit/MatchArm containers that hold them) replaced by the supplied
callbacks, via ``dataclasses.replace``. Because every rebuilt node is a fresh
frozen object, the checker's identity-keyed ``expr_types`` stays correct.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from flx.syntax import ast

ExprFn = Callable[[ast.Expr], ast.Expr]
StmtFn = Callable[[ast.Stmt], list[ast.Stmt]]
BlockFn = Callable[[ast.Block], ast.Block]
PatFn = Callable[[ast.Pattern], ast.Pattern]


def map_children(node: Any, fe: ExprFn, fs: StmtFn, fb: BlockFn, fp: PatFn) -> Any:
    changes: dict[str, Any] = {}
    for fld in dataclasses.fields(node):
        old = getattr(node, fld.name)
        new = _map(old, fe, fs, fb, fp)
        if new is not old:
            changes[fld.name] = new
    return dataclasses.replace(node, **changes) if changes else node


def _map(value: Any, fe: ExprFn, fs: StmtFn, fb: BlockFn, fp: PatFn) -> Any:
    if isinstance(value, ast.Block):
        return fb(value)
    if isinstance(value, ast.Expr):
        return fe(value)
    if isinstance(value, ast.Pattern):
        return fp(value)
    if isinstance(value, ast.Stmt):
        # A statement may expand into several (e.g. a statement macro); the only
        # places that hold a list of statements are Block.stmts, handled below.
        result = fs(value)
        return result[0] if len(result) == 1 else value
    if isinstance(value, list):
        # Statement lists are flattened (fs returns a list); other lists map 1:1.
        out: list[Any] = []
        changed = False
        for item in value:
            if isinstance(item, ast.Stmt):
                expanded = fs(item)
                changed = changed or expanded != [item]
                out.extend(expanded)
            else:
                mapped = _map(item, fe, fs, fb, fp)
                changed = changed or mapped is not item
                out.append(mapped)
        return out if changed else value
    if isinstance(value, (ast.FieldInit, ast.MatchArm)):
        return map_children(value, fe, fs, fb, fp)
    return value
