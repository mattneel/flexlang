"""Name resolution and type checking for the Flex MVP.

Produces a :class:`CheckResult` mapping each expression to its type (keyed by
node identity) and validates arity, operand types, return types, and mutability.
Effect and region checking are deferred (parsed but not enforced).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flx.diagnostics import Diagnostic, FlexError, Span
from flx.syntax import ast
from flx.types import BOOL, ERROR, I64, PRIMITIVES, STRING, UNIT, FnType, Type

# name -> (arity or None for variadic-ish, checker). Builtins are checked ad hoc.
_BUILTINS = {"assert", "assert_eq", "assert_ne", "fail", "panic"}

_ARITH = {"+", "-", "*", "/", "%"}
_COMPARE = {"<", "<=", ">", ">="}
_EQUALITY = {"==", "!="}
_BOOLEAN = {"&&", "||"}

_I64_MAX = 2**63 - 1


@dataclass
class _Binding:
    type: Type
    mutable: bool


@dataclass
class CheckResult:
    module: ast.Module
    expr_types: dict[int, Type]
    functions: dict[str, FnType]


@dataclass
class _Scope:
    frames: list[dict[str, _Binding]] = field(default_factory=lambda: [{}])

    def push(self) -> None:
        self.frames.append({})

    def pop(self) -> None:
        self.frames.pop()

    def define(self, name: str, binding: _Binding) -> None:
        self.frames[-1][name] = binding

    def lookup(self, name: str) -> _Binding | None:
        for frame in reversed(self.frames):
            if name in frame:
                return frame[name]
        return None


class Checker:
    def __init__(self, module: ast.Module) -> None:
        self.module = module
        self.diags: list[Diagnostic] = []
        self.expr_types: dict[int, Type] = {}
        self.functions: dict[str, FnType] = {}
        self.scope = _Scope()
        self.return_type: Type = UNIT
        self.in_test = False

    # --- entry ----------------------------------------------------------------

    def check(self) -> CheckResult:
        for fn in self.module.functions:
            if fn.name in self.functions:
                self._err("TYPE002", f"function {fn.name!r} is already defined", fn.span)
            params = tuple(self._resolve_type(p.type) for p in fn.params)
            ret = self._resolve_type(fn.return_type) if fn.return_type else UNIT
            self.functions[fn.name] = FnType(params, ret)

        for fn in self.module.functions:
            self._check_fn(fn)
        for test in self.module.tests:
            self._check_test(test)

        if self.diags:
            raise FlexError(self.diags)
        return CheckResult(self.module, self.expr_types, self.functions)

    # --- declarations ---------------------------------------------------------

    def _check_fn(self, fn: ast.FnDecl) -> None:
        self.scope = _Scope()
        self.in_test = False
        fn_ty = self.functions[fn.name]
        seen: set[str] = set()
        for param, ptype in zip(fn.params, fn_ty.params, strict=True):
            if param.name in seen:
                self._err("NAME002", f"duplicate parameter name {param.name!r}", param.span)
            seen.add(param.name)
            self.scope.define(param.name, _Binding(ptype, mutable=False))
        self.return_type = fn_ty.ret
        body_ty = self._check_block(fn.body)
        # A body that's guaranteed to `return` needs no tail value; its returns
        # are type-checked individually.
        if fn_ty.ret is not UNIT and not _diverges(fn.body):
            if fn.body.tail is not None:
                self._expect(fn_ty.ret, body_ty, fn.body.tail.span, "return value")
            else:
                self._err(
                    "TYPE009",
                    f"function {fn.name!r} must return {fn_ty.ret} but its body has no value",
                    fn.span,
                )

    def _check_test(self, test: ast.TestDecl) -> None:
        self.scope = _Scope()
        self.in_test = True
        self.return_type = UNIT
        self._check_block(test.body)

    # --- statements / blocks --------------------------------------------------

    def _check_block(self, block: ast.Block) -> Type:
        self.scope.push()
        result: Type = UNIT
        for stmt in block.stmts:
            result = self._check_stmt(stmt)
        # The block's value is its trailing expression, else Unit.
        value = result if block.tail is not None else UNIT
        self.scope.pop()
        return value

    def _check_stmt(self, stmt: ast.Stmt) -> Type:
        if isinstance(stmt, ast.LetStmt):
            self.scope.define(stmt.name, _Binding(self._check_expr(stmt.value), mutable=False))
            return UNIT
        if isinstance(stmt, ast.MutStmt):
            self.scope.define(stmt.name, _Binding(self._check_expr(stmt.value), mutable=True))
            return UNIT
        if isinstance(stmt, ast.AssignStmt):
            self._check_assign(stmt)
            return UNIT
        if isinstance(stmt, ast.WhileStmt):
            self._expect(BOOL, self._check_expr(stmt.cond), stmt.cond.span, "while condition")
            self._check_block(stmt.body)
            return UNIT
        if isinstance(stmt, ast.ReturnStmt):
            actual = self._check_expr(stmt.value) if stmt.value is not None else UNIT
            span = stmt.value.span if stmt.value is not None else stmt.span
            self._expect(self.return_type, actual, span, "return value")
            return UNIT
        if isinstance(stmt, ast.ExprStmt):
            return self._check_expr(stmt.expr)
        return UNIT

    def _check_assign(self, stmt: ast.AssignStmt) -> None:
        binding = self.scope.lookup(stmt.name)
        value_ty = self._check_expr(stmt.value)
        if binding is None:
            self._err("NAME001", f"cannot assign to undefined binding {stmt.name!r}", stmt.span)
            return
        if not binding.mutable:
            self._err(
                "MUT001",
                f"cannot assign to immutable binding {stmt.name!r}",
                stmt.span,
                help=f"declare it with `mut {stmt.name}` to allow mutation",
            )
            return
        self._expect(binding.type, value_ty, stmt.value.span, "assigned value")

    # --- expressions ----------------------------------------------------------

    def _check_expr(self, expr: ast.Expr) -> Type:
        ty = self._infer(expr)
        self.expr_types[id(expr)] = ty
        return ty

    def _infer(self, expr: ast.Expr) -> Type:
        if isinstance(expr, ast.IntLit):
            if expr.value > _I64_MAX:
                self._err(
                    "TYPE011",
                    f"integer literal {expr.value} is out of range for I64 (max {_I64_MAX})",
                    expr.span,
                )
            return I64
        if isinstance(expr, ast.BoolLit):
            return BOOL
        if isinstance(expr, ast.StringLit):
            return STRING
        if isinstance(expr, ast.NameExpr):
            return self._infer_name(expr)
        if isinstance(expr, ast.UnaryExpr):
            return self._infer_unary(expr)
        if isinstance(expr, ast.BinaryExpr):
            return self._infer_binary(expr)
        if isinstance(expr, ast.CallExpr):
            return self._infer_call(expr)
        if isinstance(expr, ast.IfExpr):
            return self._infer_if(expr)
        if isinstance(expr, ast.MemberExpr):
            self._err("TYPE010", "module-qualified calls are not supported yet", expr.span)
            return ERROR
        return ERROR

    def _infer_name(self, expr: ast.NameExpr) -> Type:
        binding = self.scope.lookup(expr.name)
        if binding is not None:
            return binding.type
        if expr.name in self.functions:
            return self.functions[expr.name]
        self._err("NAME001", f"unknown name {expr.name!r}", expr.span)
        return ERROR

    def _infer_unary(self, expr: ast.UnaryExpr) -> Type:
        operand = self._check_expr(expr.operand)
        if expr.op == "-":
            self._expect(I64, operand, expr.operand.span, "operand of unary `-`")
            return I64
        self._expect(BOOL, operand, expr.operand.span, "operand of `!`")
        return BOOL

    def _infer_binary(self, expr: ast.BinaryExpr) -> Type:
        left = self._check_expr(expr.left)
        right = self._check_expr(expr.right)
        op = expr.op
        if op in _ARITH:
            self._expect(I64, left, expr.left.span, f"left operand of `{op}`")
            self._expect(I64, right, expr.right.span, f"right operand of `{op}`")
            return I64
        if op in _COMPARE:
            self._expect(I64, left, expr.left.span, f"left operand of `{op}`")
            self._expect(I64, right, expr.right.span, f"right operand of `{op}`")
            return BOOL
        if op in _BOOLEAN:
            self._expect(BOOL, left, expr.left.span, f"left operand of `{op}`")
            self._expect(BOOL, right, expr.right.span, f"right operand of `{op}`")
            return BOOL
        if op in _EQUALITY:
            if not _same(left, right):
                self._err(
                    "TYPE003",
                    f"cannot compare {left} with {right}",
                    expr.span,
                )
            return BOOL
        return ERROR

    def _infer_call(self, expr: ast.CallExpr) -> Type:
        if isinstance(expr.callee, ast.NameExpr) and expr.callee.name in _BUILTINS:
            return self._check_builtin(expr.callee.name, expr)
        if isinstance(expr.callee, ast.NameExpr) and expr.callee.name in self.functions:
            fn_ty = self.functions[expr.callee.name]
            self._check_args(expr.callee.name, fn_ty, expr)
            return fn_ty.ret
        callee_ty = self._check_expr(expr.callee)
        if isinstance(callee_ty, FnType):
            self._check_args("call", callee_ty, expr)
            return callee_ty.ret
        if callee_ty is not ERROR:
            self._err("TYPE004", "expression is not callable", expr.callee.span)
        return ERROR

    def _check_args(self, name: str, fn_ty: FnType, call: ast.CallExpr) -> None:
        if len(call.args) != len(fn_ty.params):
            self._err(
                "TYPE005",
                f"{name!r} expects {len(fn_ty.params)} argument(s), got {len(call.args)}",
                call.span,
            )
        for arg, expected in zip(call.args, fn_ty.params, strict=False):
            actual = self._check_expr(arg)
            self._expect(expected, actual, arg.span, "argument")
        for extra in call.args[len(fn_ty.params) :]:
            self._check_expr(extra)

    def _check_builtin(self, name: str, call: ast.CallExpr) -> Type:
        if not self.in_test:
            self._err(
                "TEST001",
                f"{name}() can only be used inside a test block",
                call.span,
                help='move this into a `test "..." { ... }` block',
            )
        for arg in call.args:
            self._check_expr(arg)
        if name in ("assert",):
            if len(call.args) == 1:
                self._expect(
                    BOOL, self.expr_types[id(call.args[0])], call.args[0].span, "assertion"
                )
            else:
                self._err("TYPE006", "assert expects 1 argument", call.span)
        elif name in ("assert_eq", "assert_ne"):
            if len(call.args) != 2:
                self._err("TYPE006", f"{name} expects 2 arguments", call.span)
            else:
                a = self.expr_types[id(call.args[0])]
                b = self.expr_types[id(call.args[1])]
                if not _same(a, b):
                    self._err("TYPE003", f"cannot compare {a} with {b}", call.span)
        elif name in ("fail", "panic"):
            if len(call.args) != 1:
                self._err("TYPE006", f"{name} expects 1 argument", call.span)
            elif self.expr_types[id(call.args[0])] not in (STRING, ERROR):
                self._err("TYPE007", f"{name} expects a String message", call.args[0].span)
        return UNIT

    def _infer_if(self, expr: ast.IfExpr) -> Type:
        cond = self._check_expr(expr.cond)
        self._expect(BOOL, cond, expr.cond.span, "if condition")
        then_ty = self._check_block(expr.then_block)
        if expr.else_block is None:
            return UNIT
        else_ty = self._check_block(expr.else_block)
        if not _same(then_ty, else_ty):
            self._err(
                "TYPE008",
                f"if branches have mismatched types: {then_ty} vs {else_ty}",
                expr.span,
            )
            return ERROR
        return then_ty

    # --- helpers --------------------------------------------------------------

    def _resolve_type(self, type_expr: ast.TypeExpr) -> Type:
        if type_expr.name in PRIMITIVES and not type_expr.args:
            return PRIMITIVES[type_expr.name]
        span = type_expr.span
        self._err("TYPE001", f"unknown type {type_expr.name!r}", span)
        return ERROR

    def _expect(self, expected: Type, actual: Type, span: Span, what: str) -> None:
        if expected is ERROR or actual is ERROR:
            return
        if not _same(expected, actual):
            self._err("TYPE003", f"{what} has type {actual}, expected {expected}", span)

    def _err(self, code: str, message: str, span: Span | None, *, help: str | None = None) -> None:
        self.diags.append(Diagnostic(code, message, span, help=help))


def _same(a: Type, b: Type) -> bool:
    return a is ERROR or b is ERROR or a == b


def _diverges(block: ast.Block) -> bool:
    """Whether the block is guaranteed to return (so it needs no tail value)."""
    if not block.stmts:
        return False
    last = block.stmts[-1]
    if isinstance(last, ast.ReturnStmt):
        return True
    if isinstance(last, ast.ExprStmt) and isinstance(last.expr, ast.IfExpr):
        branch = last.expr
        return (
            branch.else_block is not None
            and _diverges(branch.then_block)
            and _diverges(branch.else_block)
        )
    return False


def check(module: ast.Module) -> CheckResult:
    return Checker(module).check()
