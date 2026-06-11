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

from flx.backend.runtime import BASE_RUNTIME_DECLS
from flx.sema.check import CheckResult
from flx.syntax import ast
from flx.types import BOOL, I64, STRING, UNIT, AdtType, FnType, ListType, RecordType, Type

_ARITH_OP = {"+": "addi", "-": "subi", "*": "muli"}
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
    "func.func private @__flx_assert_streq_fail(!llvm.ptr, i64, !llvm.ptr, i64)\n"
    "func.func private @__flx_assert_strne_fail(!llvm.ptr, i64)\n"
    "func.func private @__flx_explicit_fail()\n"
    "func.func private @__flx_fail_msg(!llvm.ptr, i64)\n"
)


def mlir_type(ty: Type) -> str:
    if ty is I64:
        return "i64"
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
        raise BackendError(
            "list values are not supported in native builds yet (run on the interpreter)"
        )
    raise BackendError(f"type {ty} has no MLIR representation yet")


def _is_aggregate(mty: str) -> bool:
    return mty.startswith("!llvm")


def _is_enum(ty: AdtType) -> bool:
    return all(not v.payload for v in ty.variants)


def _payload_inline(ty: Type) -> bool:
    """Whether a payload of this type lives in the i64 slot by value. Everything
    else (strings, records, non-enum ADTs — recursion included — and any
    multi-field payload) is boxed on the heap behind the slot."""
    return ty is I64 or ty is BOOL or ty is UNIT or (isinstance(ty, AdtType) and _is_enum(ty))


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
        if isinstance(stmt, ast.WhileStmt):
            self.lower_while(stmt)
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
            raise BackendError(
                "list literals are not supported in native builds yet (run on the interpreter)"
            )
        raise BackendError(f"cannot lower expression {type(expr).__name__}")

    def _const(self, literal: str, mty: str) -> str:
        out = self._fresh()
        self._emit(f"{out} = arith.constant {literal} : {mty}")
        return out

    # --- strings: {ptr, i64 length} backed by a module-level global -----------

    def _lower_string(self, expr: ast.StringLit) -> str:
        data = expr.value.encode("utf-8")
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
            ty = payload[0]
            if _payload_inline(ty):
                return self._narrow_from_i64(slot, ty)
            return self._unbox(slot, mlir_type(ty))
        box_mty = _payload_box_type(payload)
        struct = self._unbox(slot, box_mty)
        out = self._fresh()
        self._emit(f"{out} = llvm.extractvalue {struct}[{index}] : {box_mty}")
        return out

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
        assert _payload_inline(ty)
        return value  # I64, Unit (already i64 0), or an enum tag

    def _narrow_from_i64(self, value: str, ty: Type) -> str:
        if ty is BOOL:
            out = self._fresh()
            self._emit(f"{out} = arith.trunci {value} : i64 to i1")
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
        if expr.op == "-":
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
        if op in _DIV_OP:
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
                    runtime_fn = "__flx_print" if method == "print" else "__flx_log"
                    self._emit(f"func.call @{runtime_fn}({ptr}, {length}) : (!llvm.ptr, i64) -> ()")
                    return None
                if obj.name == "Fs" and method == "read_line":
                    return self._str_runtime("__flx_read_line", [], [])
                if obj.name == "Time" and method == "monotonic_ms":
                    out = self._fresh()
                    self._emit(f"{out} = func.call @__flx_monotonic_ms() : () -> i64")
                    return out
            return None
        if not isinstance(expr.callee, ast.NameExpr):
            raise BackendError("only direct function calls are supported")
        name = expr.callee.name
        # Bounded-generic call -> direct call to the monomorphized specialization.
        symbol = self.method_targets.get(id(expr))
        if symbol is not None:
            spec_ty = self.functions[symbol]
            args = [self._materialize(self.lower_expr(a), a) for a in expr.args]
            return self._emit_call(f"flx_{symbol}", args, spec_ty)
        if name in self.extern_fns:
            return self._lower_extern_call(name, expr)
        if name == "to_str":  # prelude: I64 -> String
            arg = self.lower_expr(expr.args[0])
            assert arg is not None
            return self._str_runtime("__flx_int_to_str", ["i64"], [arg])
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
        if not _is_aggregate(mty):
            out = self._fresh()
            self._emit(f"{out} = arith.cmpi eq, {left}, {right} : {mty}")
            return out
        if isinstance(ty, AdtType):
            tag_eq = self._cmp_field(left, right, mty, 0, "i32")
            payload_eq = self._cmp_field(left, right, mty, 1, "i64")
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


_EXTERN_MLIR_TYPE = {"i64": "i64", "i32": "i32", "str": "!llvm.ptr"}


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
