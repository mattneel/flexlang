"""Quasiquotation: turn a `quote { ... }` template into a concrete AST.

`unquote(e)` holes are filled with the AST value of evaluating `e` (ints/bools/
strings auto-lift to literals); `unquote_splice(e)` in a block splices a comptime
list of statements. Nodes that arrive via unquote are recorded as *sealed* so
hygiene leaves their identifiers alone.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, cast

from flx.diagnostics import Pos, Span
from flx.macro import walk
from flx.macro.interp import ComptimeError
from flx.syntax import ast

_NO_SPAN = Span("<macro>", Pos(0, 0, 0), Pos(0, 0, 0))

if TYPE_CHECKING:
    from flx.macro.interp import CtValue, Interp


def splice_block(block: ast.Block, interp: Interp, env: dict[str, CtValue]) -> ast.Block:
    stmts: list[ast.Stmt] = []
    for stmt in block.stmts:
        spliced = _splice_stmt(stmt, interp, env)
        stmts.append(spliced) if isinstance(spliced, ast.Stmt) else stmts.extend(spliced)
    return ast.Block(stmts, block.span)


def _splice_stmt(
    stmt: ast.Stmt, interp: Interp, env: dict[str, CtValue]
) -> ast.Stmt | list[ast.Stmt]:
    if isinstance(stmt, ast.ExprStmt) and isinstance(stmt.expr, ast.UnquoteSpliceExpr):
        value = interp.eval_expr(stmt.expr.expr, env)
        if not isinstance(value, list):
            raise ComptimeError("MAC006", "unquote_splice requires a comptime list", stmt.span)
        out: list[ast.Stmt] = []
        for item in value:
            out.extend(_splice_items(item, interp, stmt.span))
        return out
    return cast(ast.Stmt, _splice_via(stmt, interp, env))


def _splice_items(item: CtValue, interp: Interp, span: Span) -> list[ast.Stmt]:
    node = _as_node(item, span)
    interp.sealed.update(_ids(node))
    if isinstance(node, ast.Block):  # `quote { ... }` yields a block; flatten it
        return list(node.stmts)
    if isinstance(node, ast.Stmt):
        return [node]
    return [ast.ExprStmt(node, span)]


def _splice_expr(expr: ast.Expr, interp: Interp, env: dict[str, CtValue]) -> ast.Expr:
    if isinstance(expr, ast.UnquoteExpr):
        node = _as_node(interp.eval_expr(expr.expr, env), expr.span)
        if not isinstance(node, ast.Expr):
            raise ComptimeError("MAC007", "unquote here must produce an expression", expr.span)
        interp.sealed.update(_ids(node))
        return node
    if isinstance(expr, ast.UnquoteSpliceExpr):
        raise ComptimeError("MAC006", "unquote_splice is only valid in a block", expr.span)
    return cast(ast.Expr, _splice_via(expr, interp, env))


def _splice_via(node: Any, interp: Interp, env: dict[str, CtValue]) -> Any:
    return walk.map_children(
        node,
        lambda e: _splice_expr(e, interp, env),
        lambda s: [r] if isinstance(r := _splice_stmt(s, interp, env), ast.Stmt) else r,
        lambda b: splice_block(b, interp, env),
        lambda p: p,
    )


def _as_node(value: CtValue, span: Span | None) -> Any:
    if isinstance(value, bool):
        return ast.BoolLit(value, span or _NO_SPAN)
    if isinstance(value, int):
        return ast.IntLit(value, span or _NO_SPAN)
    if isinstance(value, str):
        return ast.StringLit(value, span or _NO_SPAN)
    if isinstance(value, (ast.Expr, ast.Stmt, ast.Block)):
        return value
    raise ComptimeError("MAC007", "unquote value is not an AST fragment", span)


_NODE_TYPES = (ast.Expr, ast.Stmt, ast.Block, ast.Pattern, ast.FieldInit, ast.MatchArm)


def _ids(node: Any) -> set[int]:
    found: set[int] = set()
    stack: list[Any] = [node]
    while stack:
        n = stack.pop()
        if not isinstance(n, _NODE_TYPES) or id(n) in found:
            continue
        found.add(id(n))
        for fld in dataclasses.fields(cast(Any, n)):
            value = getattr(n, fld.name)
            stack.extend(value) if isinstance(value, list) else stack.append(value)
    return found
