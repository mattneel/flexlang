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

import struct
from dataclasses import dataclass

from flx.backend.runtime import BASE_RUNTIME_DECLS
from flx.sema.check import CheckResult
from flx.syntax import ast
from flx.types import (
    BOOL,
    F64,
    I64,
    STRING,
    UNIT,
    AdtType,
    FnType,
    ListType,
    MapType,
    RecordType,
    Type,
)

_ARITH_OP = {"+": "addi", "-": "subi", "*": "muli"}
_BIT_OP = {"&": "andi", "|": "ori", "^": "xori"}
# Float arithmetic is plain IEEE-754 (divf/remf yield inf/nan, no traps), so it
# needs no guarded runtime calls. Comparisons are ORDERED (false on NaN), and
# != lowers as !(oeq) — i.e. une — matching the interpreter and C.
_FARITH_OP = {"+": "addf", "-": "subf", "*": "mulf", "/": "divf", "%": "remf"}
_FCMP_PRED = {"<": "olt", "<=": "ole", ">": "ogt", ">=": "oge"}
# `/` and `%` go through guarded runtime calls (see runtime.py): raw arith.divsi /
# arith.remsi are UB on a zero divisor and on INT64_MIN / -1.
_DIV_OP = {"/": "@__flx_idiv", "%": "@__flx_imod"}
_CMP_PRED = {"<": "slt", "<=": "sle", ">": "sgt", ">=": "sge", "==": "eq", "!=": "ne"}
_BUILTINS = {"assert", "assert_eq", "assert_ne", "fail", "panic"}

# External runtime declarations, prepended when tests are emitted.
_RUNTIME_DECLS = (
    "func.func private @__flx_assert_fail()\n"
    "func.func private @__flx_assert_eq_fail(i64, i64)\n"
    "func.func private @__flx_assert_ne_fail(i64, i64)\n"
    "func.func private @__flx_assert_feq_fail(f64, f64)\n"
    "func.func private @__flx_assert_fne_fail(f64)\n"
    "func.func private @__flx_assert_streq_fail(!llvm.ptr, i64, !llvm.ptr, i64)\n"
    "func.func private @__flx_assert_strne_fail(!llvm.ptr, i64)\n"
    "func.func private @__flx_explicit_fail()\n"
    "func.func private @__flx_fail_msg(!llvm.ptr, i64)\n"
)


def mlir_type(ty: Type) -> str:
    if ty is I64:
        return "i64"
    if ty is F64:
        return "f64"
    if ty is BOOL:
        return "i1"
    if ty is UNIT:
        # Unit-returning functions stay void (see _lower_callable), but a unit
        # value that is stored or passed materializes as the constant i64 0.
        return "i64"
    if ty is STRING:
        return "!llvm.struct<(ptr, i64)>"
    if isinstance(ty, RecordType):
        fields = ", ".join(mlir_type(t) for _, t in ty.fields)
        return f"!llvm.struct<({fields})>"
    if isinstance(ty, AdtType):
        # Payloadless enum: just the tag. Otherwise {i32 tag, i64 widened payload}.
        if _is_enum(ty):
            return "i64"
        return "!llvm.struct<(i32, i64)>"
    if isinstance(ty, ListType):
        return "!llvm.ptr"  # a heap header: {i64 len, i64 cap, i64* data}
    if isinstance(ty, MapType):
        return "!llvm.ptr"  # a heap header: FlxMap (insertion-ordered entries)
    if isinstance(ty, FnType):
        params = ", ".join(mlir_type(t) for t in ty.params)
        ret = "()" if ty.ret is UNIT else mlir_type(ty.ret)
        return f"({params}) -> {ret}"
    raise BackendError(f"type {ty} has no MLIR representation yet")


def _is_aggregate(mty: str) -> bool:
    return mty.startswith("!llvm")


def _is_enum(ty: AdtType) -> bool:
    return all(not v.payload for v in ty.variants)


def _payload_inline(ty: Type) -> bool:
    """Whether a payload of this type lives in the i64 slot by value. Everything
    else (strings, records, non-enum ADTs — recursion included — and any
    multi-field payload) is boxed on the heap behind the slot. Lists are already
    heap pointers, so the slot holds the pointer itself (note: that makes them
    storable, NOT comparable — the checker still rejects `==` on lists)."""
    return (
        ty is I64
        or ty is F64
        or ty is BOOL
        or ty is UNIT
        or isinstance(ty, (ListType, MapType))
        or (isinstance(ty, AdtType) and _is_enum(ty))
    )


def _payload_box_type(payload: tuple[Type, ...]) -> str:
    """The MLIR type stored in a boxed payload's heap cell: the value itself for
    one field, a struct of the fields otherwise."""
    if len(payload) == 1:
        return mlir_type(payload[0])
    fields = ", ".join(mlir_type(t) for t in payload)
    return f"!llvm.struct<({fields})>"


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
        self.checked = checked
        self.types = checked.expr_types
        self.functions = checked.functions
        self.constructors = checked.constructors
        self.method_targets = checked.method_targets
        self.extern_fns = checked.extern_fns
        self.extern_abi = checked.extern_abi
        self.globals: list[str] = []  # module-level string constants (not reset)
        self._str_count = 0
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

    def _materialize(self, value: str | None, expr: ast.Expr) -> str:
        """A value for `expr` where one is demanded (stored, passed, compared).
        Unit expressions lower to no value; here they become the i64 0 that
        `mlir_type(UNIT)` promises."""
        if value is not None:
            return value
        if self._ty_of(expr) is UNIT:
            return self._const("0", "i64")
        raise BackendError(f"cannot lower expression {type(expr).__name__} as a value")

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
            elif tail is not None:
                self._terminator(f"func.return {tail} : {mlir_type(ret)}")
            else:
                # Reachable only at an unreachable join (e.g. if/else where both
                # branches return). The checker guarantees the function diverges;
                # emit a default return so the MLIR stays well-formed.
                rt = mlir_type(ret)
                if _is_aggregate(rt):
                    dummy = self._fresh()
                    self._emit(f"{dummy} = llvm.mlir.undef : {rt}")
                else:
                    dummy = self._const("0", rt)
                self._terminator(f"func.return {dummy} : {rt}")

        header = f"func.func @{symbol}({sig}){ret_str} {{"
        return "\n".join([header, *self.lines, "}"])

    def lower_test(self, test: ast.TestDecl, index: int) -> str:
        self._reset()
        self.test_mode = True
        self.lower_block(test.body)
        if not self.terminated:
            zero = self._const("0", "i32")
            self._terminator(f"func.return {zero} : i32")
        header = f"func.func @__flx_test_{index}() -> i32 {{"
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
            value = self._materialize(self.lower_expr(stmt.value), stmt.value)
            self._define(stmt.name, _Binding("val", value, mlir_type(self._ty_of(stmt.value))))
            return None
        if isinstance(stmt, ast.MutStmt):
            value = self._materialize(self.lower_expr(stmt.value), stmt.value)
            mty = mlir_type(self._ty_of(stmt.value))
            slot = self._alloc_slot(mty)
            self._store_slot(slot, mty, value)
            self._define(stmt.name, _Binding("slot", slot, mty))
            return None
        if isinstance(stmt, ast.AssignStmt):
            value = self._materialize(self.lower_expr(stmt.value), stmt.value)
            binding = self._lookup(stmt.name)
            self._store_slot(binding.ref, binding.ty, value)
            return None
        if isinstance(stmt, ast.IndexAssignStmt):
            obj_ty = self._ty_of(stmt.obj)
            assert isinstance(obj_ty, ListType)
            lst = self._materialize(self.lower_expr(stmt.obj), stmt.obj)
            idx = self._materialize(self.lower_expr(stmt.index), stmt.index)
            value = self._materialize(self.lower_expr(stmt.value), stmt.value)
            slot = self._encode_elem(value, obj_ty.elem)
            self._emit(
                f"func.call @__flx_list_set({lst}, {idx}, {slot}) : (!llvm.ptr, i64, i64) -> ()"
            )
            return None
        if isinstance(stmt, ast.WhileStmt):
            self.lower_while(stmt)
            return None
        if isinstance(stmt, ast.ForStmt):
            self._lower_for(stmt)
            return None
        if isinstance(stmt, ast.ReturnStmt):
            if stmt.value is None:
                self._terminator("func.return")
            elif self._ty_of(stmt.value) is UNIT:
                # `return unit_expr` in a void function: evaluate for effects.
                self.lower_expr(stmt.value)
                self._terminator("func.return")
            else:
                value = self._materialize(self.lower_expr(stmt.value), stmt.value)
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
        if isinstance(expr, ast.FloatLit):
            bits = struct.unpack("<Q", struct.pack("<d", expr.value))[0]
            return self._const(f"0x{bits:016X}", "f64")  # hex = exact raw bits
        if isinstance(expr, ast.BoolLit):
            return self._const("1" if expr.value else "0", "i1")
        if isinstance(expr, ast.UnitLit):
            return None
        if isinstance(expr, ast.StringLit):
            return self._lower_string(expr)
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
        if isinstance(expr, ast.RegionExpr):
            # Shallow MVP: a region just evaluates its body inline (no real
            # scoped allocation for scalar values).
            return self.lower_block(expr.body)
        if isinstance(expr, ast.BlockExpr):
            return self.lower_block(expr.body)
        if isinstance(expr, ast.RecordExpr):
            return self._lower_record(expr)
        if isinstance(expr, ast.RecordUpdateExpr):
            return self._lower_record_update(expr)
        if isinstance(expr, ast.MemberExpr):
            return self._lower_member(expr)
        if isinstance(expr, ast.MatchExpr):
            return self._lower_match(expr)
        if isinstance(expr, ast.TryExpr):
            return self._lower_try(expr)
        if isinstance(expr, ast.ListExpr):
            return self._lower_list(expr)
        if isinstance(expr, ast.IndexExpr):
            return self._lower_index(expr)
        raise BackendError(f"cannot lower expression {type(expr).__name__}")

    def _const(self, literal: str, mty: str) -> str:
        out = self._fresh()
        self._emit(f"{out} = arith.constant {literal} : {mty}")
        return out

    # --- strings: {ptr, i64 length} backed by a module-level global -----------

    def _lower_string(self, expr: ast.StringLit) -> str:
        # surrogateescape: \xNN escapes >= 0x80 cook to surrogates in the AST
        # value; emission turns them back into the raw bytes they stand for.
        data = expr.value.encode("utf-8", "surrogateescape")
        name = f"@__flx_str_{self._str_count}"
        self._str_count += 1
        escaped = "".join(
            chr(b) if 0x20 <= b < 0x7F and b not in (0x22, 0x5C) else f"\\{b:02X}"
            for b in data + b"\x00"
        )
        self.globals.append(
            f'llvm.mlir.global private constant {name}("{escaped}") '
            f"{{addr_space = 0 : i32}} : !llvm.array<{len(data) + 1} x i8>"
        )
        mty = "!llvm.struct<(ptr, i64)>"
        ptr = self._fresh()
        self._emit(f"{ptr} = llvm.mlir.addressof {name} : !llvm.ptr")
        length = self._const(str(len(data)), "i64")
        undef = self._fresh()
        self._emit(f"{undef} = llvm.mlir.undef : {mty}")
        with_ptr = self._fresh()
        self._emit(f"{with_ptr} = llvm.insertvalue {ptr}, {undef}[0] : {mty}")
        out = self._fresh()
        self._emit(f"{out} = llvm.insertvalue {length}, {with_ptr}[1] : {mty}")
        return out

    def _string_parts(self, value: str) -> tuple[str, str]:
        """Lower a String expression to its (ptr, length) SSA values."""
        mty = "!llvm.struct<(ptr, i64)>"
        ptr = self._fresh()
        self._emit(f"{ptr} = llvm.extractvalue {value}[0] : {mty}")
        length = self._fresh()
        self._emit(f"{length} = llvm.extractvalue {value}[1] : {mty}")
        return ptr, length

    def _str_runtime(self, symbol: str, in_types: list[str], in_args: list[str]) -> str:
        """Call a string-producing runtime fn via an out-pointer (sret-style)."""
        one = self._fresh()
        self._emit(f"{one} = llvm.mlir.constant(1 : i64) : i64")
        slot = self._fresh()
        self._emit(f"{slot} = llvm.alloca {one} x !llvm.struct<(ptr, i64)> : (i64) -> !llvm.ptr")
        args = ", ".join([*in_args, slot])
        types = ", ".join([*in_types, "!llvm.ptr"])
        self._emit(f"func.call @{symbol}({args}) : ({types}) -> ()")
        out = self._fresh()
        self._emit(f"{out} = llvm.load {slot} : !llvm.ptr -> !llvm.struct<(ptr, i64)>")
        return out

    def _lower_read_line(self) -> str:
        """Fs.read_line() -> Option<String>: the runtime reports EOF as a flag,
        and the Option {i32 tag, i64 slot} is assembled here exactly as a
        Some(s)/None constructor pair would build it (None=0, Some=1; a String
        payload is a boxed {ptr, len} cell)."""
        str_mty = "!llvm.struct<(ptr, i64)>"
        one = self._fresh()
        self._emit(f"{one} = llvm.mlir.constant(1 : i64) : i64")
        slot = self._fresh()
        self._emit(f"{slot} = llvm.alloca {one} x {str_mty} : (i64) -> !llvm.ptr")
        got = self._fresh()
        self._emit(f"{got} = func.call @__flx_read_line_opt({slot}) : (!llvm.ptr) -> i64")
        s = self._fresh()
        self._emit(f"{s} = llvm.load {slot} : !llvm.ptr -> {str_mty}")
        boxed = self._box(s, str_mty)
        zero = self._const("0", "i64")
        is_some = self._fresh()
        self._emit(f"{is_some} = arith.cmpi ne, {got}, {zero} : i64")
        some_tag = self._const("1", "i32")
        none_tag = self._const("0", "i32")
        tag = self._fresh()
        self._emit(f"{tag} = arith.select {is_some}, {some_tag}, {none_tag} : i32")
        payload = self._fresh()
        self._emit(f"{payload} = arith.select {is_some}, {boxed}, {zero} : i64")
        opt_mty = "!llvm.struct<(i32, i64)>"
        undef = self._fresh()
        self._emit(f"{undef} = llvm.mlir.undef : {opt_mty}")
        with_tag = self._fresh()
        self._emit(f"{with_tag} = llvm.insertvalue {tag}, {undef}[0] : {opt_mty}")
        out = self._fresh()
        self._emit(f"{out} = llvm.insertvalue {payload}, {with_tag}[1] : {opt_mty}")
        return out

    def _result_from_string_flag(self, ok: str, payload: str, *, ok_has_payload: bool) -> str:
        """Assemble Result<T, String> from an i64 success flag and a String.

        When ok_has_payload is true the String is Ok's payload
        (Result<String, String>, used by read_text). Otherwise the String is the
        Err payload and Ok carries Unit (Result<Unit, String>, used by
        write_text).
        """
        str_mty = "!llvm.struct<(ptr, i64)>"
        boxed = self._box(payload, str_mty)
        zero64 = self._const("0", "i64")
        is_ok = self._fresh()
        self._emit(f"{is_ok} = arith.cmpi ne, {ok}, {zero64} : i64")
        ok_tag = self._const("0", "i32")
        err_tag = self._const("1", "i32")
        tag = self._fresh()
        self._emit(f"{tag} = arith.select {is_ok}, {ok_tag}, {err_tag} : i32")
        if ok_has_payload:
            payload_slot = boxed
        else:
            payload_slot = self._fresh()
            self._emit(f"{payload_slot} = arith.select {is_ok}, {zero64}, {boxed} : i64")
        result_mty = "!llvm.struct<(i32, i64)>"
        undef = self._fresh()
        self._emit(f"{undef} = llvm.mlir.undef : {result_mty}")
        with_tag = self._fresh()
        self._emit(f"{with_tag} = llvm.insertvalue {tag}, {undef}[0] : {result_mty}")
        out = self._fresh()
        self._emit(f"{out} = llvm.insertvalue {payload_slot}, {with_tag}[1] : {result_mty}")
        return out

    def _lower_read_text(self, expr: ast.CallExpr) -> str:
        path = self._materialize(self.lower_expr(expr.args[0]), expr.args[0])
        pptr, plen = self._string_parts(path)
        str_mty = "!llvm.struct<(ptr, i64)>"
        one = self._fresh()
        self._emit(f"{one} = llvm.mlir.constant(1 : i64) : i64")
        slot = self._fresh()
        self._emit(f"{slot} = llvm.alloca {one} x {str_mty} : (i64) -> !llvm.ptr")
        ok = self._fresh()
        self._emit(
            f"{ok} = func.call @__flx_read_text({pptr}, {plen}, {slot}) : "
            "(!llvm.ptr, i64, !llvm.ptr) -> i64"
        )
        payload = self._fresh()
        self._emit(f"{payload} = llvm.load {slot} : !llvm.ptr -> {str_mty}")
        return self._result_from_string_flag(ok, payload, ok_has_payload=True)

    def _lower_write_text(self, expr: ast.CallExpr) -> str:
        path = self._materialize(self.lower_expr(expr.args[0]), expr.args[0])
        text = self._materialize(self.lower_expr(expr.args[1]), expr.args[1])
        pptr, plen = self._string_parts(path)
        tptr, tlen = self._string_parts(text)
        str_mty = "!llvm.struct<(ptr, i64)>"
        one = self._fresh()
        self._emit(f"{one} = llvm.mlir.constant(1 : i64) : i64")
        slot = self._fresh()
        self._emit(f"{slot} = llvm.alloca {one} x {str_mty} : (i64) -> !llvm.ptr")
        ok = self._fresh()
        self._emit(
            f"{ok} = func.call @__flx_write_text({pptr}, {plen}, {tptr}, {tlen}, {slot}) : "
            "(!llvm.ptr, i64, !llvm.ptr, i64, !llvm.ptr) -> i64"
        )
        err = self._fresh()
        self._emit(f"{err} = llvm.load {slot} : !llvm.ptr -> {str_mty}")
        return self._result_from_string_flag(ok, err, ok_has_payload=False)

    # --- slots: memref for scalars, llvm.alloca for aggregates ----------------

    def _alloc_slot(self, mty: str) -> str:
        slot = self._fresh()
        if _is_aggregate(mty):
            one = self._fresh()
            self._emit(f"{one} = llvm.mlir.constant(1 : i64) : i64")
            self._emit(f"{slot} = llvm.alloca {one} x {mty} : (i64) -> !llvm.ptr")
        else:
            self._emit(f"{slot} = memref.alloca() : memref<{mty}>")
        return slot

    def _store_slot(self, slot: str, mty: str, value: str) -> None:
        if _is_aggregate(mty):
            self._emit(f"llvm.store {value}, {slot} : {mty}, !llvm.ptr")
        else:
            self._emit(f"memref.store {value}, {slot}[] : memref<{mty}>")

    def _load_slot(self, slot: str, mty: str) -> str:
        out = self._fresh()
        if _is_aggregate(mty):
            self._emit(f"{out} = llvm.load {slot} : !llvm.ptr -> {mty}")
        else:
            self._emit(f"{out} = memref.load {slot}[] : memref<{mty}>")
        return out

    def _lower_name(self, expr: ast.NameExpr) -> str:
        if expr.name in self.constructors:  # bare variant, e.g. None / Red
            adt = self._ty_of(expr)
            assert isinstance(adt, AdtType)
            return self._lower_ctor(adt, expr.name, [])
        ty = self.types.get(id(expr))
        if isinstance(ty, FnType) and expr.name in self.functions:
            local = next((f for f in self.scopes if expr.name in f), None)
            if local is None:  # a bare (pure) top-level function reference
                out = self._fresh()
                self._emit(f"{out} = func.constant @flx_{expr.name} : {mlir_type(ty)}")
                return out
        binding = self._lookup(expr.name)
        if binding.kind == "val":
            return binding.ref
        return self._load_slot(binding.ref, binding.ty)

    # --- records --------------------------------------------------------------

    def _lower_record(self, expr: ast.RecordExpr) -> str:
        rt = self._ty_of(expr)
        assert isinstance(rt, RecordType)
        mty = mlir_type(rt)
        values = {f.name: self._materialize(self.lower_expr(f.value), f.value) for f in expr.fields}
        cur = self._fresh()
        self._emit(f"{cur} = llvm.mlir.undef : {mty}")
        for i, (fname, _) in enumerate(rt.fields):
            nxt = self._fresh()
            self._emit(f"{nxt} = llvm.insertvalue {values[fname]}, {cur}[{i}] : {mty}")
            cur = nxt
        return cur

    def _lower_record_update(self, expr: ast.RecordUpdateExpr) -> str:
        rt = self._ty_of(expr)
        assert isinstance(rt, RecordType)
        mty = mlir_type(rt)
        index_of = {n: i for i, (n, _) in enumerate(rt.fields)}
        cur = self.lower_expr(expr.base)
        assert cur is not None
        for f in expr.fields:
            value = self._materialize(self.lower_expr(f.value), f.value)
            nxt = self._fresh()
            self._emit(f"{nxt} = llvm.insertvalue {value}, {cur}[{index_of[f.name]}] : {mty}")
            cur = nxt
        return cur

    def _lower_member(self, expr: ast.MemberExpr) -> str:
        # `Type.Variant` path access to a constructor (e.g. MathError.DivideByZero).
        if expr.name in self.constructors:
            adt = self._ty_of(expr)
            assert isinstance(adt, AdtType)
            return self._lower_ctor(adt, expr.name, [])
        obj = self.lower_expr(expr.obj)
        obj_ty = self._ty_of(expr.obj)
        assert isinstance(obj_ty, RecordType)
        mty = mlir_type(obj_ty)
        index = next(i for i, (n, _) in enumerate(obj_ty.fields) if n == expr.name)
        out = self._fresh()
        self._emit(f"{out} = llvm.extractvalue {obj}[{index}] : {mty}")
        return out

    # --- ADTs / constructors / match / `?` ------------------------------------

    def _lower_ctor(self, adt: AdtType, vname: str, args: list[ast.Expr]) -> str:
        vidx = next(i for i, v in enumerate(adt.variants) if v.name == vname)
        if _is_enum(adt):
            return self._const(str(vidx), "i64")
        mty = mlir_type(adt)
        payload = self._encode_payload(args, adt.variants[vidx].payload)
        tag = self._const(str(vidx), "i32")
        undef = self._fresh()
        self._emit(f"{undef} = llvm.mlir.undef : {mty}")
        with_tag = self._fresh()
        self._emit(f"{with_tag} = llvm.insertvalue {tag}, {undef}[0] : {mty}")
        out = self._fresh()
        self._emit(f"{out} = llvm.insertvalue {payload}, {with_tag}[1] : {mty}")
        return out

    # --- the payload slot codec -------------------------------------------------
    # A non-enum ADT is {i32 tag, i64 slot}. The slot holds an inline scalar by
    # value, or the address of a heap box (__flx_box) for everything else.

    def _encode_payload(self, args: list[ast.Expr], payload: tuple[Type, ...]) -> str:
        if not args:
            return self._const("0", "i64")
        if len(payload) == 1 and _payload_inline(payload[0]):
            raw = self._materialize(self.lower_expr(args[0]), args[0])
            return self._widen_to_i64(raw, payload[0])
        box_mty = _payload_box_type(payload)
        if len(payload) == 1:
            value = self._materialize(self.lower_expr(args[0]), args[0])
        else:
            value = self._fresh()
            self._emit(f"{value} = llvm.mlir.undef : {box_mty}")
            for i, arg in enumerate(args):
                field = self._materialize(self.lower_expr(arg), arg)
                nxt = self._fresh()
                self._emit(f"{nxt} = llvm.insertvalue {field}, {value}[{i}] : {box_mty}")
                value = nxt
        return self._box(value, box_mty)

    def _decode_payload_field(self, slot: str, payload: tuple[Type, ...], index: int) -> str:
        """Field `index` of a variant payload, given the i64 slot."""
        if len(payload) == 1:
            return self._decode_elem(slot, payload[0])
        box_mty = _payload_box_type(payload)
        struct = self._unbox(slot, box_mty)
        out = self._fresh()
        self._emit(f"{out} = llvm.extractvalue {struct}[{index}] : {box_mty}")
        return out

    def _encode_elem(self, value: str, ty: Type) -> str:
        """A value as an i64 slot (list elements share the payload codec)."""
        if _payload_inline(ty):
            return self._widen_to_i64(value, ty)
        return self._box(value, mlir_type(ty))

    def _decode_elem(self, slot: str, ty: Type) -> str:
        if _payload_inline(ty):
            return self._narrow_from_i64(slot, ty)
        return self._unbox(slot, mlir_type(ty))

    def _sizeof(self, mty: str) -> str:
        """sizeof(mty) in bytes via the null-GEP idiom (target layout aware)."""
        null = self._fresh()
        self._emit(f"{null} = llvm.mlir.zero : !llvm.ptr")
        gep = self._fresh()
        self._emit(f"{gep} = llvm.getelementptr {null}[1] : (!llvm.ptr) -> !llvm.ptr, {mty}")
        size = self._fresh()
        self._emit(f"{size} = llvm.ptrtoint {gep} : !llvm.ptr to i64")
        return size

    def _box(self, value: str, mty: str) -> str:
        size = self._sizeof(mty)
        ptr = self._fresh()
        self._emit(f"{ptr} = func.call @__flx_box({size}) : (i64) -> !llvm.ptr")
        self._emit(f"llvm.store {value}, {ptr} : {mty}, !llvm.ptr")
        slot = self._fresh()
        self._emit(f"{slot} = llvm.ptrtoint {ptr} : !llvm.ptr to i64")
        return slot

    def _unbox(self, slot: str, mty: str) -> str:
        ptr = self._fresh()
        self._emit(f"{ptr} = llvm.inttoptr {slot} : i64 to !llvm.ptr")
        out = self._fresh()
        self._emit(f"{out} = llvm.load {ptr} : !llvm.ptr -> {mty}")
        return out

    def _widen_to_i64(self, value: str, ty: Type) -> str:
        if ty is BOOL:
            out = self._fresh()
            self._emit(f"{out} = arith.extui {value} : i1 to i64")
            return out
        if ty is F64:
            out = self._fresh()
            self._emit(f"{out} = arith.bitcast {value} : f64 to i64")
            return out
        if isinstance(ty, (ListType, MapType)):
            out = self._fresh()
            self._emit(f"{out} = llvm.ptrtoint {value} : !llvm.ptr to i64")
            return out
        assert _payload_inline(ty)
        return value  # I64, Unit (already i64 0), or an enum tag

    def _narrow_from_i64(self, value: str, ty: Type) -> str:
        if ty is BOOL:
            out = self._fresh()
            self._emit(f"{out} = arith.trunci {value} : i64 to i1")
            return out
        if ty is F64:
            out = self._fresh()
            self._emit(f"{out} = arith.bitcast {value} : i64 to f64")
            return out
        if isinstance(ty, (ListType, MapType)):
            out = self._fresh()
            self._emit(f"{out} = llvm.inttoptr {value} : i64 to !llvm.ptr")
            return out
        assert _payload_inline(ty)
        return value

    def _lower_match(self, expr: ast.MatchExpr) -> str | None:
        scrut_ty = self._ty_of(expr.scrutinee)
        assert isinstance(scrut_ty, AdtType)
        scrut = self.lower_expr(expr.scrutinee)
        assert scrut is not None

        result_ty = self._ty_of(expr)
        has_value = result_ty is not UNIT
        rmty = mlir_type(result_ty) if has_value else ""
        slot = self._alloc_slot(rmty) if has_value else None

        # First-match semantics, arm by arm: each arm tests its pattern (tag
        # compares, literal compares, recursively for nested patterns) and
        # branches to the next arm's test on mismatch. A tag switch can't
        # express several arms on one constructor or nested refutation.
        join_lbl = self._label()
        join_reachable = False
        for i, arm in enumerate(expr.arms):
            fail_lbl = self._label()  # next arm's test, or the trap
            self.scopes.append({})
            refutable = self._lower_pattern_test(arm.pattern, scrut_ty, scrut, fail_lbl)
            body_val = self.lower_expr(arm.body)
            self.scopes.pop()
            if not self.terminated:
                if slot is not None and body_val is not None:
                    self._store_slot(slot, rmty, body_val)
                self._terminator(f"cf.br {join_lbl}")
                join_reachable = True
            if not refutable:
                # A catch-all arm: nothing branched to fail_lbl and any later
                # arms are unreachable — emitting them would orphan blocks.
                break
            self._start_block(fail_lbl)
            if i == len(expr.arms) - 1:
                # The checker proves exhaustiveness; this trap must still link.
                self._emit("func.call @__flx_match_fail() : () -> ()")
                self._terminator("llvm.unreachable")

        if not join_reachable:
            self.terminated = True
            return None
        self._start_block(join_lbl)
        if slot is not None:
            return self._load_slot(slot, rmty)
        return None

    def _lower_pattern_test(
        self, pattern: ast.Pattern, ty: Type, value: str, fail_lbl: str
    ) -> bool:
        """Emit the tests that branch to `fail_lbl` unless `value` (typed `ty`)
        matches `pattern`, binding pattern names along the success path.
        Returns True when any test was emitted (the pattern can fail)."""
        if isinstance(pattern, ast.WildcardPattern):
            return False
        if isinstance(pattern, ast.BindPattern):
            self._define(pattern.name, _Binding("val", value, mlir_type(ty)))
            return False
        if isinstance(pattern, ast.LiteralPattern):
            if isinstance(pattern.value, bool):
                lit = self._const("1" if pattern.value else "0", "i1")
                mty = "i1"
            else:
                lit = self._const(str(pattern.value), "i64")
                mty = "i64"
            eq = self._fresh()
            self._emit(f"{eq} = arith.cmpi eq, {value}, {lit} : {mty}")
            ok = self._label()
            self._terminator(f"cf.cond_br {eq}, {ok}, {fail_lbl}")
            self._start_block(ok)
            return True
        assert isinstance(pattern, ast.CtorPattern)
        assert isinstance(ty, AdtType)
        vidx = next(i for i, v in enumerate(ty.variants) if v.name == pattern.name)
        refutable = False
        if len(ty.variants) > 1:
            if _is_enum(ty):
                tag, tag_mty = value, "i64"
            else:
                tag = self._fresh()
                self._emit(f"{tag} = llvm.extractvalue {value}[0] : {mlir_type(ty)}")
                tag_mty = "i32"
            want = self._const(str(vidx), tag_mty)
            eq = self._fresh()
            self._emit(f"{eq} = arith.cmpi eq, {tag}, {want} : {tag_mty}")
            ok = self._label()
            self._terminator(f"cf.cond_br {eq}, {ok}, {fail_lbl}")
            self._start_block(ok)
            refutable = True
        if pattern.args:
            payload_types = ty.variants[vidx].payload
            payload_slot = self._fresh()
            self._emit(f"{payload_slot} = llvm.extractvalue {value}[1] : {mlir_type(ty)}")
            for j, (sub, pty) in enumerate(zip(pattern.args, payload_types, strict=True)):
                field = self._decode_payload_field(payload_slot, payload_types, j)
                if self._lower_pattern_test(sub, pty, field, fail_lbl):
                    refutable = True
        return refutable

    def _lower_list(self, expr: ast.ListExpr) -> str:
        ty = self._ty_of(expr)
        assert isinstance(ty, ListType)
        lst = self._fresh()
        self._emit(f"{lst} = func.call @__flx_list_new() : () -> !llvm.ptr")
        for item in expr.items:
            value = self._materialize(self.lower_expr(item), item)
            slot = self._encode_elem(value, ty.elem)
            self._emit(f"func.call @__flx_list_push({lst}, {slot}) : (!llvm.ptr, i64) -> ()")
        return lst

    def _lower_index(self, expr: ast.IndexExpr) -> str:
        obj_ty = self._ty_of(expr.obj)
        assert isinstance(obj_ty, ListType)
        lst = self._materialize(self.lower_expr(expr.obj), expr.obj)
        idx = self._materialize(self.lower_expr(expr.index), expr.index)
        slot = self._fresh()
        self._emit(f"{slot} = func.call @__flx_list_get({lst}, {idx}) : (!llvm.ptr, i64) -> i64")
        return self._decode_elem(slot, obj_ty.elem)

    def _lower_list_op(self, op: str, expr: ast.CallExpr) -> str | None:
        obj_ty = self._ty_of(expr.args[0])
        assert isinstance(obj_ty, ListType)
        lst = self._materialize(self.lower_expr(expr.args[0]), expr.args[0])
        if op == "len":
            out = self._fresh()
            self._emit(f"{out} = func.call @__flx_list_len({lst}) : (!llvm.ptr) -> i64")
            return out
        if op == "push":
            value = self._materialize(self.lower_expr(expr.args[1]), expr.args[1])
            slot = self._encode_elem(value, obj_ty.elem)
            self._emit(f"func.call @__flx_list_push({lst}, {slot}) : (!llvm.ptr, i64) -> ()")
            return None
        if op == "pop":
            # The element slot codec IS the Option payload codec, so the popped
            # slot drops straight into Some's payload slot.
            return self._option_from_runtime_slot(
                f"func.call @__flx_list_pop({lst}, {{slot}}) : (!llvm.ptr, !llvm.ptr) -> i64"
            )
        assert op == "set"
        idx = self._materialize(self.lower_expr(expr.args[1]), expr.args[1])
        value = self._materialize(self.lower_expr(expr.args[2]), expr.args[2])
        slot = self._encode_elem(value, obj_ty.elem)
        self._emit(f"func.call @__flx_list_set({lst}, {idx}, {slot}) : (!llvm.ptr, i64, i64) -> ()")
        return None

    def _option_from_runtime_slot(self, call_template: str) -> str:
        """Run a runtime call that fills an i64 slot and returns a found flag,
        and assemble the Option {i32 tag, i64 slot} from them (None=0, Some=1).
        `call_template` must contain `{slot}` for the out-pointer SSA name."""
        one = self._fresh()
        self._emit(f"{one} = llvm.mlir.constant(1 : i64) : i64")
        slot_ptr = self._fresh()
        self._emit(f"{slot_ptr} = llvm.alloca {one} x i64 : (i64) -> !llvm.ptr")
        zero = self._const("0", "i64")
        self._emit(f"llvm.store {zero}, {slot_ptr} : i64, !llvm.ptr")
        found = self._fresh()
        self._emit(f"{found} = " + call_template.format(slot=slot_ptr))
        slot = self._fresh()
        self._emit(f"{slot} = llvm.load {slot_ptr} : !llvm.ptr -> i64")
        is_some = self._fresh()
        self._emit(f"{is_some} = arith.cmpi ne, {found}, {zero} : i64")
        some_tag = self._const("1", "i32")
        none_tag = self._const("0", "i32")
        tag = self._fresh()
        self._emit(f"{tag} = arith.select {is_some}, {some_tag}, {none_tag} : i32")
        payload = self._fresh()
        self._emit(f"{payload} = arith.select {is_some}, {slot}, {zero} : i64")
        opt_mty = "!llvm.struct<(i32, i64)>"
        undef = self._fresh()
        self._emit(f"{undef} = llvm.mlir.undef : {opt_mty}")
        with_tag = self._fresh()
        self._emit(f"{with_tag} = llvm.insertvalue {tag}, {undef}[0] : {opt_mty}")
        out = self._fresh()
        self._emit(f"{out} = llvm.insertvalue {payload}, {with_tag}[1] : {opt_mty}")
        return out

    def _lower_map_op(self, op: str, expr: ast.CallExpr) -> str | None:
        if op == "new":
            out = self._fresh()
            self._emit(f"{out} = func.call @__flx_map_new() : () -> !llvm.ptr")
            return out
        obj_ty = self._ty_of(expr.args[0])
        assert isinstance(obj_ty, MapType)
        m = self._materialize(self.lower_expr(expr.args[0]), expr.args[0])
        if op == "len":
            out = self._fresh()
            self._emit(f"{out} = func.call @__flx_map_len({m}) : (!llvm.ptr) -> i64")
            return out
        if op == "keys":
            out = self._fresh()
            self._emit(f"{out} = func.call @__flx_map_keys({m}) : (!llvm.ptr) -> !llvm.ptr")
            return out
        if op == "values":
            out = self._fresh()
            self._emit(f"{out} = func.call @__flx_map_values({m}) : (!llvm.ptr) -> !llvm.ptr")
            return out
        key = self._materialize(self.lower_expr(expr.args[1]), expr.args[1])
        kptr, klen = self._string_parts(key)
        if op == "set":
            value = self._materialize(self.lower_expr(expr.args[2]), expr.args[2])
            slot = self._encode_elem(value, obj_ty.value)
            self._emit(
                f"func.call @__flx_map_set({m}, {kptr}, {klen}, {slot}) : "
                "(!llvm.ptr, !llvm.ptr, i64, i64) -> ()"
            )
            return None
        if op == "get":
            return self._option_from_runtime_slot(
                f"func.call @__flx_map_get({m}, {kptr}, {klen}, {{slot}}) : "
                "(!llvm.ptr, !llvm.ptr, i64, !llvm.ptr) -> i64"
            )
        if op == "has":
            found = self._fresh()
            self._emit(
                f"{found} = func.call @__flx_map_has({m}, {kptr}, {klen}) : "
                "(!llvm.ptr, !llvm.ptr, i64) -> i64"
            )
            zero = self._const("0", "i64")
            out = self._fresh()
            self._emit(f"{out} = arith.cmpi ne, {found}, {zero} : i64")
            return out
        assert op == "remove"
        self._emit(
            f"func.call @__flx_map_remove({m}, {kptr}, {klen}) : (!llvm.ptr, !llvm.ptr, i64) -> ()"
        )
        return None

    def _lower_for(self, stmt: ast.ForStmt) -> None:
        iter_ty = self._ty_of(stmt.iter)
        assert isinstance(iter_ty, ListType)
        lst = self._materialize(self.lower_expr(stmt.iter), stmt.iter)
        n = self._fresh()
        self._emit(f"{n} = func.call @__flx_list_len({lst}) : (!llvm.ptr) -> i64")
        islot = self._alloc_slot("i64")
        self._store_slot(islot, "i64", self._const("0", "i64"))
        cond_lbl, body_lbl, exit_lbl = self._label(), self._label(), self._label()
        self._terminator(f"cf.br {cond_lbl}")
        self._start_block(cond_lbl)
        i = self._load_slot(islot, "i64")
        cmp = self._fresh()
        self._emit(f"{cmp} = arith.cmpi slt, {i}, {n} : i64")
        self._terminator(f"cf.cond_br {cmp}, {body_lbl}, {exit_lbl}")
        self._start_block(body_lbl)
        cur = self._load_slot(islot, "i64")
        slot = self._fresh()
        self._emit(f"{slot} = func.call @__flx_list_get({lst}, {cur}) : (!llvm.ptr, i64) -> i64")
        elem = self._decode_elem(slot, iter_ty.elem)
        self.scopes.append({})
        self._define(stmt.name, _Binding("val", elem, mlir_type(iter_ty.elem)))
        self.lower_block(stmt.body)
        self.scopes.pop()
        one = self._const("1", "i64")
        nxt = self._fresh()
        self._emit(f"{nxt} = arith.addi {cur}, {one} : i64")
        self._store_slot(islot, "i64", nxt)
        self._terminator(f"cf.br {cond_lbl}")
        self._start_block(exit_lbl)

    def _lower_try(self, expr: ast.TryExpr) -> str:
        result_adt = self._ty_of(expr.expr)
        assert isinstance(result_adt, AdtType)
        r = self.lower_expr(expr.expr)
        assert r is not None
        rmty = mlir_type(result_adt)
        tag = self._fresh()
        self._emit(f"{tag} = llvm.extractvalue {r}[0] : {rmty}")
        zero = self._const("0", "i32")
        is_ok = self._fresh()
        self._emit(f"{is_ok} = arith.cmpi eq, {tag}, {zero} : i32")
        cont, prop = self._label(), self._label()
        self._terminator(f"cf.cond_br {is_ok}, {cont}, {prop}")

        self._start_block(prop)
        if self.test_mode:
            self._emit("func.call @__flx_explicit_fail() : () -> ()")
            one = self._const("1", "i32")
            self._terminator(f"func.return {one} : i32")
        else:
            self._terminator(f"func.return {r} : {rmty}")

        self._start_block(cont)
        payload = self._fresh()
        self._emit(f"{payload} = llvm.extractvalue {r}[1] : {rmty}")
        ok_payload = next(v for v in result_adt.variants if v.name == "Ok").payload
        return self._decode_payload_field(payload, ok_payload, 0)

    def _lower_unary(self, expr: ast.UnaryExpr) -> str:
        if (
            expr.op == "-"
            and isinstance(expr.operand, ast.IntLit)
            and expr.operand.value == 1 << 63
        ):
            # INT64_MIN: the positive magnitude is not a valid i64 constant.
            return self._const(str(-(1 << 63)), "i64")
        operand = self.lower_expr(expr.operand)
        out = self._fresh()
        if expr.op == "-" and self._ty_of(expr.operand) is F64:
            self._emit(f"{out} = arith.negf {operand} : f64")
        elif expr.op == "-":
            zero = self._const("0", "i64")
            self._emit(f"{out} = arith.subi {zero}, {operand} : i64")
        else:
            one = self._const("1", "i1")
            self._emit(f"{out} = arith.xori {operand}, {one} : i1")
        return out

    def _lower_binary(self, expr: ast.BinaryExpr) -> str:
        op = expr.op
        if op in ("&&", "||"):
            return self._lower_short_circuit(expr)
        left = self._materialize(self.lower_expr(expr.left), expr.left)
        right = self._materialize(self.lower_expr(expr.right), expr.right)
        if op == "++":
            lp, ll = self._string_parts(left)
            rp, rl = self._string_parts(right)
            return self._str_runtime(
                "__flx_str_concat", ["!llvm.ptr", "i64", "!llvm.ptr", "i64"], [lp, ll, rp, rl]
            )
        if op in ("==", "!="):
            # Structural equality (works for scalars, records, and ADTs).
            equal = self._emit_equal(left, right, self._ty_of(expr.left))
            if op == "==":
                return equal
            one = self._const("1", "i1")
            out = self._fresh()
            self._emit(f"{out} = arith.xori {equal}, {one} : i1")
            return out
        out = self._fresh()
        is_float = self._ty_of(expr.left) is F64
        if op in ("<<", ">>"):
            # Shift counts are masked to 0..63 (matching the interpreter):
            # LLVM shifts by >= bit-width are poison.
            c63 = self._const("63", "i64")
            masked = self._fresh()
            self._emit(f"{masked} = arith.andi {right}, {c63} : i64")
            shift = "shli" if op == "<<" else "shrsi"
            self._emit(f"{out} = arith.{shift} {left}, {masked} : i64")
        elif op in _BIT_OP:
            self._emit(f"{out} = arith.{_BIT_OP[op]} {left}, {right} : i64")
        elif is_float and op in _FARITH_OP:
            self._emit(f"{out} = arith.{_FARITH_OP[op]} {left}, {right} : f64")
        elif is_float and op in _FCMP_PRED:
            self._emit(f"{out} = arith.cmpf {_FCMP_PRED[op]}, {left}, {right} : f64")
        elif op in _DIV_OP:
            self._emit(f"{out} = func.call {_DIV_OP[op]}({left}, {right}) : (i64, i64) -> i64")
        elif op in _ARITH_OP:
            self._emit(f"{out} = arith.{_ARITH_OP[op]} {left}, {right} : i64")
        elif op in _CMP_PRED:
            operand_ty = mlir_type(self._ty_of(expr.left))
            self._emit(f"{out} = arith.cmpi {_CMP_PRED[op]}, {left}, {right} : {operand_ty}")
        else:
            raise BackendError(f"unknown operator {op!r}")
        return out

    def _lower_short_circuit(self, expr: ast.BinaryExpr) -> str:
        """Lower `&&` / `||` with proper short-circuit evaluation of the RHS."""
        left = self.lower_expr(expr.left)
        assert left is not None
        slot = self._fresh()
        self._emit(f"{slot} = memref.alloca() : memref<i1>")
        rhs_lbl, short_lbl, join_lbl = self._label(), self._label(), self._label()
        # `&&`: if left, evaluate RHS, else store false. `||`: if left, store true, else RHS.
        if expr.op == "&&":
            self._terminator(f"cf.cond_br {left}, {rhs_lbl}, {short_lbl}")
            short_value = "0"
        else:
            self._terminator(f"cf.cond_br {left}, {short_lbl}, {rhs_lbl}")
            short_value = "1"

        self._start_block(rhs_lbl)
        right = self.lower_expr(expr.right)
        assert right is not None
        self._emit(f"memref.store {right}, {slot}[] : memref<i1>")
        self._terminator(f"cf.br {join_lbl}")

        self._start_block(short_lbl)
        constant = self._const(short_value, "i1")
        self._emit(f"memref.store {constant}, {slot}[] : memref<i1>")
        self._terminator(f"cf.br {join_lbl}")

        self._start_block(join_lbl)
        out = self._fresh()
        self._emit(f"{out} = memref.load {slot}[] : memref<i1>")
        return out

    def _lower_call(self, expr: ast.CallExpr) -> str | None:
        # Trait method call (p.show()) -> direct call to the resolved impl symbol.
        if isinstance(expr.callee, ast.MemberExpr):
            symbol = self.method_targets.get(id(expr))
            if symbol is not None:
                method_ty = self.functions[symbol]
                recv = self._materialize(self.lower_expr(expr.callee.obj), expr.callee.obj)
                args = [recv, *(self._materialize(self.lower_expr(a), a) for a in expr.args)]
                return self._emit_call(f"flx_{symbol}", args, method_ty)
            recv_ty = self.types.get(id(expr.callee.obj))
            if isinstance(recv_ty, (ListType, MapType)) and expr.callee.name in ("eq", "show"):
                recv = self._materialize(self.lower_expr(expr.callee.obj), expr.callee.obj)
                if expr.callee.name == "eq":
                    other = self._materialize(self.lower_expr(expr.args[0]), expr.args[0])
                    return self._emit_equal(recv, other, recv_ty)
                return self._emit_show(recv, recv_ty)
        # Effectful intrinsics (validated by the checker). Log.* prints its
        # message; other intrinsics are MVP no-ops at runtime.
        if isinstance(expr.callee, ast.MemberExpr):
            # Qualified constructor with payload, e.g. E.Code(x).
            if expr.callee.name in self.constructors:
                adt = self._ty_of(expr)
                assert isinstance(adt, AdtType)
                return self._lower_ctor(adt, expr.callee.name, expr.args)
            obj = expr.callee.obj
            if isinstance(obj, ast.NameExpr):
                method = expr.callee.name
                if obj.name == "Log" and expr.args:
                    value = self.lower_expr(expr.args[0])
                    assert value is not None
                    ptr, length = self._string_parts(value)
                    if method == "print":
                        runtime_fn = "__flx_print"
                    elif method == "error":
                        runtime_fn = "__flx_error"
                    else:
                        runtime_fn = "__flx_log"
                    self._emit(f"func.call @{runtime_fn}({ptr}, {length}) : (!llvm.ptr, i64) -> ()")
                    return None
                if obj.name == "Fs" and method == "read_line":
                    return self._lower_read_line()
                if obj.name == "Fs" and method == "read_text":
                    return self._lower_read_text(expr)
                if obj.name == "Fs" and method == "write_text":
                    return self._lower_write_text(expr)
                if obj.name == "Time" and method == "monotonic_ms":
                    out = self._fresh()
                    self._emit(f"{out} = func.call @__flx_monotonic_ms() : () -> i64")
                    return out
                if obj.name == "List":
                    return self._lower_list_op(method, expr)
                if obj.name == "Map":
                    return self._lower_map_op(method, expr)
                if obj.name == "Str" and method == "byte_at":
                    s = self._materialize(self.lower_expr(expr.args[0]), expr.args[0])
                    ptr, length = self._string_parts(s)
                    idx = self._materialize(self.lower_expr(expr.args[1]), expr.args[1])
                    out = self._fresh()
                    self._emit(
                        f"{out} = func.call @__flx_byte_at({ptr}, {length}, {idx}) : "
                        "(!llvm.ptr, i64, i64) -> i64"
                    )
                    return out
                if obj.name == "Str" and method == "substr":
                    s = self._materialize(self.lower_expr(expr.args[0]), expr.args[0])
                    ptr, length = self._string_parts(s)
                    start = self._materialize(self.lower_expr(expr.args[1]), expr.args[1])
                    count = self._materialize(self.lower_expr(expr.args[2]), expr.args[2])
                    return self._str_runtime(
                        "__flx_substr",
                        ["!llvm.ptr", "i64", "i64", "i64"],
                        [ptr, length, start, count],
                    )
                if obj.name == "Str" and method == "from_byte":
                    b = self._materialize(self.lower_expr(expr.args[0]), expr.args[0])
                    return self._str_runtime("__flx_from_byte", ["i64"], [b])
                if obj.name == "Str" and method == "from_bytes":
                    xs = self._materialize(self.lower_expr(expr.args[0]), expr.args[0])
                    return self._str_runtime("__flx_from_bytes", ["!llvm.ptr"], [xs])
                if obj.name == "Str" and method == "parse_f64":
                    s = self._materialize(self.lower_expr(expr.args[0]), expr.args[0])
                    ptr, _length = self._string_parts(s)  # strings are NUL-terminated
                    out = self._fresh()
                    self._emit(f"{out} = func.call @__flx_parse_f64({ptr}) : (!llvm.ptr) -> f64")
                    return out
                if obj.name == "Str" and method == "to_str_fixed":
                    x = self._materialize(self.lower_expr(expr.args[0]), expr.args[0])
                    d = self._materialize(self.lower_expr(expr.args[1]), expr.args[1])
                    return self._str_runtime("__flx_f64_fixed", ["f64", "i64"], [x, d])
                if obj.name == "Str" and method == "to_hex":
                    n = self._materialize(self.lower_expr(expr.args[0]), expr.args[0])
                    return self._str_runtime("__flx_i64_to_hex", ["i64"], [n])
                if obj.name == "Str" and method == "to_unsigned":
                    n = self._materialize(self.lower_expr(expr.args[0]), expr.args[0])
                    return self._str_runtime("__flx_i64_to_unsigned", ["i64"], [n])
                if obj.name == "Env" and method == "argv":
                    out = self._fresh()
                    self._emit(f"{out} = func.call @__flx_argv() : () -> !llvm.ptr")
                    return out
            return None
        if not isinstance(expr.callee, ast.NameExpr):
            raise BackendError("only direct function calls are supported")
        name = expr.callee.name
        # A function VALUE in scope (a parameter or let-bound reference)
        # shadows the global namespaces — call through it indirectly.
        local_fn = next((f[name] for f in reversed(self.scopes) if name in f), None)
        callee_ty = self.types.get(id(expr.callee))
        if local_fn is not None and isinstance(callee_ty, FnType):
            args = [self._materialize(self.lower_expr(a), a) for a in expr.args]
            assert local_fn.kind == "val", "fn values are SSA-only (no slots)"
            arg_list = ", ".join(args)
            fty = mlir_type(callee_ty)
            if callee_ty.ret is UNIT:
                self._emit(f"func.call_indirect {local_fn.ref}({arg_list}) : {fty}")
                return None
            out = self._fresh()
            self._emit(f"{out} = func.call_indirect {local_fn.ref}({arg_list}) : {fty}")
            return out
        # Bounded-generic call -> direct call to the monomorphized specialization.
        symbol = self.method_targets.get(id(expr))
        if symbol is not None:
            spec_ty = self.functions[symbol]
            args = [self._materialize(self.lower_expr(a), a) for a in expr.args]
            return self._emit_call(f"flx_{symbol}", args, spec_ty)
        if name in self.extern_fns:
            return self._lower_extern_call(name, expr)
        if name == "to_str":  # prelude: I64 | F64 -> String
            arg = self.lower_expr(expr.args[0])
            assert arg is not None
            if self._ty_of(expr.args[0]) is F64:
                return self._str_runtime("__flx_f64_to_str", ["f64"], [arg])
            return self._str_runtime("__flx_int_to_str", ["i64"], [arg])
        if name == "to_f64":
            arg = self.lower_expr(expr.args[0])
            assert arg is not None
            out = self._fresh()
            self._emit(f"{out} = arith.sitofp {arg} : i64 to f64")
            return out
        if name == "to_i64":
            arg = self.lower_expr(expr.args[0])
            assert arg is not None
            out = self._fresh()
            self._emit(f"{out} = func.call @__flx_f64_to_i64({arg}) : (f64) -> i64")
            return out
        if name in self.constructors:  # variant constructor, e.g. Ok(x)
            adt = self._ty_of(expr)
            assert isinstance(adt, AdtType)
            return self._lower_ctor(adt, name, expr.args)
        if name in _BUILTINS:
            self._lower_builtin(name, expr)
            return None
        fn_ty = self.functions.get(name)
        if fn_ty is None:
            raise BackendError(f"call to non-function {name!r}")
        args = [self._materialize(self.lower_expr(a), a) for a in expr.args]
        return self._emit_call(f"flx_{name}", args, fn_ty)

    def _lower_builtin(self, name: str, call: ast.CallExpr) -> None:
        if not self.test_mode:
            raise BackendError(f"{name}() can only be used inside a test")
        if name == "assert":
            cond = self.lower_expr(call.args[0])
            assert cond is not None
            self._assert_branch(cond, "@__flx_assert_fail", "", "")
        elif name in ("assert_eq", "assert_ne"):
            left = self._materialize(self.lower_expr(call.args[0]), call.args[0])
            right = self._materialize(self.lower_expr(call.args[1]), call.args[1])
            operand_type = self._ty_of(call.args[0])
            operand_ty = mlir_type(operand_type)
            if operand_type is STRING:
                # Strings compare through the Eq impl (the checker required it),
                # and failures print the actual values.
                equal = self._fresh()
                self._emit(
                    f"{equal} = func.call @flx_t$Eq$0$String$eq({left}, {right}) : "
                    f"({operand_ty}, {operand_ty}) -> i1"
                )
                if name == "assert_eq":
                    ok_cond = equal
                else:
                    one = self._const("1", "i1")
                    ok_cond = self._fresh()
                    self._emit(f"{ok_cond} = arith.xori {equal}, {one} : i1")
                lp, ll = self._string_parts(left)
                if name == "assert_eq":
                    rp, rl = self._string_parts(right)
                    self._assert_branch(
                        ok_cond,
                        "@__flx_assert_streq_fail",
                        f"{lp}, {ll}, {rp}, {rl}",
                        "!llvm.ptr, i64, !llvm.ptr, i64",
                    )
                else:
                    self._assert_branch(
                        ok_cond, "@__flx_assert_strne_fail", f"{lp}, {ll}", "!llvm.ptr, i64"
                    )
                return None
            if operand_type is F64:
                equal = self._fresh()
                self._emit(f"{equal} = arith.cmpf oeq, {left}, {right} : f64")
                if name == "assert_eq":
                    self._assert_branch(
                        equal, "@__flx_assert_feq_fail", f"{left}, {right}", "f64, f64"
                    )
                else:
                    one = self._const("1", "i1")
                    ok = self._fresh()
                    self._emit(f"{ok} = arith.xori {equal}, {one} : i1")
                    self._assert_branch(ok, "@__flx_assert_fne_fail", f"{left}", "f64")
                return None
            impl_symbol = self.checked.assert_impls.get(id(call))
            if impl_symbol is not None:
                # An Eq impl carries the comparison (the checker routed it);
                # failures report generically, like any aggregate.
                equal = self._fresh()
                self._emit(
                    f"{equal} = func.call @flx_{impl_symbol}({left}, {right}) : "
                    f"({operand_ty}, {operand_ty}) -> i1"
                )
                if name == "assert_eq":
                    ok_cond = equal
                else:
                    one = self._const("1", "i1")
                    ok_cond = self._fresh()
                    self._emit(f"{ok_cond} = arith.xori {equal}, {one} : i1")
                self._assert_branch(ok_cond, "@__flx_assert_fail", "", "")
                return None
            equal = self._emit_equal(left, right, operand_type)
            if name == "assert_eq":
                ok_cond = equal
            else:  # assert_ne passes when the values differ
                one = self._const("1", "i1")
                ok_cond = self._fresh()
                self._emit(f"{ok_cond} = arith.xori {equal}, {one} : i1")
            if _is_aggregate(operand_ty):
                # Aggregate values: report a generic failure (no scalar to print).
                self._assert_branch(ok_cond, "@__flx_assert_fail", "", "")
            else:
                a64 = self._to_i64(left, operand_ty)
                b64 = self._to_i64(right, operand_ty)
                sym = "@__flx_assert_eq_fail" if name == "assert_eq" else "@__flx_assert_ne_fail"
                self._assert_branch(ok_cond, sym, f"{a64}, {b64}", "i64, i64")
        else:  # fail(msg) / panic(msg) always fail the test
            if call.args:
                value = self.lower_expr(call.args[0])
                assert value is not None
                ptr, length = self._string_parts(value)
                self._emit(f"func.call @__flx_fail_msg({ptr}, {length}) : (!llvm.ptr, i64) -> ()")
            else:
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

    def _emit_equal(self, left: str, right: str, ty: Type) -> str:
        """Structural equality producing an i1 (scalars, ADTs, and records)."""
        mty = mlir_type(ty)
        if ty is F64:
            out = self._fresh()
            self._emit(f"{out} = arith.cmpf oeq, {left}, {right} : f64")
            return out
        if isinstance(ty, ListType):
            return self._emit_list_equal(left, right, ty)
        if isinstance(ty, MapType):
            return self._emit_map_equal(left, right, ty)
        if not _is_aggregate(mty):
            out = self._fresh()
            self._emit(f"{out} = arith.cmpi eq, {left}, {right} : {mty}")
            return out
        if isinstance(ty, AdtType):
            tag_eq = self._cmp_field(left, right, mty, 0, "i32")
            lslot = self._fresh()
            self._emit(f"{lslot} = llvm.extractvalue {left}[1] : {mty}")
            rslot = self._fresh()
            self._emit(f"{rslot} = llvm.extractvalue {right}[1] : {mty}")
            string_variants = [
                i
                for i, v in enumerate(ty.variants)
                if len(v.payload) == 1 and v.payload[0] is STRING
            ]
            if string_variants:
                # Variants carrying a String compare by CONTENT. The runtime
                # helper only dereferences the boxed slots when the use-string
                # flag is set — and the flag requires EQUAL tags too, because
                # with differing tags the right slot can be a raw inline value
                # (Word("hi") vs Num(7)) that must never be treated as a
                # pointer. The payload result is dead when tags differ anyway.
                ltag = self._fresh()
                self._emit(f"{ltag} = llvm.extractvalue {left}[0] : {mty}")
                is_str = None
                for i in string_variants:
                    ci = self._const(str(i), "i32")
                    this = self._fresh()
                    self._emit(f"{this} = arith.cmpi eq, {ltag}, {ci} : i32")
                    if is_str is None:
                        is_str = this
                    else:
                        nxt = self._fresh()
                        self._emit(f"{nxt} = arith.ori {is_str}, {this} : i1")
                        is_str = nxt
                str_and_same = self._fresh()
                self._emit(f"{str_and_same} = arith.andi {is_str}, {tag_eq} : i1")
                use_str = self._fresh()
                self._emit(f"{use_str} = arith.extui {str_and_same} : i1 to i64")
                raw = self._fresh()
                self._emit(
                    f"{raw} = func.call @__flx_slot_str_eq({use_str}, {lslot}, {rslot}) : "
                    "(i64, i64, i64) -> i64"
                )
                zero = self._const("0", "i64")
                payload_eq = self._fresh()
                self._emit(f"{payload_eq} = arith.cmpi ne, {raw}, {zero} : i64")
            else:
                payload_eq = self._fresh()
                self._emit(f"{payload_eq} = arith.cmpi eq, {lslot}, {rslot} : i64")
            # Variants carrying an F64 compare as FLOATS, not slot bits — bit
            # equality would make Some(0.0) != Some(-0.0) and Some(nan) ==
            # Some(nan), diverging from the interpreter's structural equality.
            float_variants = [
                i for i, v in enumerate(ty.variants) if len(v.payload) == 1 and v.payload[0] is F64
            ]
            if float_variants:
                lf = self._fresh()
                self._emit(f"{lf} = arith.bitcast {lslot} : i64 to f64")
                rf = self._fresh()
                self._emit(f"{rf} = arith.bitcast {rslot} : i64 to f64")
                feq = self._fresh()
                self._emit(f"{feq} = arith.cmpf oeq, {lf}, {rf} : f64")
                ltag = self._fresh()
                self._emit(f"{ltag} = llvm.extractvalue {left}[0] : {mty}")
                for i in float_variants:
                    # When tags differ tag_eq already kills the AND, so picking
                    # the comparison by the LEFT tag is sound.
                    ci = self._const(str(i), "i32")
                    is_float_arm = self._fresh()
                    self._emit(f"{is_float_arm} = arith.cmpi eq, {ltag}, {ci} : i32")
                    nxt = self._fresh()
                    self._emit(f"{nxt} = arith.select {is_float_arm}, {feq}, {payload_eq} : i1")
                    payload_eq = nxt
            out = self._fresh()
            self._emit(f"{out} = arith.andi {tag_eq}, {payload_eq} : i1")
            return out
        if isinstance(ty, RecordType):
            conj: str | None = None
            for i, (_, fty) in enumerate(ty.fields):
                lf = self._fresh()
                self._emit(f"{lf} = llvm.extractvalue {left}[{i}] : {mty}")
                rf = self._fresh()
                self._emit(f"{rf} = llvm.extractvalue {right}[{i}] : {mty}")
                feq = self._emit_equal(lf, rf, fty)
                if conj is None:
                    conj = feq
                else:
                    nxt = self._fresh()
                    self._emit(f"{nxt} = arith.andi {conj}, {feq} : i1")
                    conj = nxt
            return conj if conj is not None else self._const("1", "i1")
        raise BackendError(f"cannot compare values of type {ty}")

    def _emit_list_equal(self, left: str, right: str, ty: ListType) -> str:
        ln = self._fresh()
        self._emit(f"{ln} = func.call @__flx_list_len({left}) : (!llvm.ptr) -> i64")
        rn = self._fresh()
        self._emit(f"{rn} = func.call @__flx_list_len({right}) : (!llvm.ptr) -> i64")
        same_len = self._fresh()
        self._emit(f"{same_len} = arith.cmpi eq, {ln}, {rn} : i64")
        result_slot = self._alloc_slot("i1")
        self._store_slot(result_slot, "i1", same_len)
        index_slot = self._alloc_slot("i64")
        self._store_slot(index_slot, "i64", self._const("0", "i64"))
        cond_lbl, body_lbl, exit_lbl = self._label(), self._label(), self._label()
        self._terminator(f"cf.br {cond_lbl}")
        self._start_block(cond_lbl)
        current = self._load_slot(result_slot, "i1")
        i = self._load_slot(index_slot, "i64")
        in_bounds = self._fresh()
        self._emit(f"{in_bounds} = arith.cmpi slt, {i}, {ln} : i64")
        keep_going = self._fresh()
        self._emit(f"{keep_going} = arith.andi {current}, {in_bounds} : i1")
        self._terminator(f"cf.cond_br {keep_going}, {body_lbl}, {exit_lbl}")
        self._start_block(body_lbl)
        cur = self._load_slot(index_slot, "i64")
        lslot = self._fresh()
        self._emit(f"{lslot} = func.call @__flx_list_get({left}, {cur}) : (!llvm.ptr, i64) -> i64")
        rslot = self._fresh()
        self._emit(f"{rslot} = func.call @__flx_list_get({right}, {cur}) : (!llvm.ptr, i64) -> i64")
        lelem = self._decode_elem(lslot, ty.elem)
        relem = self._decode_elem(rslot, ty.elem)
        elem_eq = self._emit_equal(lelem, relem, ty.elem)
        still = self._load_slot(result_slot, "i1")
        next_result = self._fresh()
        self._emit(f"{next_result} = arith.andi {still}, {elem_eq} : i1")
        self._store_slot(result_slot, "i1", next_result)
        one = self._const("1", "i64")
        nxt = self._fresh()
        self._emit(f"{nxt} = arith.addi {cur}, {one} : i64")
        self._store_slot(index_slot, "i64", nxt)
        self._terminator(f"cf.br {cond_lbl}")
        self._start_block(exit_lbl)
        return self._load_slot(result_slot, "i1")

    def _concat_strings(self, left: str, right: str) -> str:
        lp, ll = self._string_parts(left)
        rp, rl = self._string_parts(right)
        return self._str_runtime(
            "__flx_str_concat", ["!llvm.ptr", "i64", "!llvm.ptr", "i64"], [lp, ll, rp, rl]
        )

    def _const_string_value(self, text: str) -> str:
        return self._lower_string(ast.StringLit(text, self.checked.module.span))

    def _emit_show(self, value: str, ty: Type) -> str:
        if ty is I64:
            return self._str_runtime("__flx_int_to_str", ["i64"], [value])
        if ty is F64:
            return self._str_runtime("__flx_f64_to_str", ["f64"], [value])
        if ty is STRING:
            return value
        if ty is BOOL:
            true_s = self._const_string_value("true")
            false_s = self._const_string_value("false")
            out = self._fresh()
            self._emit(
                f"{out} = arith.select {value}, {true_s}, {false_s} : !llvm.struct<(ptr, i64)>"
            )
            return out
        if ty is UNIT:
            return self._const_string_value("()")
        if isinstance(ty, ListType):
            return self._emit_list_show(value, ty)
        if isinstance(ty, MapType):
            return self._emit_map_show(value, ty)
        raise BackendError(f"cannot show values of type {ty}")

    def _emit_list_show(self, value: str, ty: ListType) -> str:
        str_mty = "!llvm.struct<(ptr, i64)>"
        out_slot = self._alloc_slot(str_mty)
        self._store_slot(out_slot, str_mty, self._const_string_value("["))
        n = self._fresh()
        self._emit(f"{n} = func.call @__flx_list_len({value}) : (!llvm.ptr) -> i64")
        index_slot = self._alloc_slot("i64")
        self._store_slot(index_slot, "i64", self._const("0", "i64"))
        cond_lbl, body_lbl, exit_lbl = self._label(), self._label(), self._label()
        self._terminator(f"cf.br {cond_lbl}")
        self._start_block(cond_lbl)
        i = self._load_slot(index_slot, "i64")
        in_bounds = self._fresh()
        self._emit(f"{in_bounds} = arith.cmpi slt, {i}, {n} : i64")
        self._terminator(f"cf.cond_br {in_bounds}, {body_lbl}, {exit_lbl}")
        self._start_block(body_lbl)
        cur = self._load_slot(index_slot, "i64")
        zero = self._const("0", "i64")
        is_first = self._fresh()
        self._emit(f"{is_first} = arith.cmpi eq, {cur}, {zero} : i64")
        comma_lbl, elem_lbl = self._label(), self._label()
        self._terminator(f"cf.cond_br {is_first}, {elem_lbl}, {comma_lbl}")
        self._start_block(comma_lbl)
        with_comma = self._concat_strings(
            self._load_slot(out_slot, str_mty), self._const_string_value(", ")
        )
        self._store_slot(out_slot, str_mty, with_comma)
        self._terminator(f"cf.br {elem_lbl}")
        self._start_block(elem_lbl)
        elem_slot = self._fresh()
        self._emit(
            f"{elem_slot} = func.call @__flx_list_get({value}, {cur}) : (!llvm.ptr, i64) -> i64"
        )
        elem = self._decode_elem(elem_slot, ty.elem)
        shown = self._emit_show(elem, ty.elem)
        next_out = self._concat_strings(self._load_slot(out_slot, str_mty), shown)
        self._store_slot(out_slot, str_mty, next_out)
        one = self._const("1", "i64")
        nxt = self._fresh()
        self._emit(f"{nxt} = arith.addi {cur}, {one} : i64")
        self._store_slot(index_slot, "i64", nxt)
        self._terminator(f"cf.br {cond_lbl}")
        self._start_block(exit_lbl)
        closed = self._concat_strings(
            self._load_slot(out_slot, str_mty), self._const_string_value("]")
        )
        self._store_slot(out_slot, str_mty, closed)
        return self._load_slot(out_slot, str_mty)

    def _emit_map_show(self, value: str, ty: MapType) -> str:
        str_mty = "!llvm.struct<(ptr, i64)>"
        out_slot = self._alloc_slot(str_mty)
        self._store_slot(out_slot, str_mty, self._const_string_value("{"))
        keys = self._fresh()
        self._emit(f"{keys} = func.call @__flx_map_keys({value}) : (!llvm.ptr) -> !llvm.ptr")
        n = self._fresh()
        self._emit(f"{n} = func.call @__flx_list_len({keys}) : (!llvm.ptr) -> i64")
        index_slot = self._alloc_slot("i64")
        self._store_slot(index_slot, "i64", self._const("0", "i64"))
        cond_lbl, body_lbl, exit_lbl = self._label(), self._label(), self._label()
        self._terminator(f"cf.br {cond_lbl}")
        self._start_block(cond_lbl)
        i = self._load_slot(index_slot, "i64")
        in_bounds = self._fresh()
        self._emit(f"{in_bounds} = arith.cmpi slt, {i}, {n} : i64")
        self._terminator(f"cf.cond_br {in_bounds}, {body_lbl}, {exit_lbl}")
        self._start_block(body_lbl)
        cur = self._load_slot(index_slot, "i64")
        zero = self._const("0", "i64")
        is_first = self._fresh()
        self._emit(f"{is_first} = arith.cmpi eq, {cur}, {zero} : i64")
        comma_lbl, entry_lbl = self._label(), self._label()
        self._terminator(f"cf.cond_br {is_first}, {entry_lbl}, {comma_lbl}")
        self._start_block(comma_lbl)
        with_comma = self._concat_strings(
            self._load_slot(out_slot, str_mty), self._const_string_value(", ")
        )
        self._store_slot(out_slot, str_mty, with_comma)
        self._terminator(f"cf.br {entry_lbl}")
        self._start_block(entry_lbl)
        key_slot = self._fresh()
        self._emit(
            f"{key_slot} = func.call @__flx_list_get({keys}, {cur}) : (!llvm.ptr, i64) -> i64"
        )
        key = self._decode_elem(key_slot, STRING)
        with_key = self._concat_strings(self._load_slot(out_slot, str_mty), key)
        with_colon = self._concat_strings(with_key, self._const_string_value(": "))
        kptr, klen = self._string_parts(key)
        one_i64 = self._fresh()
        self._emit(f"{one_i64} = llvm.mlir.constant(1 : i64) : i64")
        value_slot_ptr = self._fresh()
        self._emit(f"{value_slot_ptr} = llvm.alloca {one_i64} x i64 : (i64) -> !llvm.ptr")
        found = self._fresh()
        self._emit(
            f"{found} = func.call @__flx_map_get({value}, {kptr}, {klen}, {value_slot_ptr}) : "
            "(!llvm.ptr, !llvm.ptr, i64, !llvm.ptr) -> i64"
        )
        vslot = self._fresh()
        self._emit(f"{vslot} = llvm.load {value_slot_ptr} : !llvm.ptr -> i64")
        item = self._decode_elem(vslot, ty.value)
        shown = self._emit_show(item, ty.value)
        with_value = self._concat_strings(with_colon, shown)
        self._store_slot(out_slot, str_mty, with_value)
        one = self._const("1", "i64")
        nxt = self._fresh()
        self._emit(f"{nxt} = arith.addi {cur}, {one} : i64")
        self._store_slot(index_slot, "i64", nxt)
        self._terminator(f"cf.br {cond_lbl}")
        self._start_block(exit_lbl)
        closed = self._concat_strings(
            self._load_slot(out_slot, str_mty), self._const_string_value("}")
        )
        self._store_slot(out_slot, str_mty, closed)
        return self._load_slot(out_slot, str_mty)

    def _emit_map_equal(self, left: str, right: str, ty: MapType) -> str:
        ln = self._fresh()
        self._emit(f"{ln} = func.call @__flx_map_len({left}) : (!llvm.ptr) -> i64")
        rn = self._fresh()
        self._emit(f"{rn} = func.call @__flx_map_len({right}) : (!llvm.ptr) -> i64")
        same_len = self._fresh()
        self._emit(f"{same_len} = arith.cmpi eq, {ln}, {rn} : i64")
        result_slot = self._alloc_slot("i1")
        self._store_slot(result_slot, "i1", same_len)
        keys = self._fresh()
        self._emit(f"{keys} = func.call @__flx_map_keys({left}) : (!llvm.ptr) -> !llvm.ptr")
        nkeys = self._fresh()
        self._emit(f"{nkeys} = func.call @__flx_list_len({keys}) : (!llvm.ptr) -> i64")
        index_slot = self._alloc_slot("i64")
        self._store_slot(index_slot, "i64", self._const("0", "i64"))
        cond_lbl, body_lbl, exit_lbl = self._label(), self._label(), self._label()
        self._terminator(f"cf.br {cond_lbl}")
        self._start_block(cond_lbl)
        current = self._load_slot(result_slot, "i1")
        i = self._load_slot(index_slot, "i64")
        in_bounds = self._fresh()
        self._emit(f"{in_bounds} = arith.cmpi slt, {i}, {nkeys} : i64")
        keep_going = self._fresh()
        self._emit(f"{keep_going} = arith.andi {current}, {in_bounds} : i1")
        self._terminator(f"cf.cond_br {keep_going}, {body_lbl}, {exit_lbl}")

        self._start_block(body_lbl)
        cur = self._load_slot(index_slot, "i64")
        key_slot = self._fresh()
        self._emit(
            f"{key_slot} = func.call @__flx_list_get({keys}, {cur}) : (!llvm.ptr, i64) -> i64"
        )
        key = self._decode_elem(key_slot, STRING)
        kptr, klen = self._string_parts(key)
        one_i64 = self._fresh()
        self._emit(f"{one_i64} = llvm.mlir.constant(1 : i64) : i64")
        left_slot_ptr = self._fresh()
        self._emit(f"{left_slot_ptr} = llvm.alloca {one_i64} x i64 : (i64) -> !llvm.ptr")
        right_slot_ptr = self._fresh()
        self._emit(f"{right_slot_ptr} = llvm.alloca {one_i64} x i64 : (i64) -> !llvm.ptr")
        _lfound = self._fresh()
        self._emit(
            f"{_lfound} = func.call @__flx_map_get({left}, {kptr}, {klen}, {left_slot_ptr}) : "
            "(!llvm.ptr, !llvm.ptr, i64, !llvm.ptr) -> i64"
        )
        rfound = self._fresh()
        self._emit(
            f"{rfound} = func.call @__flx_map_get({right}, {kptr}, {klen}, {right_slot_ptr}) : "
            "(!llvm.ptr, !llvm.ptr, i64, !llvm.ptr) -> i64"
        )
        zero = self._const("0", "i64")
        has_right = self._fresh()
        self._emit(f"{has_right} = arith.cmpi ne, {rfound}, {zero} : i64")
        found_lbl, missing_lbl, cont_lbl = self._label(), self._label(), self._label()
        self._terminator(f"cf.cond_br {has_right}, {found_lbl}, {missing_lbl}")

        self._start_block(missing_lbl)
        self._store_slot(result_slot, "i1", self._const("0", "i1"))
        self._terminator(f"cf.br {cont_lbl}")

        self._start_block(found_lbl)
        lslot = self._fresh()
        self._emit(f"{lslot} = llvm.load {left_slot_ptr} : !llvm.ptr -> i64")
        rslot = self._fresh()
        self._emit(f"{rslot} = llvm.load {right_slot_ptr} : !llvm.ptr -> i64")
        lvalue = self._decode_elem(lslot, ty.value)
        rvalue = self._decode_elem(rslot, ty.value)
        value_eq = self._emit_equal(lvalue, rvalue, ty.value)
        still = self._load_slot(result_slot, "i1")
        next_result = self._fresh()
        self._emit(f"{next_result} = arith.andi {still}, {value_eq} : i1")
        self._store_slot(result_slot, "i1", next_result)
        self._terminator(f"cf.br {cont_lbl}")

        self._start_block(cont_lbl)
        one = self._const("1", "i64")
        nxt = self._fresh()
        self._emit(f"{nxt} = arith.addi {cur}, {one} : i64")
        self._store_slot(index_slot, "i64", nxt)
        self._terminator(f"cf.br {cond_lbl}")
        self._start_block(exit_lbl)
        return self._load_slot(result_slot, "i1")

    def _cmp_field(self, left: str, right: str, mty: str, index: int, field_ty: str) -> str:
        lf = self._fresh()
        self._emit(f"{lf} = llvm.extractvalue {left}[{index}] : {mty}")
        rf = self._fresh()
        self._emit(f"{rf} = llvm.extractvalue {right}[{index}] : {mty}")
        out = self._fresh()
        self._emit(f"{out} = arith.cmpi eq, {lf}, {rf} : {field_ty}")
        return out

    def _lower_extern_call(self, name: str, expr: ast.CallExpr) -> str | None:
        """Call a C function by its unmangled symbol with its declared C-level
        ABI. Strings cross as their (NUL-terminated) data pointer; a returned
        char* is wrapped back into a Flex String (NULL becomes \"\"); I32 params
        truncate from i64 and I32 results sign-extend back to i64."""
        param_kinds, ret_kind = self.extern_abi[name]
        args: list[str] = []
        types: list[str] = []
        for arg_expr, kind in zip(expr.args, param_kinds, strict=True):
            value = self.lower_expr(arg_expr)
            assert value is not None
            if kind == "str":
                ptr, _length = self._string_parts(value)
                args.append(ptr)
                types.append("!llvm.ptr")
            elif kind == "i32":
                narrowed = self._fresh()
                self._emit(f"{narrowed} = arith.trunci {value} : i64 to i32")
                args.append(narrowed)
                types.append("i32")
            elif kind == "f64":
                args.append(value)
                types.append("f64")
            else:
                args.append(value)
                types.append("i64")
        arg_list = ", ".join(args)
        type_list = ", ".join(types)
        if ret_kind == "unit":
            self._emit(f"func.call @{name}({arg_list}) : ({type_list}) -> ()")
            return None
        out = self._fresh()
        cret = _EXTERN_MLIR_TYPE[ret_kind]
        self._emit(f"{out} = func.call @{name}({arg_list}) : ({type_list}) -> {cret}")
        if ret_kind == "str":
            return self._str_runtime("__flx_cstr_wrap", ["!llvm.ptr"], [out])
        if ret_kind == "i32":
            widened = self._fresh()
            self._emit(f"{widened} = arith.extsi {out} : i32 to i64")
            return widened
        return out

    def _emit_call(self, symbol: str, args: list[str], fn_ty: FnType) -> str | None:
        arg_list = ", ".join(args)
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
        mty = mlir_type(result_ty) if has_value else ""
        if has_value:
            slot = self._alloc_slot(mty)

        def store(value: str) -> None:
            assert slot is not None
            self._store_slot(slot, mty, value)

        cond = self.lower_expr(expr.cond)
        then_lbl = self._label()
        join_lbl = self._label()
        else_lbl = self._label() if expr.else_block is not None else join_lbl
        self._terminator(f"cf.cond_br {cond}, {then_lbl}, {else_lbl}")

        # Track whether control can actually fall through to the join block. A
        # branch that returns (diverges) does not reach the join; if no edge
        # reaches it we must not emit it (an unreachable block with func.return
        # breaks --convert-to-llvm).
        join_reachable = expr.else_block is None  # the false edge falls through

        self._start_block(then_lbl)
        then_val = self.lower_block(expr.then_block)
        if not self.terminated:
            if slot is not None and then_val is not None:
                store(then_val)
            self._terminator(f"cf.br {join_lbl}")
            join_reachable = True

        if expr.else_block is not None:
            self._start_block(else_lbl)
            else_val = self.lower_block(expr.else_block)
            if not self.terminated:
                if slot is not None and else_val is not None:
                    store(else_val)
                self._terminator(f"cf.br {join_lbl}")
                join_reachable = True

        if not join_reachable:
            self.terminated = True
            return None

        self._start_block(join_lbl)
        if slot is not None:
            return self._load_slot(slot, mty)
        return None


_EXTERN_MLIR_TYPE = {"i64": "i64", "i32": "i32", "str": "!llvm.ptr", "f64": "f64"}


def _extern_decls(checked: CheckResult) -> str:
    """C-ABI declarations for `extern fn`s, by their unmangled symbol names."""
    lines = []
    for name in sorted(checked.extern_fns):
        param_kinds, ret_kind = checked.extern_abi[name]
        params = ", ".join(_EXTERN_MLIR_TYPE[k] for k in param_kinds)
        ret = "" if ret_kind == "unit" else f" -> {_EXTERN_MLIR_TYPE[ret_kind]}"
        lines.append(f"func.func private @{name}({params}){ret}\n")
    return "".join(lines)


def emit_program(checked: CheckResult, *, with_tests: bool) -> str:
    """Emit MLIR for all functions, optionally including ``@flx_test_<i>``."""
    lowerer = FunctionLowerer(checked)
    parts = [lowerer.lower_function(fn) for fn in checked.module.functions]
    if with_tests:
        for i, test in enumerate(checked.module.tests):
            parts.append(lowerer.lower_test(test, i))
    body = "\n".join(parts) + "\n"
    decls = BASE_RUNTIME_DECLS + (_RUNTIME_DECLS if with_tests else "") + _extern_decls(checked)
    globals_text = "".join(g + "\n" for g in lowerer.globals)
    return decls + globals_text + body


def emit_module(checked: CheckResult) -> str:
    """Emit MLIR for all functions in the module (tests excluded)."""
    return emit_program(checked, with_tests=False)
