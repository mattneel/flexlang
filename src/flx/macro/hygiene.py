"""Macro hygiene by gensym renaming.

Identifiers a macro *introduces* (a `let`/`mut`/pattern binder inside `quote`)
are renamed to fresh `name$N` gensyms — `$` is unspellable in source, so they
can't capture or be captured. Nodes that arrived via `unquote` are *sealed* and
left untouched, so caller identifiers keep their meaning.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from flx.macro import walk
from flx.syntax import ast

Scope = dict[str, str]


class Renamer:
    def __init__(self, sealed: set[int], gensym: Callable[[str], str]) -> None:
        self.sealed = sealed
        self.gensym = gensym

    def rename_block(self, block: ast.Block, scope: Scope) -> ast.Block:
        local = dict(scope)
        return ast.Block([self.rename_stmt(s, local) for s in block.stmts], block.span)

    def rename_stmt(self, stmt: ast.Stmt, scope: Scope) -> ast.Stmt:
        if id(stmt) in self.sealed:
            return stmt
        if isinstance(stmt, (ast.LetStmt, ast.MutStmt)):
            value = self.rename_expr(stmt.value, scope)
            name = stmt.name
            if id(stmt) not in self.sealed:
                name = self.gensym(stmt.name)
                scope[stmt.name] = name
            cls = type(stmt)
            return cls(name, value, stmt.span)
        if isinstance(stmt, ast.AssignStmt):
            target = scope.get(stmt.name, stmt.name)
            return ast.AssignStmt(target, self.rename_expr(stmt.value, scope), stmt.span)
        if isinstance(stmt, ast.WhileStmt):
            cond = self.rename_expr(stmt.cond, scope)
            return ast.WhileStmt(cond, self.rename_block(stmt.body, scope), stmt.span)
        if isinstance(stmt, ast.ForStmt):
            iterable = self.rename_expr(stmt.iter, scope)
            inner = dict(scope)
            inner[stmt.name] = name = self.gensym(stmt.name)
            return ast.ForStmt(name, iterable, self.rename_block(stmt.body, inner), stmt.span)
        if isinstance(stmt, ast.ReturnStmt):
            ret = self.rename_expr(stmt.value, scope) if stmt.value is not None else None
            return ast.ReturnStmt(ret, stmt.span)
        if isinstance(stmt, ast.ExprStmt):
            return ast.ExprStmt(self.rename_expr(stmt.expr, scope), stmt.span)
        return stmt

    def rename_expr(self, expr: ast.Expr, scope: Scope) -> ast.Expr:
        if id(expr) in self.sealed:
            return expr
        if isinstance(expr, ast.NameExpr):
            return ast.NameExpr(scope.get(expr.name, expr.name), expr.span)
        if isinstance(expr, ast.IfExpr):
            cond = self.rename_expr(expr.cond, scope)
            then = self.rename_block(expr.then_block, scope)
            els = self.rename_block(expr.else_block, scope) if expr.else_block else None
            return ast.IfExpr(cond, then, els, expr.span)
        if isinstance(expr, ast.RegionExpr):
            inner = dict(scope)
            inner[expr.name] = name = self.gensym(expr.name)
            return ast.RegionExpr(name, self.rename_block(expr.body, inner), expr.span)
        if isinstance(expr, ast.MatchExpr):
            scrut = self.rename_expr(expr.scrutinee, scope)
            arms = [self._rename_arm(a, scope) for a in expr.arms]
            return ast.MatchExpr(scrut, arms, expr.span)
        # Generic recursion for the remaining expression shapes.
        return cast(
            ast.Expr,
            walk.map_children(
                expr,
                lambda e: self.rename_expr(e, scope),
                lambda s: [self.rename_stmt(s, scope)],
                lambda b: self.rename_block(b, scope),
                lambda p: p,
            ),
        )

    def _rename_arm(self, arm: ast.MatchArm, scope: Scope) -> ast.MatchArm:
        inner = dict(scope)
        pattern = self._rename_pattern(arm.pattern, inner)
        return ast.MatchArm(pattern, self.rename_expr(arm.body, inner), arm.span)

    def _rename_pattern(self, pat: ast.Pattern, scope: Scope) -> ast.Pattern:
        if id(pat) in self.sealed:
            return pat
        if isinstance(pat, ast.BindPattern):
            scope[pat.name] = name = self.gensym(pat.name)
            return ast.BindPattern(name, pat.span)
        if isinstance(pat, ast.CtorPattern):
            args = [self._rename_pattern(a, scope) for a in pat.args]
            return ast.CtorPattern(pat.name, args, pat.span)
        return pat


def rename(node: Any, sealed: set[int], gensym: Callable[[str], str]) -> Any:
    renamer = Renamer(sealed, gensym)
    if isinstance(node, ast.Block):
        return renamer.rename_block(node, {})
    return renamer.rename_expr(node, {})
