"""The macro/comptime expansion pass: a pure AST -> AST transform run between
parsing and type-checking.

After `expand`, no `ComptimeExpr`/`QuoteExpr`/`UnquoteExpr`/`MacroDecl` remain,
every `comptime { }` is folded to a literal, every macro call is expanded
(hygienically), and every `derive(...)` has produced a generated function.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, cast

from flx.diagnostics import Diagnostic, FlexError, Span
from flx.macro import hygiene
from flx.macro.interp import ComptimeError, Interp
from flx.macro.walk import map_children
from flx.syntax import ast

_EXPAND_DEPTH_LIMIT = 64


@dataclass
class Context:
    functions: dict[str, ast.FnDecl]
    records: dict[str, ast.RecordDecl]
    adts: dict[str, ast.AdtDecl]
    macros: dict[str, ast.MacroDecl]

    @classmethod
    def from_module(cls, module: ast.Module) -> Context:
        return cls(
            functions={f.name: f for f in module.functions},
            records={r.name: r for r in module.records},
            adts={a.name: a for a in module.adts},
            macros={m.name: m for m in module.macros},
        )


@dataclass
class Expander:
    ctx: Context
    depth: int = 0
    _gensym_n: int = 0
    trace: list[tuple[str, Span]] = field(default_factory=list)

    def gensym(self, base: str) -> str:
        self._gensym_n += 1
        return f"{base}${self._gensym_n}"

    # --- items ----------------------------------------------------------------

    def expand_item(self, item: ast.Item) -> ast.Item:
        if isinstance(item, (ast.FnDecl, ast.TestDecl, ast.TargetDecl)):
            return _replace(item, body=self.expand_block(item.body))
        return item

    # --- blocks / statements --------------------------------------------------

    def expand_block(self, block: ast.Block) -> ast.Block:
        stmts: list[ast.Stmt] = []
        for stmt in block.stmts:
            stmts.extend(self.expand_stmt(stmt))
        return ast.Block(stmts, block.span)

    def expand_stmt(self, stmt: ast.Stmt) -> list[ast.Stmt]:
        # A macro call in statement position may splice in several statements.
        if isinstance(stmt, ast.ExprStmt) and self._macro_call(stmt.expr):
            assert isinstance(stmt.expr, ast.CallExpr)
            fragment = self._expand_macro_fragment(stmt.expr)  # already re-expanded
            return list(fragment.stmts)
        rebuilt = map_children(
            stmt, self.expand_expr, self.expand_stmt, self.expand_block, _identity
        )
        return [rebuilt]

    # --- expressions ----------------------------------------------------------

    def expand_expr(self, expr: ast.Expr) -> ast.Expr:
        if isinstance(expr, (ast.QuoteExpr, ast.UnquoteExpr, ast.UnquoteSpliceExpr)):
            raise self._error(
                "MAC004", f"`{_kw(expr)}` is only valid inside a macro body", expr.span
            )
        # Fold comptime before recursing into children, so the interpreter (not
        # a premature inner fold with an empty env) handles nested comptime.
        if isinstance(expr, ast.ComptimeExpr):
            return self._fold_comptime(expr)
        node = cast(
            ast.Expr,
            map_children(expr, self.expand_expr, self.expand_stmt, self.expand_block, _identity),
        )
        if self._macro_call(node):
            assert isinstance(node, ast.CallExpr)
            fragment = self._expand_macro_fragment(node)
            return _block_to_expr(fragment, node.span, self)
        return node

    def _macro_call(self, expr: ast.Expr) -> bool:
        return (
            isinstance(expr, ast.CallExpr)
            and isinstance(expr.callee, ast.NameExpr)
            and expr.callee.name in self.ctx.macros
        )

    def _expand_macro_fragment(self, call: ast.CallExpr) -> ast.Block:
        assert isinstance(call.callee, ast.NameExpr)
        macro = self.ctx.macros[call.callee.name]
        if len(call.args) != len(macro.params):
            raise self._error(
                "MAC001",
                f"macro {macro.name!r} expects {len(macro.params)} argument(s), "
                f"got {len(call.args)}",
                call.span,
            )
        self.depth += 1
        if self.depth > _EXPAND_DEPTH_LIMIT:
            raise self._error("MAC003", "macro expansion too deep (recursive macro?)", call.span)
        self.trace.append((macro.name, call.span))
        try:
            env = dict(zip(macro.params, call.args, strict=True))
            interp = Interp(self.ctx)
            try:
                result = interp.eval_expr(macro.body, env)
            except ComptimeError as err:
                raise self._from_comptime(err) from err
            fragment = _coerce_block(result, call.span, self)
            fragment = cast(ast.Block, hygiene.rename(fragment, interp.sealed, self.gensym))
            # Re-expand the produced fragment while the depth guard is still
            # raised, so nested/recursive macros accumulate depth (MAC003).
            return self.expand_block(fragment)
        finally:
            self.trace.pop()
            self.depth -= 1

    def _fold_comptime(self, node: ast.ComptimeExpr) -> ast.Expr:
        interp = Interp(self.ctx)
        try:
            value = interp.eval_block(node.body, {})
        except ComptimeError as err:
            raise self._from_comptime(err) from err
        if isinstance(value, bool):
            return ast.BoolLit(value, node.span)
        if isinstance(value, int):
            return ast.IntLit(value, node.span)
        if isinstance(value, str):
            return ast.StringLit(value, node.span)
        raise self._error("CT008", "comptime { } did not produce a literal value", node.span)

    # --- errors ---------------------------------------------------------------

    def _error(self, code: str, message: str, span: Span | None) -> FlexError:
        notes = [f"in expansion of macro {name!r}" for name, _ in self.trace]
        return FlexError([Diagnostic(code, message, span, notes=notes)])

    def _from_comptime(self, err: ComptimeError) -> FlexError:
        return self._error(err.code, err.message, err.span)


def _identity(pattern: ast.Pattern) -> ast.Pattern:
    return pattern


def _replace(node: ast.Item, **changes: object) -> ast.Item:
    return cast(ast.Item, dataclasses.replace(cast(Any, node), **changes))


def _kw(expr: ast.Expr) -> str:
    return {
        ast.QuoteExpr: "quote",
        ast.UnquoteExpr: "unquote",
        ast.UnquoteSpliceExpr: "unquote_splice",
    }[type(expr)]


def _coerce_block(value: object, span: Span | None, exp: Expander) -> ast.Block:
    if isinstance(value, ast.Block):
        return value
    if isinstance(value, ast.Expr):
        return ast.Block([ast.ExprStmt(value, value.span)], value.span)
    raise exp._error("MAC002", "macro body did not produce code (expected a quote)", span)


def _block_to_expr(block: ast.Block, span: Span | None, exp: Expander) -> ast.Expr:
    if len(block.stmts) == 1 and isinstance(block.stmts[0], ast.ExprStmt):
        return block.stmts[0].expr
    raise exp._error(
        "MAC005", "macro in expression position must produce a single expression", span
    )


def expand(module: ast.Module) -> ast.Module:
    from flx.macro.derive import run_derives

    ctx = Context.from_module(module)
    exp = Expander(ctx)
    generated = run_derives(module, exp)

    items: list[ast.Item] = []
    for item in module.items:
        if isinstance(item, ast.MacroDecl):
            continue  # compile-time only
        items.append(exp.expand_item(item))
    items.extend(generated)
    return dataclasses.replace(module, items=items)
