"""Name resolution and type checking for the Flex MVP.

Produces a :class:`CheckResult` mapping each expression to its type (keyed by
node identity) and validates arity, operand types, return types, mutability,
records, ADTs/match (with exhaustiveness), generic instantiation (monomorphic),
`?` propagation, and `uses { ... }` effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from flx.diagnostics import Diagnostic, FlexError, Pos, Span
from flx.syntax import ast
from flx.types import (
    BOOL,
    ERROR,
    I64,
    PRIMITIVES,
    REGION,
    STRING,
    UNIT,
    AdtType,
    FnType,
    PrimType,
    RecordType,
    Type,
    VariantDef,
)

# Builtin generic ADT templates (tag order is fixed): name -> (params, variants),
# where each variant is (name, list of payload TypeExprs).
_TE = ast.TypeExpr
_BUILTIN_ADTS: dict[str, tuple[list[str], list[tuple[str, list[ast.TypeExpr]]]]] = {
    "Result": (["T", "E"], [("Ok", [_TE("T")]), ("Err", [_TE("E")])]),
    "Option": (["T"], [("None", []), ("Some", [_TE("T")])]),
}

# Builtin traits: name -> [(method, [param TypeExprs incl self], return)].
_BUILTIN_TRAITS: dict[str, list[tuple[str, list[ast.TypeExpr], ast.TypeExpr]]] = {
    "Show": [("show", [_TE("Self")], _TE("String"))],
    "Eq": [("eq", [_TE("Self"), _TE("Self")], _TE("Bool"))],
}


def _type_key(ty: Type) -> str:
    """Nominal head used as the impl-table key and in symbol mangling."""
    if isinstance(ty, (PrimType, RecordType, AdtType)):
        return ty.name
    return "?"


def _mono_key(ty: Type) -> str:
    """Structural key for a monomorphization: distinguishes `Option<I64>` from
    `Option<Bool>`, unlike the nominal `_type_key` used for impl lookup.

    Type arguments are joined with `$`, which is illegal in source identifiers, so
    a generic instantiation's key can never collide with a user type's name (a
    user type literally named `Option_I64` keys to `Option_I64`, while `Option<I64>`
    keys to `Option$I64`). The encoding is prefix-notation over fixed-arity type
    heads, so distinct types always produce distinct keys."""
    if isinstance(ty, AdtType) and ty.type_args:
        return ty.name + "".join("$" + _mono_key(a) for a in ty.type_args)
    if isinstance(ty, (PrimType, RecordType, AdtType)):
        return ty.name
    return "?"


def _mangle(trait: str, key: str, method: str) -> str:
    # `$` is illegal in source identifiers, so this can't collide with user code.
    return f"{trait}${key}${method}"


def spec_symbol(name: str, key_tuple: tuple[str, ...]) -> str:
    """Backend symbol for a monomorphized generic-function instantiation."""
    return name + "$" + "$".join(key_tuple)


# name -> (arity or None for variadic-ish, checker). Builtins are checked ad hoc.
_BUILTINS = {"assert", "assert_eq", "assert_ne", "fail", "panic"}

# Capability modules whose calls (e.g. Log.info) are effectful intrinsics.
_EFFECT_MODULES = {"Fs", "Http", "Db", "Log", "Time", "Alloc", "Random", "Process", "Unsafe"}

_ARITH = {"+", "-", "*", "/", "%"}
_COMPARE = {"<", "<=", ">", ">="}
_EQUALITY = {"==", "!="}
_BOOLEAN = {"&&", "||"}

_I64_MAX = 2**63 - 1
_NO_SPAN = Span("<builtin>", Pos(0, 0, 0), Pos(0, 0, 0))

# (module, method) -> (effect, param types, return type).
_INTRINSICS: dict[tuple[str, str], tuple[str, tuple[Type, ...], Type]] = {
    ("Log", "info"): ("Log", (STRING,), UNIT),
    ("Log", "warn"): ("Log", (STRING,), UNIT),
    ("Log", "error"): ("Log", (STRING,), UNIT),
}


@dataclass
class _Binding:
    type: Type
    mutable: bool


@dataclass
class CheckResult:
    module: ast.Module
    expr_types: dict[int, Type]
    functions: dict[str, FnType]
    constructors: set[str]
    method_targets: dict[int, str]  # id(CallExpr) -> resolved impl/spec symbol
    # generic function templates (name -> decl) and the instantiations demanded
    # by call sites. The monomorphizer turns these into concrete functions.
    generic_fns: dict[str, ast.FnDecl] = field(default_factory=dict)
    instantiations: set[tuple[str, tuple[str, ...]]] = field(default_factory=set)
    inst_subst: dict[tuple[str, tuple[str, ...]], dict[str, Type]] = field(default_factory=dict)


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
        self.fn_effects: dict[str, set[str]] = {}
        self.declared_effects: set[str] = set()
        self.record_types: dict[str, RecordType] = {}
        # ADT templates: name -> (type params, [(variant name, payload TypeExprs)]).
        self.adt_templates: dict[str, tuple[list[str], list[tuple[str, list[ast.TypeExpr]]]]] = {}
        self.ctors: dict[str, tuple[str, int]] = {}  # variant name -> (adt name, index)
        self._subst: dict[str, Type] = {}  # active type-parameter substitution
        # traits / impls
        self.traits: dict[str, dict[str, ast.TraitMethod]] = {}
        self.method_index: dict[str, set[str]] = {}  # method -> declaring traits
        self.impls: dict[tuple[str, str], dict[str, str]] = {}  # (trait,key) -> method->symbol
        self.method_targets: dict[int, str] = {}  # id(CallExpr) -> impl symbol
        self._impl_fns: list[ast.FnDecl] = []  # renamed impl methods, emitted as functions
        self._self_type: Type | None = None
        # bounded generics: templates kept out of `functions`; instantiations
        # demanded by call sites and the substitution that produced each one.
        self.generic_fns: dict[str, ast.FnDecl] = {}
        self.instantiations: set[tuple[str, tuple[str, ...]]] = set()
        self.inst_subst: dict[tuple[str, tuple[str, ...]], dict[str, Type]] = {}

    # --- entry ----------------------------------------------------------------

    def check(self) -> CheckResult:
        for adt in self.module.adts:
            for variant in adt.variants:
                if len(variant.payload) > 1:
                    self._err(
                        "TYPE022",
                        f"variant {variant.name!r} has a multi-field payload, "
                        "which is not supported yet",
                        variant.span,
                    )
            self.adt_templates[adt.name] = (
                adt.type_params,
                [(v.name, v.payload) for v in adt.variants],
            )
        for name, template in _BUILTIN_ADTS.items():
            self.adt_templates.setdefault(name, template)
        for adt_name, (_, variants) in self.adt_templates.items():
            for i, (vname, _) in enumerate(variants):
                self.ctors[vname] = (adt_name, i)

        for record in self.module.records:
            fields = tuple((f.name, self._resolve_type(f.type)) for f in record.fields)
            self.record_types[record.name] = RecordType(record.name, fields)

        self._register_traits()

        for fn in self.module.functions:
            if fn.name in self.functions or fn.name in self.generic_fns:
                self._err("TYPE002", f"function {fn.name!r} is already defined", fn.span)
            if fn.type_params:
                # A template: type params are unresolved here. Its concrete
                # instantiations are checked (and registered) by the monomorphizer.
                self._register_generic_fn(fn)
                continue
            params = tuple(self._resolve_type(p.type) for p in fn.params)
            ret = self._resolve_type(fn.return_type) if fn.return_type else UNIT
            self.functions[fn.name] = FnType(params, ret)
            self.fn_effects[fn.name] = set(fn.effects)

        self._register_impls()

        for fn in self.module.functions:
            if fn.name in self.generic_fns:
                continue
            self._check_fn(fn)
        for impl_fn in self._impl_fns:
            self._check_fn(impl_fn)
        for test in self.module.tests:
            self._check_test(test)

        if self.diags:
            raise FlexError(self.diags)
        # Emit impl methods as ordinary (mangled) functions for the backend, and
        # drop generic templates (the monomorphizer emits concrete copies instead).
        kept = [
            it for it in self.module.items if not (isinstance(it, ast.FnDecl) and it.type_params)
        ]
        module = replace(self.module, items=[*kept, *self._impl_fns])
        return CheckResult(
            module,
            self.expr_types,
            self.functions,
            set(self.ctors),
            self.method_targets,
            generic_fns=self.generic_fns,
            instantiations=self.instantiations,
            inst_subst=self.inst_subst,
        )

    # --- traits / impls -------------------------------------------------------

    def _register_traits(self) -> None:
        for tname, sigs in _BUILTIN_TRAITS.items():
            self.traits[tname] = {
                m: ast.TraitMethod(
                    m,
                    [ast.Param(f"a{i}", pe, _NO_SPAN) for i, pe in enumerate(ps)],
                    ret,
                    _NO_SPAN,
                )
                for m, ps, ret in sigs
            }
        for trait in self.module.traits:
            if trait.name in self.traits and trait.name not in _BUILTIN_TRAITS:
                self._err("TRAIT001", f"trait {trait.name!r} is already defined", trait.span)
            seen: dict[str, ast.TraitMethod] = {}
            for method in trait.methods:
                if method.name in seen:
                    self._err("TRAIT002", f"duplicate method {method.name!r}", method.span)
                seen[method.name] = method
            self.traits[trait.name] = seen
        for tname, methods in self.traits.items():
            for mname in methods:
                self.method_index.setdefault(mname, set()).add(tname)

    def _register_impls(self) -> None:
        for impl in self.module.impls:
            if impl.trait not in self.traits:
                self._err("IMPL001", f"unknown trait {impl.trait!r}", impl.span)
                continue
            impl_ty = self._resolve_type(ast.TypeExpr(impl.type_name, [], impl.span))
            if impl_ty is ERROR:
                continue
            key = _type_key(impl_ty)
            if (impl.trait, key) in self.impls:
                self._err(
                    "IMPL006", f"conflicting impl {impl.trait} for {impl.type_name}", impl.span
                )
            table: dict[str, str] = {}
            self.impls[(impl.trait, key)] = table
            trait_methods = self.traits[impl.trait]
            provided = {m.name for m in impl.methods}
            for mname in trait_methods:
                if mname not in provided:
                    self._err("IMPL003", f"impl is missing method {mname!r}", impl.span)
            for method in impl.methods:
                sig = trait_methods.get(method.name)
                if sig is None:
                    self._err(
                        "IMPL004",
                        f"{method.name!r} is not a method of {impl.trait}",
                        method.span,
                    )
                    continue
                self._check_impl_conformance(method, sig, impl_ty)
                symbol = _mangle(impl.trait, key, method.name)
                self._self_type = impl_ty
                params = tuple(self._resolve_type(p.type) for p in method.params)
                ret = self._resolve_type(method.return_type) if method.return_type else UNIT
                self._self_type = None
                self.functions[symbol] = FnType(params, ret)
                self.fn_effects[symbol] = set(method.effects)
                table[method.name] = symbol
                self._impl_fns.append(replace(method, name=symbol))

    def _check_impl_conformance(
        self, method: ast.FnDecl, sig: ast.TraitMethod, impl_ty: Type
    ) -> None:
        self._self_type = impl_ty
        want_params = tuple(self._resolve_type(p.type) for p in sig.params)
        want_ret = self._resolve_type(sig.return_type) if sig.return_type else UNIT
        self._self_type = None
        got_params = tuple(self._resolve_type(p.type) for p in method.params)
        got_ret = self._resolve_type(method.return_type) if method.return_type else UNIT
        if want_params != got_params or not _same(want_ret, got_ret):
            self._err(
                "IMPL005",
                f"method {method.name!r} does not match the trait signature",
                method.span,
            )

    def _is_method_call(self, recv_ty: Type, name: str) -> bool:
        if isinstance(recv_ty, RecordType) and any(f == name for f, _ in recv_ty.fields):
            return False  # field access takes priority over methods
        return name in self.method_index

    def _infer_method_call(self, callee: ast.MemberExpr, recv_ty: Type, call: ast.CallExpr) -> Type:
        key = _type_key(recv_ty)
        candidates = [
            trait
            for trait in self.method_index.get(callee.name, set())
            if callee.name in self.impls.get((trait, key), {})
        ]
        if not candidates:
            for arg in call.args:
                self._check_expr(arg)
            self._err(
                "DISP001", f"no impl provides method {callee.name!r} for {recv_ty}", call.span
            )
            return ERROR
        if len(candidates) > 1:
            self._err("DISP003", f"ambiguous method {callee.name!r} for {recv_ty}", call.span)
        symbol = self.impls[(candidates[0], key)][callee.name]
        fn_ty = self.functions[symbol]
        self._expect(fn_ty.params[0], recv_ty, callee.obj.span, "receiver")
        rest = fn_ty.params[1:]
        if len(call.args) != len(rest):
            self._err(
                "TYPE005",
                f"method {callee.name!r} expects {len(rest)} argument(s), got {len(call.args)}",
                call.span,
            )
        for arg, exp in zip(call.args, rest, strict=False):
            self._expect(exp, self._check_expr(arg, exp), arg.span, "argument")
        self._require_effects(self.fn_effects.get(symbol, set()), call.span)
        self.method_targets[id(call)] = symbol
        return fn_ty.ret

    # --- bounded generics -----------------------------------------------------

    def _register_generic_fn(self, fn: ast.FnDecl) -> None:
        seen: set[str] = set()
        for tp in fn.type_params:
            if tp.name in seen:
                self._err("TYPE002", f"duplicate type parameter {tp.name!r}", tp.span)
            seen.add(tp.name)
            for bound in tp.bounds:
                if bound not in self.traits:
                    self._err("BOUND004", f"unknown trait {bound!r} in bound", tp.span)
        self.generic_fns[fn.name] = fn

    def _infer_generic_call(self, name: str, call: ast.CallExpr, expected: Type | None) -> Type:
        template = self.generic_fns[name]
        tp_names = {tp.name for tp in template.type_params}
        arg_types = [self._check_expr(a) for a in call.args]
        if len(call.args) != len(template.params):
            self._err(
                "TYPE005",
                f"{name!r} expects {len(template.params)} argument(s), got {len(call.args)}",
                call.span,
            )
            return ERROR
        # Solve the substitution positionally: a parameter written exactly as a
        # type-parameter name binds it to that argument's type.
        subst: dict[str, Type] = {}
        for param, at in zip(template.params, arg_types, strict=True):
            if param.type.name in tp_names and not param.type.args and at is not ERROR:
                subst.setdefault(param.type.name, at)
        for tp in template.type_params:
            if tp.name not in subst:
                self._err(
                    "BOUND003",
                    f"cannot infer type parameter {tp.name!r} of {name!r} from its arguments",
                    call.span,
                )
                return ERROR
        # Check declared bounds against the chosen concrete types.
        for tp in template.type_params:
            concrete = subst[tp.name]
            for bound in tp.bounds:
                if (bound, _type_key(concrete)) not in self.impls:
                    self._err(
                        "BOUND001",
                        f"{concrete} does not satisfy bound {bound!r} required by {name!r}",
                        call.span,
                    )
        # Re-check the arguments against the substituted parameter types.
        saved = self._subst
        self._subst = {**saved, **subst}
        try:
            for param, at, arg in zip(template.params, arg_types, call.args, strict=True):
                self._expect(self._resolve_type(param.type), at, arg.span, "argument")
            ret = self._resolve_type(template.return_type) if template.return_type else UNIT
        finally:
            self._subst = saved
        self._require_effects(set(template.effects), call.span)
        key_tuple = tuple(_mono_key(subst[tp.name]) for tp in template.type_params)
        inst = (name, key_tuple)
        self.instantiations.add(inst)
        self.inst_subst[inst] = subst
        self.method_targets[id(call)] = spec_symbol(name, key_tuple)
        return ret

    # --- declarations ---------------------------------------------------------

    def _check_fn(self, fn: ast.FnDecl) -> None:
        self.scope = _Scope()
        self.in_test = False
        self.declared_effects = set(fn.effects)
        fn_ty = self.functions[fn.name]
        seen: set[str] = set()
        for param, ptype in zip(fn.params, fn_ty.params, strict=True):
            if param.name in seen:
                self._err("NAME002", f"duplicate parameter name {param.name!r}", param.span)
            seen.add(param.name)
            self.scope.define(param.name, _Binding(ptype, mutable=False))
        self.return_type = fn_ty.ret
        body_ty = self._check_block(fn.body, fn_ty.ret)
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
        self.declared_effects = set(test.effects)
        self.return_type = UNIT
        self._check_block(test.body)

    # --- statements / blocks --------------------------------------------------

    def _check_block(self, block: ast.Block, expected: Type | None = None) -> Type:
        self.scope.push()
        result: Type = UNIT
        last = block.stmts[-1] if block.stmts else None
        for stmt in block.stmts:
            result = self._check_stmt(stmt, expected if stmt is last else None)
        # The block's value is its trailing expression, else Unit.
        value = result if block.tail is not None else UNIT
        self.scope.pop()
        return value

    def _check_stmt(self, stmt: ast.Stmt, expected: Type | None = None) -> Type:
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
        if isinstance(stmt, ast.ForStmt):
            self._err("TYPE021", "`for` is only supported inside comptime for now", stmt.span)
            return UNIT
        if isinstance(stmt, ast.ReturnStmt):
            actual = (
                self._check_expr(stmt.value, self.return_type) if stmt.value is not None else UNIT
            )
            span = stmt.value.span if stmt.value is not None else stmt.span
            self._expect(self.return_type, actual, span, "return value")
            return UNIT
        if isinstance(stmt, ast.ExprStmt):
            return self._check_expr(stmt.expr, expected)
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

    def _check_expr(self, expr: ast.Expr, expected: Type | None = None) -> Type:
        ty = self._infer(expr, expected)
        self.expr_types[id(expr)] = ty
        return ty

    def _infer(self, expr: ast.Expr, expected: Type | None) -> Type:
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
            return self._infer_name(expr, expected)
        if isinstance(expr, ast.UnaryExpr):
            return self._infer_unary(expr)
        if isinstance(expr, ast.BinaryExpr):
            return self._infer_binary(expr)
        if isinstance(expr, ast.CallExpr):
            return self._infer_call(expr, expected)
        if isinstance(expr, ast.IfExpr):
            return self._infer_if(expr, expected)
        if isinstance(expr, ast.RegionExpr):
            return self._infer_region(expr)
        if isinstance(expr, ast.RecordExpr):
            return self._infer_record(expr)
        if isinstance(expr, ast.RecordUpdateExpr):
            return self._infer_record_update(expr)
        if isinstance(expr, ast.MemberExpr):
            return self._infer_member(expr)
        if isinstance(expr, ast.MatchExpr):
            return self._infer_match(expr, expected)
        if isinstance(expr, ast.TryExpr):
            return self._infer_try(expr)
        return ERROR

    def _infer_record(self, expr: ast.RecordExpr) -> Type:
        self._check_duplicate_fields(expr.fields)
        names = {f.name for f in expr.fields}
        matches = [rt for rt in self.record_types.values() if {n for n, _ in rt.fields} == names]
        if len(matches) != 1:
            for f in expr.fields:
                self._check_expr(f.value)
            detail = "ambiguous" if matches else "no record type matches"
            self._err("TYPE014", f"cannot determine record type ({detail})", expr.span)
            return ERROR
        rt = matches[0]
        field_types = dict(rt.fields)
        for f in expr.fields:
            self._expect(
                field_types[f.name], self._check_expr(f.value), f.value.span, f"field {f.name!r}"
            )
        return rt

    def _check_duplicate_fields(self, fields: list[ast.FieldInit]) -> None:
        seen: set[str] = set()
        for f in fields:
            if f.name in seen:
                self._err("TYPE020", f"duplicate field {f.name!r} in record literal", f.span)
            seen.add(f.name)

    def _infer_record_update(self, expr: ast.RecordUpdateExpr) -> Type:
        self._check_duplicate_fields(expr.fields)
        base_ty = self._check_expr(expr.base)
        if not isinstance(base_ty, RecordType):
            if base_ty is not ERROR:
                self._err(
                    "TYPE017", f"record update requires a record, found {base_ty}", expr.base.span
                )
            for f in expr.fields:
                self._check_expr(f.value)
            return ERROR
        field_types = dict(base_ty.fields)
        for f in expr.fields:
            value_ty = self._check_expr(f.value)
            if f.name not in field_types:
                self._err("TYPE015", f"record {base_ty.name} has no field {f.name!r}", f.span)
            else:
                self._expect(field_types[f.name], value_ty, f.value.span, f"field {f.name!r}")
        return base_ty

    def _infer_member(self, expr: ast.MemberExpr) -> Type:
        # `Type.Variant` path access to a (payloadless) constructor, e.g.
        # MathError.DivideByZero.
        if isinstance(expr.obj, ast.NameExpr) and expr.obj.name in self.adt_templates:
            return self._infer_ctor(expr.name, [], None, expr.span)
        obj_ty = self._check_expr(expr.obj)
        if isinstance(obj_ty, RecordType):
            for fname, ftype in obj_ty.fields:
                if fname == expr.name:
                    return ftype
            self._err("TYPE015", f"record {obj_ty.name} has no field {expr.name!r}", expr.span)
            return ERROR
        if obj_ty is ERROR:
            return ERROR
        self._err("TYPE010", f"cannot access field .{expr.name} on {obj_ty}", expr.span)
        return ERROR

    def _infer_region(self, expr: ast.RegionExpr) -> Type:
        # Shallow MVP regions: the name binds a Region capability in the body and
        # the block's value is the region expression's value. Escape analysis is
        # deferred (scalars are copied out, so nothing can dangle yet).
        self.scope.push()
        self.scope.define(expr.name, _Binding(REGION, mutable=False))
        ty = self._check_block(expr.body)
        self.scope.pop()
        if ty is REGION:
            self._err("REGION001", "a region cannot yield a region capability", expr.span)
            return ERROR
        return ty

    def _infer_name(self, expr: ast.NameExpr, expected: Type | None) -> Type:
        binding = self.scope.lookup(expr.name)
        if binding is not None:
            return binding.type
        if expr.name in self.ctors:  # bare variant, e.g. None / Red
            return self._infer_ctor(expr.name, [], expected, expr.span)
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
        if op == "++":
            self._expect(STRING, left, expr.left.span, "left operand of `++`")
            self._expect(STRING, right, expr.right.span, "right operand of `++`")
            return STRING
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
                self._err("TYPE003", f"cannot compare {left} with {right}", expr.span)
            elif not _is_comparable(left):
                self._err(
                    "TYPE019", f"`{op}` is not supported for {left} (contains a String)", expr.span
                )
            return BOOL
        return ERROR

    def _infer_call(self, expr: ast.CallExpr, expected: Type | None) -> Type:
        callee = expr.callee
        if isinstance(callee, ast.NameExpr):
            if callee.name in _BUILTINS:
                return self._check_builtin(callee.name, expr)
            if callee.name == "to_str":  # prelude: I64 -> String
                if len(expr.args) == 1:
                    self._expect(I64, self._check_expr(expr.args[0]), expr.args[0].span, "argument")
                else:
                    self._err("TYPE006", "to_str expects 1 argument", expr.span)
                return STRING
            if callee.name in self.ctors:  # constructor call, e.g. Ok(x)
                return self._infer_ctor(callee.name, expr.args, expected, expr.span)
            if callee.name in self.generic_fns:  # bounded generic, monomorphized
                return self._infer_generic_call(callee.name, expr, expected)
            if callee.name in self.functions:
                fn_ty = self.functions[callee.name]
                self._check_args(callee.name, fn_ty, expr)
                self._require_effects(self.fn_effects.get(callee.name, set()), expr.span)
                return fn_ty.ret
        if isinstance(callee, ast.MemberExpr) and isinstance(callee.obj, ast.NameExpr):
            if callee.obj.name in _EFFECT_MODULES:
                return self._infer_intrinsic(callee.obj.name, callee.name, expr)
            if callee.obj.name in self.adt_templates and callee.name in self.ctors:
                # Qualified constructor with payload, e.g. E.Code(x).
                return self._infer_ctor(callee.name, expr.args, expected, expr.span)
        if isinstance(callee, ast.MemberExpr):
            recv_ty = self._check_expr(callee.obj)
            if self._is_method_call(recv_ty, callee.name):
                return self._infer_method_call(callee, recv_ty, expr)
        callee_ty = self._check_expr(callee)
        if isinstance(callee_ty, FnType):
            self._check_args("call", callee_ty, expr)
            return callee_ty.ret
        if callee_ty is not ERROR:
            self._err("TYPE004", "expression is not callable", callee.span)
        return ERROR

    def _infer_intrinsic(self, module: str, method: str, call: ast.CallExpr) -> Type:
        sig = _INTRINSICS.get((module, method))
        if sig is None:
            for arg in call.args:
                self._check_expr(arg)
            self._err("TYPE010", f"unknown operation {module}.{method}", call.span)
            return ERROR
        effect, params, ret = sig
        if len(call.args) != len(params):
            self._err("TYPE005", f"{module}.{method} expects {len(params)} argument(s)", call.span)
        for arg, expected in zip(call.args, params, strict=False):
            self._expect(expected, self._check_expr(arg), arg.span, "argument")
        for extra in call.args[len(params) :]:
            self._check_expr(extra)
        self._require_effects({effect}, call.span)
        return ret

    def _require_effects(self, effects: set[str], span: Span) -> None:
        site = "test" if self.in_test else "function"
        for eff in sorted(effects):
            if eff not in self.declared_effects:
                self._err(
                    "EFFECT001",
                    f"this call requires effect {eff!r}, which the {site} does not declare",
                    span,
                    help=f"add {eff} to its `uses {{ ... }}`",
                )

    # --- ADTs / constructors / match / `?` ------------------------------------

    def _infer_ctor(
        self, name: str, args: list[ast.Expr], expected: Type | None, span: Span
    ) -> Type:
        adt_name, vidx = self.ctors[name]
        params, variants = self.adt_templates[adt_name]
        payload_exprs = variants[vidx][1]
        arg_types = [self._check_expr(a) for a in args]
        if len(args) != len(payload_exprs):
            self._err(
                "TYPE005",
                f"{name!r} expects {len(payload_exprs)} argument(s), got {len(args)}",
                span,
            )
        # Resolve type parameters from the expected type, then from arguments.
        subst: dict[str, Type] = {}
        if (
            isinstance(expected, AdtType)
            and expected.name == adt_name
            and len(expected.type_args) == len(params)
        ):
            subst = dict(zip(params, expected.type_args, strict=True))
        for pe, at in zip(payload_exprs, arg_types, strict=False):
            if pe.name in params and not pe.args and pe.name not in subst:
                subst[pe.name] = at
        if any(p not in subst for p in params):
            self._err("TYPE016", f"cannot infer type arguments for {name!r} from context", span)
            return ERROR
        adt = self._instantiate(adt_name, [subst[p] for p in params], span)
        for arg, at, pty in zip(args, arg_types, adt.variants[vidx].payload, strict=False):
            self._expect(pty, at, arg.span, f"argument to {name!r}")
        return adt

    def _infer_try(self, expr: ast.TryExpr) -> Type:
        inner = self._check_expr(expr.expr)
        if not (isinstance(inner, AdtType) and inner.name == "Result"):
            if inner is not ERROR:
                self._err("QUEST001", f"`?` requires a Result, found {inner}", expr.span)
            return ERROR
        payload_t, err_e = inner.type_args
        if self.in_test:
            return payload_t  # `?` in a test propagates failure as a failed test
        ret = self.return_type
        if not (isinstance(ret, AdtType) and ret.name == "Result"):
            self._err("QUEST001", "`?` used outside a Result-returning function", expr.span)
            return ERROR
        if not _same(ret.type_args[1], err_e):
            self._err(
                "QUEST001",
                f"`?` error type {err_e} is incompatible with {ret.type_args[1]}",
                expr.span,
            )
        return payload_t

    def _infer_match(self, expr: ast.MatchExpr, expected: Type | None) -> Type:
        scrut = self._check_expr(expr.scrutinee)
        if not isinstance(scrut, AdtType):
            for arm in expr.arms:
                self.scope.push()
                self._bind_pattern(arm.pattern, scrut, {}, set())
                self._check_expr(arm.body, expected)
                self.scope.pop()
            if scrut is not ERROR:
                self._err("TYPE018", f"match requires an ADT, found {scrut}", expr.scrutinee.span)
            return ERROR
        variants = {v.name: v for v in scrut.variants}
        covered: set[str] = set()
        catchall = False
        result: Type | None = None
        for arm in expr.arms:
            self.scope.push()
            if self._bind_pattern(arm.pattern, scrut, variants, covered):
                catchall = True
            body_ty = self._check_expr(arm.body, expected)
            self.scope.pop()
            if result is None:
                result = body_ty
            elif not _same(result, body_ty):
                self._err(
                    "TYPE008",
                    f"match arms have mismatched types: {result} vs {body_ty}",
                    arm.span,
                )
        if not catchall and covered != set(variants):
            missing = ", ".join(sorted(set(variants) - covered))
            self._err("MATCH001", f"non-exhaustive match; missing {missing}", expr.span)
        return result if result is not None else UNIT

    def _bind_pattern(
        self,
        pattern: ast.Pattern,
        scrut_ty: Type,
        variants: dict[str, VariantDef],
        covered: set[str],
    ) -> bool:
        if isinstance(pattern, ast.WildcardPattern):
            return True
        if isinstance(pattern, ast.BindPattern):
            self.scope.define(pattern.name, _Binding(scrut_ty, mutable=False))
            return True
        if isinstance(pattern, ast.CtorPattern):
            variant = variants.get(pattern.name)
            if variant is None:
                self._err(
                    "MATCH003", f"{pattern.name!r} is not a variant of {scrut_ty}", pattern.span
                )
                return False
            if pattern.name in covered:
                self._err("MATCH002", f"duplicate match arm for {pattern.name!r}", pattern.span)
            covered.add(pattern.name)
            if len(pattern.args) != len(variant.payload):
                self._err(
                    "TYPE005",
                    f"{pattern.name!r} expects {len(variant.payload)} pattern argument(s)",
                    pattern.span,
                )
            for sub, pty in zip(pattern.args, variant.payload, strict=False):
                self._bind_subpattern(sub, pty)
        return False

    def _bind_subpattern(self, pattern: ast.Pattern, ty: Type) -> None:
        if isinstance(pattern, ast.BindPattern):
            self.scope.define(pattern.name, _Binding(ty, mutable=False))
        elif isinstance(pattern, ast.CtorPattern):
            # Nested constructor patterns aren't lowered yet (the backend keys
            # cf.switch on the outer tag only), so reject rather than mis-compile.
            self._err(
                "MATCH004",
                "nested constructor patterns are not supported yet; "
                "bind the payload and match it separately",
                pattern.span,
            )

    def _check_args(self, name: str, fn_ty: FnType, call: ast.CallExpr) -> None:
        if len(call.args) != len(fn_ty.params):
            self._err(
                "TYPE005",
                f"{name!r} expects {len(fn_ty.params)} argument(s), got {len(call.args)}",
                call.span,
            )
        for arg, expected in zip(call.args, fn_ty.params, strict=False):
            actual = self._check_expr(arg, expected)
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
        if name == "assert":
            if len(call.args) == 1:
                self._expect(BOOL, self._check_expr(call.args[0]), call.args[0].span, "assertion")
            else:
                self._err("TYPE006", "assert expects 1 argument", call.span)
        elif name in ("assert_eq", "assert_ne"):
            if len(call.args) != 2:
                for arg in call.args:
                    self._check_expr(arg)
                self._err("TYPE006", f"{name} expects 2 arguments", call.span)
            else:
                # Check the second operand against the first so constructors like
                # `Err(...)` get the expected type.
                a = self._check_expr(call.args[0])
                b = self._check_expr(call.args[1], a)
                if not _same(a, b):
                    self._err("TYPE003", f"cannot compare {a} with {b}", call.span)
                elif not _is_comparable(a):
                    self._err(
                        "TYPE019", f"{name} is not supported for {a} (contains a String)", call.span
                    )
        elif name in ("fail", "panic"):
            for arg in call.args:
                self._check_expr(arg)
            if len(call.args) != 1:
                self._err("TYPE006", f"{name} expects 1 argument", call.span)
            elif self.expr_types[id(call.args[0])] not in (STRING, ERROR):
                self._err("TYPE007", f"{name} expects a String message", call.args[0].span)
        return UNIT

    def _infer_if(self, expr: ast.IfExpr, expected: Type | None) -> Type:
        cond = self._check_expr(expr.cond)
        self._expect(BOOL, cond, expr.cond.span, "if condition")
        then_ty = self._check_block(expr.then_block, expected)
        if expr.else_block is None:
            return UNIT
        else_ty = self._check_block(expr.else_block, expected)
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
        if type_expr.name == "Self" and not type_expr.args:
            if self._self_type is not None:
                return self._self_type
            self._err("TRAIT008", "`Self` is only valid in a trait or impl method", type_expr.span)
            return ERROR
        if type_expr.name in self._subst and not type_expr.args:
            return self._subst[type_expr.name]
        if type_expr.name in PRIMITIVES and not type_expr.args:
            return PRIMITIVES[type_expr.name]
        if type_expr.name in self.record_types and not type_expr.args:
            return self.record_types[type_expr.name]
        if type_expr.name in self.adt_templates:
            args = [self._resolve_type(a) for a in type_expr.args]
            return self._instantiate(type_expr.name, args, type_expr.span)
        self._err("TYPE001", f"unknown type {type_expr.name!r}", type_expr.span)
        return ERROR

    def _instantiate(self, adt_name: str, type_args: list[Type], span: Span | None) -> AdtType:
        params, variants = self.adt_templates[adt_name]
        if len(type_args) != len(params):
            self._err(
                "TYPE013",
                f"type {adt_name!r} expects {len(params)} type argument(s), got {len(type_args)}",
                span,
            )
            type_args = (type_args + [ERROR] * len(params))[: len(params)]
        saved = self._subst
        self._subst = dict(zip(params, type_args, strict=True))
        defs = tuple(
            VariantDef(vname, tuple(self._resolve_type(pe) for pe in payload))
            for vname, payload in variants
        )
        self._subst = saved
        return AdtType(adt_name, defs, tuple(type_args))

    def _expect(self, expected: Type, actual: Type, span: Span, what: str) -> None:
        if expected is ERROR or actual is ERROR:
            return
        if not _same(expected, actual):
            self._err("TYPE003", f"{what} has type {actual}, expected {expected}", span)

    def _err(self, code: str, message: str, span: Span | None, *, help: str | None = None) -> None:
        self.diags.append(Diagnostic(code, message, span, help=help))


def _same(a: Type, b: Type) -> bool:
    return a is ERROR or b is ERROR or a == b


def _is_comparable(ty: Type) -> bool:
    """Whether `==`/`!=`/assert_eq can be lowered for this type (no strings yet)."""
    if ty is STRING:
        return False
    if isinstance(ty, RecordType):
        return all(_is_comparable(t) for _, t in ty.fields)
    if isinstance(ty, AdtType):
        return all(_is_comparable(t) for v in ty.variants for t in v.payload)
    return True


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
