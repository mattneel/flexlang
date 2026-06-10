"""The comptime interpreter.

A small tree-walking evaluator over the pure subset of Flex, used both to fold
`comptime { ... }` blocks and to run macro bodies. Compile-time values are
dynamically typed:

* ``int`` / ``bool`` / ``str`` — I64 / Bool / String constants;
* ``list`` — a comptime list (e.g. the result of ``reflect.fields``);
* ``dict`` — a comptime record (a field descriptor: ``{"name", "type", "of"}``);
* an ``ast`` node — a quoted AST fragment (produced by ``quote { ... }``).

Errors are raised as :class:`ComptimeError` and rendered with the offending
node's span plus an expansion trace by the expander.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from flx.diagnostics import Span
from flx.syntax import ast

if TYPE_CHECKING:
    from flx.macro.expand import Context

_I64_MAX = 2**63 - 1
_I64_MIN = -(2**63)
_STEP_LIMIT = 200_000
_DEPTH_LIMIT = 64

CtValue = Any  # int | bool | str | list | dict | ast node


class ComptimeError(Exception):
    def __init__(self, code: str, message: str, span: Span | None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.span = span


_INT_BINOPS: dict[str, Callable[[int, int], int]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
}
_CMP_BINOPS: dict[str, Callable[[Any, Any], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


@dataclass
class Interp:
    ctx: Context
    steps: int = 0
    depth: int = 0
    sealed: set[int] = field(default_factory=set)  # ids of unquote-spliced nodes

    def _tick(self, span: Span | None) -> None:
        self.steps += 1
        if self.steps > _STEP_LIMIT:
            raise ComptimeError("CT006", "comptime evaluation exceeded the step limit", span)

    # --- blocks ---------------------------------------------------------------

    def eval_block(self, block: ast.Block, env: dict[str, CtValue]) -> CtValue:
        local = dict(env)
        value: CtValue = None
        for stmt in block.stmts:
            value = self.eval_stmt(stmt, local)
            if isinstance(value, _Return):
                return value.value
        return value

    def eval_stmt(self, stmt: ast.Stmt, env: dict[str, CtValue]) -> CtValue:
        if isinstance(stmt, ast.LetStmt):
            env[stmt.name] = self.eval_expr(stmt.value, env)
            return None
        if isinstance(stmt, ast.ReturnStmt):
            return _Return(self.eval_expr(stmt.value, env) if stmt.value is not None else None)
        if isinstance(stmt, ast.ForStmt):
            return self._eval_for(stmt, env)
        if isinstance(stmt, ast.ExprStmt):
            return self.eval_expr(stmt.expr, env)
        raise ComptimeError(
            "CT001", f"`{type(stmt).__name__}` is not allowed in comptime", stmt.span
        )

    def _eval_for(self, stmt: ast.ForStmt, env: dict[str, CtValue]) -> CtValue:
        seq = self.eval_expr(stmt.iter, env)
        if not isinstance(seq, list):
            raise ComptimeError("CT010", "comptime `for` requires a list", stmt.iter.span)
        results: list[CtValue] = []
        for item in seq:
            env[stmt.name] = item
            results.append(self.eval_block(stmt.body, env))
        return results

    # --- expressions ----------------------------------------------------------

    def eval_expr(self, expr: ast.Expr, env: dict[str, CtValue]) -> CtValue:
        self._tick(expr.span)
        if isinstance(expr, ast.IntLit):
            return expr.value
        if isinstance(expr, ast.BoolLit):
            return expr.value
        if isinstance(expr, ast.StringLit):
            return expr.value
        if isinstance(expr, ast.NameExpr):
            if expr.name in env:
                return env[expr.name]
            raise ComptimeError("CT002", f"{expr.name!r} is not a comptime constant", expr.span)
        if isinstance(expr, ast.UnaryExpr):
            return self._eval_unary(expr, env)
        if isinstance(expr, ast.BinaryExpr):
            return self._eval_binary(expr, env)
        if isinstance(expr, ast.IfExpr):
            return self._eval_if(expr, env)
        if isinstance(expr, ast.MemberExpr):
            return self._eval_member(expr, env)
        if isinstance(expr, ast.CallExpr):
            return self._eval_call(expr, env)
        if isinstance(expr, ast.ComptimeExpr):
            return self.eval_block(expr.body, env)
        if isinstance(expr, ast.QuoteExpr):
            return self._eval_quote(expr, env)
        raise ComptimeError(
            "CT001", f"`{type(expr).__name__}` is not allowed in comptime", expr.span
        )

    def _eval_unary(self, expr: ast.UnaryExpr, env: dict[str, CtValue]) -> CtValue:
        v = self.eval_expr(expr.operand, env)
        if expr.op == "-" and isinstance(v, int) and not isinstance(v, bool):
            return self._int(-v, expr.span)
        if expr.op == "!" and isinstance(v, bool):
            return not v
        raise ComptimeError("CT001", f"bad operand for unary `{expr.op}`", expr.span)

    def _eval_binary(self, expr: ast.BinaryExpr, env: dict[str, CtValue]) -> CtValue:
        op = expr.op
        if op == "&&":
            return bool(self.eval_expr(expr.left, env)) and bool(self.eval_expr(expr.right, env))
        if op == "||":
            return bool(self.eval_expr(expr.left, env)) or bool(self.eval_expr(expr.right, env))
        left = self.eval_expr(expr.left, env)
        right = self.eval_expr(expr.right, env)
        if op == "++":  # comptime string concatenation
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            raise ComptimeError("CT001", "`++` requires String operands", expr.span)
        if op in ("/", "%"):
            if right == 0:
                raise ComptimeError("CT003", "comptime division by zero", expr.span)
            result = left // right if op == "/" else left - (left // right) * right
            return self._int(int(result), expr.span)
        if op in _INT_BINOPS:
            return self._int(_INT_BINOPS[op](left, right), expr.span)
        if op in _CMP_BINOPS:
            return _CMP_BINOPS[op](left, right)
        raise ComptimeError("CT001", f"operator `{op}` is not allowed in comptime", expr.span)

    def _eval_if(self, expr: ast.IfExpr, env: dict[str, CtValue]) -> CtValue:
        cond = self.eval_expr(expr.cond, env)
        if not isinstance(cond, bool):
            raise ComptimeError("CT004", "comptime `if` condition must be Bool", expr.cond.span)
        if cond:
            return self.eval_block(expr.then_block, env)
        if expr.else_block is not None:
            return self.eval_block(expr.else_block, env)
        return None

    def _eval_member(self, expr: ast.MemberExpr, env: dict[str, CtValue]) -> CtValue:
        obj = self.eval_expr(expr.obj, env)
        if isinstance(obj, dict) and expr.name in obj:
            return obj[expr.name]
        if isinstance(obj, str) and expr.name == "length":
            return len(obj)
        raise ComptimeError("CT001", f"no comptime member .{expr.name}", expr.span)

    def _eval_call(self, expr: ast.CallExpr, env: dict[str, CtValue]) -> CtValue:
        callee = expr.callee
        # reflect.fields(T)
        if (
            isinstance(callee, ast.MemberExpr)
            and isinstance(callee.obj, ast.NameExpr)
            and callee.obj.name == "reflect"
        ):
            return self._eval_reflect(callee.name, expr, env)
        if isinstance(callee, ast.NameExpr) and callee.name in self.ctx.functions:
            return self._eval_user_call(callee.name, expr, env)
        raise ComptimeError("CT001", "this call is not allowed in comptime", expr.span)

    def _eval_reflect(self, method: str, call: ast.CallExpr, env: dict[str, CtValue]) -> CtValue:
        if method != "fields" or len(call.args) != 1 or not isinstance(call.args[0], ast.NameExpr):
            raise ComptimeError("CT009", "reflect.fields(T) requires a type name", call.span)
        tname = call.args[0].name
        record = self.ctx.records.get(tname)
        if record is None:
            raise ComptimeError(
                "CT009", f"reflect.fields needs a record type, got {tname!r}", call.span
            )
        return [{"name": f.name, "type": f.type, "of": tname} for f in record.fields]

    def _eval_user_call(self, name: str, call: ast.CallExpr, env: dict[str, CtValue]) -> CtValue:
        fn = self.ctx.functions[name]
        if fn.effects:
            raise ComptimeError("CT001", f"comptime cannot call effectful {name!r}", call.span)
        if len(call.args) != len(fn.params):
            raise ComptimeError("CT001", f"{name!r} arity mismatch in comptime", call.span)
        self.depth += 1
        if self.depth > _DEPTH_LIMIT:
            raise ComptimeError("CT005", "comptime recursion too deep", call.span)
        local = {p.name: self.eval_expr(a, env) for p, a in zip(fn.params, call.args, strict=True)}
        result = self.eval_block(fn.body, local)
        if isinstance(result, _Return):
            result = result.value
        self.depth -= 1
        return result

    def _eval_quote(self, expr: ast.QuoteExpr, env: dict[str, CtValue]) -> CtValue:
        from flx.macro.quasiquote import splice_block

        node = splice_block(expr.body, self, env)
        return node

    def _int(self, value: int, span: Span | None) -> int:
        if not (_I64_MIN <= value <= _I64_MAX):
            raise ComptimeError("CT007", f"comptime integer {value} out of I64 range", span)
        return value


@dataclass
class _Return:
    value: CtValue
