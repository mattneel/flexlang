"""Lower the typed AST to textual MLIR (func / arith / cf / memref dialects).

The emission is deliberately simple and structured so the standard
``mlir-opt --convert-to-llvm`` pipeline can take it to LLVM IR:

* immutable ``let`` bindings and parameters become SSA values;
* ``mut`` locals and ``if``-expression results become 0-d ``memref`` slots
  (alloca + load/store), so we never hand-write SSA phi/block arguments;
* ``if`` / ``while`` become ``cf`` branches between explicit blocks.

User functions are emitted as ``@flx_<name>`` to avoid clashing with the C
runtime shim (``main``, ``printf``, …). Tests are emitted by
:mod:`flx.backend.harness`.
"""

from __future__ import annotations

from dataclasses import dataclass

from flx.sema.check import CheckResult
from flx.syntax import ast
from flx.types import BOOL, I64, UNIT, FnType, Type

_ARITH_OP = {"+": "addi", "-": "subi", "*": "muli", "/": "divsi", "%": "remsi"}
_CMP_PRED = {"<": "slt", "<=": "sle", ">": "sgt", ">=": "sge", "==": "eq", "!=": "ne"}
_BOOL_OP = {"&&": "andi", "||": "ori"}
_BUILTINS = {"assert", "assert_eq", "assert_ne", "fail", "panic"}

# External runtime declarations, prepended when tests are emitted.
_RUNTIME_DECLS = (
    "func.func private @__flx_assert_fail()\n"
    "func.func private @__flx_assert_eq_fail(i64, i64)\n"
    "func.func private @__flx_assert_ne_fail(i64, i64)\n"
    "func.func private @__flx_explicit_fail()\n"
)


def mlir_type(ty: Type) -> str:
    if ty is I64:
        return "i64"
    if ty is BOOL:
        return "i1"
    raise BackendError(f"type {ty} has no MLIR representation yet")


class BackendError(Exception):
    """Raised when the AST uses a construct the MVP backend can't lower."""


@dataclass
class _Binding:
    kind: str  # "val" or "slot"
    ref: str  # SSA value, or memref slot name
    ty: str  # MLIR type (value type, or slot element type)


class FunctionLowerer:
    """Lowers a single function or test body into MLIR text lines."""

    def __init__(self, checked: CheckResult) -> None:
        self.types = checked.expr_types
        self.functions = checked.functions
        self.lines: list[str] = []
        self._n = 0
        self._b = 0
        self.scopes: list[dict[str, _Binding]] = [{}]
        self.terminated = False
        self.test_mode = False

    def _reset(self) -> None:
        self.lines = []
        self._n = 0
        self._b = 0
        self.scopes = [{}]
        self.terminated = False

    # --- emission helpers -----------------------------------------------------

    def _fresh(self) -> str:
        self._n += 1
        return f"%v{self._n}"

    def _label(self) -> str:
        self._b += 1
        return f"^bb{self._b}"

    def _emit(self, line: str) -> None:
        if not self.terminated:
            self.lines.append("  " + line)

    def _terminator(self, line: str) -> None:
        self._emit(line)
        self.terminated = True

    def _start_block(self, label: str) -> None:
        self.lines.append(f"{label}:")
        self.terminated = False

    def _ty_of(self, expr: ast.Expr) -> Type:
        return self.types[id(expr)]

    # --- scope ----------------------------------------------------------------

    def _define(self, name: str, binding: _Binding) -> None:
        self.scopes[-1][name] = binding

    def _lookup(self, name: str) -> _Binding:
        for frame in reversed(self.scopes):
            if name in frame:
                return frame[name]
        raise BackendError(f"unresolved name {name!r} reached the backend")

    # --- function entry -------------------------------------------------------

    def lower_function(self, fn: ast.FnDecl) -> str:
        fn_ty = self.functions[fn.name]
        return self._lower_callable(
            symbol=f"flx_{fn.name}",
            params=[(p.name, t) for p, t in zip(fn.params, fn_ty.params, strict=True)],
            ret=fn_ty.ret,
            body=fn.body,
        )

    def _lower_callable(
        self,
        symbol: str,
        params: list[tuple[str, Type]],
        ret: Type,
        body: ast.Block,
    ) -> str:
        self._reset()
        self.test_mode = False

        sig_parts = []
        for i, (pname, pty) in enumerate(params):
            arg = f"%arg{i}"
            mty = mlir_type(pty)
            sig_parts.append(f"{arg}: {mty}")
            self._define(pname, _Binding("val", arg, mty))
        sig = ", ".join(sig_parts)

        ret_str = "" if ret is UNIT else f" -> {mlir_type(ret)}"
        tail = self.lower_block(body)
        if not self.terminated:
            if ret is UNIT:
                self._terminator("func.return")
            else:
                assert tail is not None
                self._terminator(f"func.return {tail} : {mlir_type(ret)}")

        header = f"func.func @{symbol}({sig}){ret_str} {{"
        return "\n".join([header, *self.lines, "}"])

    def lower_test(self, test: ast.TestDecl, index: int) -> str:
        self._reset()
        self.test_mode = True
        self.lower_block(test.body)
        if not self.terminated:
            zero = self._const("0", "i32")
            self._terminator(f"func.return {zero} : i32")
        header = f"func.func @flx_test_{index}() -> i32 {{"
        return "\n".join([header, *self.lines, "}"])

    # --- blocks / statements --------------------------------------------------

    def lower_block(self, block: ast.Block) -> str | None:
        self.scopes.append({})
        tail: str | None = None
        last = block.stmts[-1] if block.stmts else None
        for stmt in block.stmts:
            value = self.lower_stmt(stmt)
            if stmt is last and isinstance(stmt, ast.ExprStmt):
                tail = value
        self.scopes.pop()
        return tail

    def lower_stmt(self, stmt: ast.Stmt) -> str | None:
        if isinstance(stmt, ast.LetStmt):
            value = self.lower_expr(stmt.value)
            if value is not None:
                self._define(stmt.name, _Binding("val", value, mlir_type(self._ty_of(stmt.value))))
            return None
        if isinstance(stmt, ast.MutStmt):
            value = self.lower_expr(stmt.value)
            mty = mlir_type(self._ty_of(stmt.value))
            slot = self._fresh()
            self._emit(f"{slot} = memref.alloca() : memref<{mty}>")
            self._emit(f"memref.store {value}, {slot}[] : memref<{mty}>")
            self._define(stmt.name, _Binding("slot", slot, mty))
            return None
        if isinstance(stmt, ast.AssignStmt):
            value = self.lower_expr(stmt.value)
            binding = self._lookup(stmt.name)
            self._emit(f"memref.store {value}, {binding.ref}[] : memref<{binding.ty}>")
            return None
        if isinstance(stmt, ast.WhileStmt):
            self.lower_while(stmt)
            return None
        if isinstance(stmt, ast.ReturnStmt):
            if stmt.value is None:
                self._terminator("func.return")
            else:
                value = self.lower_expr(stmt.value)
                self._terminator(f"func.return {value} : {mlir_type(self._ty_of(stmt.value))}")
            return None
        if isinstance(stmt, ast.ExprStmt):
            return self.lower_expr(stmt.expr)
        return None

    def lower_while(self, stmt: ast.WhileStmt) -> None:
        cond_lbl, body_lbl, exit_lbl = self._label(), self._label(), self._label()
        self._terminator(f"cf.br {cond_lbl}")
        self._start_block(cond_lbl)
        cond = self.lower_expr(stmt.cond)
        self._terminator(f"cf.cond_br {cond}, {body_lbl}, {exit_lbl}")
        self._start_block(body_lbl)
        self.lower_block(stmt.body)
        self._terminator(f"cf.br {cond_lbl}")
        self._start_block(exit_lbl)

    # --- expressions ----------------------------------------------------------

    def lower_expr(self, expr: ast.Expr) -> str | None:
        if isinstance(expr, ast.IntLit):
            return self._const(str(expr.value), "i64")
        if isinstance(expr, ast.BoolLit):
            return self._const("1" if expr.value else "0", "i1")
        if isinstance(expr, ast.NameExpr):
            return self._lower_name(expr)
        if isinstance(expr, ast.UnaryExpr):
            return self._lower_unary(expr)
        if isinstance(expr, ast.BinaryExpr):
            return self._lower_binary(expr)
        if isinstance(expr, ast.CallExpr):
            return self._lower_call(expr)
        if isinstance(expr, ast.IfExpr):
            return self._lower_if(expr)
        raise BackendError(f"cannot lower expression {type(expr).__name__}")

    def _const(self, literal: str, mty: str) -> str:
        out = self._fresh()
        self._emit(f"{out} = arith.constant {literal} : {mty}")
        return out

    def _lower_name(self, expr: ast.NameExpr) -> str:
        binding = self._lookup(expr.name)
        if binding.kind == "val":
            return binding.ref
        out = self._fresh()
        self._emit(f"{out} = memref.load {binding.ref}[] : memref<{binding.ty}>")
        return out

    def _lower_unary(self, expr: ast.UnaryExpr) -> str:
        operand = self.lower_expr(expr.operand)
        out = self._fresh()
        if expr.op == "-":
            zero = self._const("0", "i64")
            self._emit(f"{out} = arith.subi {zero}, {operand} : i64")
        else:
            one = self._const("1", "i1")
            self._emit(f"{out} = arith.xori {operand}, {one} : i1")
        return out

    def _lower_binary(self, expr: ast.BinaryExpr) -> str:
        left = self.lower_expr(expr.left)
        right = self.lower_expr(expr.right)
        out = self._fresh()
        op = expr.op
        if op in _ARITH_OP:
            self._emit(f"{out} = arith.{_ARITH_OP[op]} {left}, {right} : i64")
        elif op in _BOOL_OP:
            self._emit(f"{out} = arith.{_BOOL_OP[op]} {left}, {right} : i1")
        elif op in _CMP_PRED:
            operand_ty = mlir_type(self._ty_of(expr.left))
            self._emit(f"{out} = arith.cmpi {_CMP_PRED[op]}, {left}, {right} : {operand_ty}")
        else:
            raise BackendError(f"unknown operator {op!r}")
        return out

    def _lower_call(self, expr: ast.CallExpr) -> str | None:
        if not isinstance(expr.callee, ast.NameExpr):
            raise BackendError("only direct function calls are supported")
        name = expr.callee.name
        if name in _BUILTINS:
            self._lower_builtin(name, expr)
            return None
        fn_ty = self.functions.get(name)
        if fn_ty is None:
            raise BackendError(f"call to non-function {name!r}")
        args = [self.lower_expr(a) for a in expr.args]
        return self._emit_call(f"flx_{name}", args, fn_ty)

    def _lower_builtin(self, name: str, call: ast.CallExpr) -> None:
        if not self.test_mode:
            raise BackendError(f"{name}() can only be used inside a test")
        if name == "assert":
            cond = self.lower_expr(call.args[0])
            assert cond is not None
            self._assert_branch(cond, "@__flx_assert_fail", "", "")
        elif name in ("assert_eq", "assert_ne"):
            left = self.lower_expr(call.args[0])
            right = self.lower_expr(call.args[1])
            assert left is not None and right is not None
            operand_ty = mlir_type(self._ty_of(call.args[0]))
            pred = "eq" if name == "assert_eq" else "ne"
            cond = self._fresh()
            self._emit(f"{cond} = arith.cmpi {pred}, {left}, {right} : {operand_ty}")
            a64 = self._to_i64(left, operand_ty)
            b64 = self._to_i64(right, operand_ty)
            sym = "@__flx_assert_eq_fail" if name == "assert_eq" else "@__flx_assert_ne_fail"
            self._assert_branch(cond, sym, f"{a64}, {b64}", "i64, i64")
        else:  # fail / panic always fail
            self._emit("func.call @__flx_explicit_fail() : () -> ()")
            one = self._const("1", "i32")
            self._terminator(f"func.return {one} : i32")
        return None

    def _assert_branch(self, ok_cond: str, fail_sym: str, args: str, arg_types: str) -> None:
        ok_lbl, fail_lbl = self._label(), self._label()
        self._terminator(f"cf.cond_br {ok_cond}, {ok_lbl}, {fail_lbl}")
        self._start_block(fail_lbl)
        self._emit(f"func.call {fail_sym}({args}) : ({arg_types}) -> ()")
        one = self._const("1", "i32")
        self._terminator(f"func.return {one} : i32")
        self._start_block(ok_lbl)

    def _to_i64(self, value: str, ty: str) -> str:
        if ty == "i64":
            return value
        out = self._fresh()
        self._emit(f"{out} = arith.extui {value} : i1 to i64")
        return out

    def _emit_call(self, symbol: str, args: list[str | None], fn_ty: FnType) -> str | None:
        arg_list = ", ".join(a for a in args if a is not None)
        arg_types = ", ".join(mlir_type(t) for t in fn_ty.params)
        if fn_ty.ret is UNIT:
            self._emit(f"func.call @{symbol}({arg_list}) : ({arg_types}) -> ()")
            return None
        out = self._fresh()
        ret = mlir_type(fn_ty.ret)
        self._emit(f"{out} = func.call @{symbol}({arg_list}) : ({arg_types}) -> {ret}")
        return out

    def _lower_if(self, expr: ast.IfExpr) -> str | None:
        result_ty = self._ty_of(expr)
        has_value = result_ty is not UNIT and expr.else_block is not None
        slot: str | None = None
        if has_value:
            mty = mlir_type(result_ty)
            slot = self._fresh()
            self._emit(f"{slot} = memref.alloca() : memref<{mty}>")

        cond = self.lower_expr(expr.cond)
        then_lbl = self._label()
        join_lbl = self._label()
        else_lbl = self._label() if expr.else_block is not None else join_lbl
        self._terminator(f"cf.cond_br {cond}, {then_lbl}, {else_lbl}")

        self._start_block(then_lbl)
        then_val = self.lower_block(expr.then_block)
        if slot is not None and then_val is not None:
            self._emit(f"memref.store {then_val}, {slot}[] : memref<{mlir_type(result_ty)}>")
        self._terminator(f"cf.br {join_lbl}")

        if expr.else_block is not None:
            self._start_block(else_lbl)
            else_val = self.lower_block(expr.else_block)
            if slot is not None and else_val is not None:
                self._emit(f"memref.store {else_val}, {slot}[] : memref<{mlir_type(result_ty)}>")
            self._terminator(f"cf.br {join_lbl}")

        self._start_block(join_lbl)
        if slot is not None:
            out = self._fresh()
            self._emit(f"{out} = memref.load {slot}[] : memref<{mlir_type(result_ty)}>")
            return out
        return None


def emit_program(checked: CheckResult, *, with_tests: bool) -> str:
    """Emit MLIR for all functions, optionally including ``@flx_test_<i>``."""
    lowerer = FunctionLowerer(checked)
    parts = [lowerer.lower_function(fn) for fn in checked.module.functions]
    if with_tests:
        for i, test in enumerate(checked.module.tests):
            parts.append(lowerer.lower_test(test, i))
    body = "\n".join(parts) + "\n"
    return _RUNTIME_DECLS + body if with_tests else body


def emit_module(checked: CheckResult) -> str:
    """Emit MLIR for all functions in the module (tests excluded)."""
    return emit_program(checked, with_tests=False)
