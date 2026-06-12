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
    BYTES,
    ERROR,
    F64,
    I8,
    I16,
    I32,
    I64,
    PRIMITIVES,
    REGION,
    STRING,
    U8,
    U16,
    U32,
    U64,
    UNIT,
    AdtType,
    FnType,
    ListType,
    MapType,
    PrimType,
    RecordType,
    Type,
    VariantDef,
    int_bounds,
    is_int_type,
)

# Builtin generic ADT templates (tag order is fixed): name -> (params, variants),
# where each variant is (name, list of payload TypeExprs).
_TE = ast.TypeExpr
_BUILTIN_ADTS: dict[str, tuple[list[str], list[tuple[str, list[ast.TypeExpr]]]]] = {
    "Result": (["T", "E"], [("Ok", [_TE("T")]), ("Err", [_TE("E")])]),
    "Option": (["T"], [("None", []), ("Some", [_TE("T")])]),
    "MapEntry": (["V"], [("MapEntry", [_TE("String"), _TE("V")])]),
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


def _type_enc(ty: Type) -> str:
    """Self-delimiting, injective encoding of a type, used inside mangled symbols
    and monomorphization keys.

    Prefix notation with an explicit arity in front of every type head, so a
    concatenation of encodings is unambiguously decodable and distinct types
    always encode differently: `I64` -> ``0$I64``, `Option<I64>` -> ``1$Option$0$I64``,
    `Pair<I64, Bool>` -> ``2$Pair$0$I64$0$Bool``. Crucially the nesting is
    recoverable — `h<Pair<I64, Bool>>` (one arg) and a hypothetical `h<Pair, I64,
    Bool>` (three args) encode to different argument lists rather than collapsing
    to one ``$``-joined string. `$` is illegal in source identifiers, so the name
    tokens never clash with the numeric arity tokens."""
    if isinstance(ty, FnType):
        fn_inner = "$".join(_type_enc(t) for t in [*ty.params, ty.ret])
        return f"{len(ty.params) + 1}$->${fn_inner}"  # '->' is not a source name
    if isinstance(ty, AdtType) and ty.type_args:
        inner = "$".join(_type_enc(a) for a in ty.type_args)
        return f"{len(ty.type_args)}${ty.name}${inner}"
    if isinstance(ty, ListType):
        return f"1$List${_type_enc(ty.elem)}"
    if isinstance(ty, MapType):
        return f"1$Map${_type_enc(ty.value)}"  # the key is always String
    if isinstance(ty, (PrimType, RecordType, AdtType)):
        return f"0${ty.name}"
    return "0$?"


def _mono_key(ty: Type) -> str:
    """Monomorphization dedup key for a concrete type argument — the injective
    `_type_enc`, so two distinct instantiations never share a key/symbol."""
    return _type_enc(ty)


# Every backend symbol that is not a plain user function carries a reserved
# leading kind tag (`t$` trait-impl method, `g$` generic spec, and `f$` reserved
# for module-qualified functions once modules land). Plain user functions stay
# tag-free and, since `$` is illegal in source identifiers, contain no `$` at all
# — so the four namespaces are provably disjoint and a module prefix can be added
# later without making any of them collide.


def _mangle(trait: str, key: str, method: str) -> str:
    """Backend symbol for a trait-impl method (`key` is the impl type's nominal
    name). Tagged `t$` and self-delimiting (the type is arity-framed `0$<key>`),
    so it cannot collide with a generic spec, a module function, or a plain one."""
    return f"t${trait}$0${key}${method}"


def spec_symbol(name: str, key_tuple: tuple[str, ...]) -> str:
    """Backend symbol for a monomorphized generic instantiation. Tagged `g$` with
    an explicit type-argument count, so the number and nesting of type arguments
    is recoverable and distinct instantiations always get distinct symbols."""
    inner = "$".join(key_tuple)
    return f"g${name}${len(key_tuple)}" + (f"${inner}" if key_tuple else "")


# name -> (arity or None for variadic-ish, checker). Builtins are checked ad hoc.
_BUILTINS = {"assert", "assert_eq", "assert_ne", "fail", "panic"}
_INT_CONVERSIONS: dict[str, Type] = {
    "to_i8": I8,
    "to_u8": U8,
    "to_i16": I16,
    "to_u16": U16,
    "to_i32": I32,
    "to_u32": U32,
    "to_i64": I64,
    "to_u64": U64,
}
_CONVERSIONS = {"to_str", "to_f64", *_INT_CONVERSIONS}

# Prelude names docs may reference: builtins plus the always-available
# conversions. These exist in every program with no import.
_PRELUDE_DOC_NAMES = _BUILTINS | _CONVERSIONS

# Per-module top-level names of bundled-but-unloaded std modules, parsed from
# disk on first use (docs may `see Std.X.y` without importing Std.X).
_std_symbol_cache: dict[str, set[str] | None] = {}


def _std_module_symbols(qual: str) -> set[str] | None:
    """Top-level symbol names of the bundled std module `qual` (e.g. "Std.Str"),
    or None when `qual` names no bundled module."""
    if qual not in _std_symbol_cache:
        names: set[str] | None = None
        if Checker._is_std_module(qual):
            from flx.modules import std_root
            from flx.syntax.parser import parse as _parse

            path = std_root().joinpath(*qual.split(".")).with_suffix(".flx")
            module = _parse(path.read_text(encoding="utf-8"), str(path))
            names = set()
            decl_kinds = (
                ast.FnDecl,
                ast.ExternFnDecl,
                ast.RecordDecl,
                ast.AdtDecl,
                ast.TraitDecl,
                ast.MacroDecl,
            )
            for item in module.items:
                if isinstance(item, decl_kinds):
                    names.add(item.name)
                if isinstance(item, ast.AdtDecl):
                    names.update(v.name for v in item.variants)
        _std_symbol_cache[qual] = names
    return _std_symbol_cache[qual]


# Capability modules whose calls (e.g. Log.info) are effectful intrinsics.
_EFFECT_MODULES = {"Fs", "Http", "Db", "Log", "Time", "Alloc", "Random", "Process", "Unsafe"}

# Things users from other languages reach for that don't exist yet. Erroring
# as if they were typos cost real study time; say "not yet" and name the
# workaround instead.
_KNOWN_ABSENT: dict[str, str] = {
    "break": "loops have no break; use a flag in the `while` condition, or an early `return`",
    "continue": "loops have no continue; guard the rest of the body with an `if`",
    "format": "there are no format strings yet; build output with `++`, to_str, and "
    "Std.Str's to_str_fixed/pad_left/pad_right",
    "printf": "there is no printf; use println (import Std.IO) with `++`, to_str, "
    "and Std.Str's to_str_fixed/pad_left/pad_right",
}

# Features that EXIST under another spelling. These must not say "Flex does
# not have X yet" — that headline would deny a shipped feature.
_SPELLED_DIFFERENTLY: dict[str, str] = {
    "chr": 'Flex calls it from_byte: import Std.Str, then from_byte(65) is "A" '
    "(from_bytes builds a string from a whole List<I64>)",
    "to_float": "Flex calls it parse_float (import Std.Str) for strings, and to_f64 for I64 values",
    "sorted": "Flex sorts in place: sort(xs) / sort_by(xs, key) / sort_with(xs, lt) "
    "(import Std.List)",
    "dict": "Flex calls it Map: let m: Map<String, I64> = Map.new(), then "
    "Map.set(m, k, v) and Map.get(m, k) — no import needed",
    "hashmap": "Flex calls it Map (see `dict`)",
    "HashMap": "Flex calls it Map (see `dict`)",
    "pop": "pop is a built-in list operation: List.pop(xs) returns Some(last) or None when empty",
    "input": "use read_line() (import Std.IO, uses { Fs })",
}

# Stdlib names users reach for without the import. A plain "unknown name" on
# `split` or `from_byte` reads as "Flex can't do this"; name the module instead.
_NEEDS_IMPORT: dict[str, str] = (
    dict.fromkeys(("print", "println", "read_line"), "Std.IO")
    | dict.fromkeys(
        (
            "length",
            "is_empty",
            "trim",
            "split",
            "parse_int",
            "parse_float",
            "byte_at",
            "substr",
            "char_at",
            "from_byte",
            "from_bytes",
            "to_bytes",
            "to_hex",
            "to_unsigned",
            "to_str_fixed",
            "is_ascii_alpha",
            "is_ascii_digit",
            "is_ascii_alnum",
            "lower_ascii",
            "repeat",
            "pad_left",
            "pad_right",
        ),
        "Std.Str",
    )
    | dict.fromkeys(("map", "filter", "fold", "range", "sort", "sort_by", "sort_with"), "Std.List")
    | dict.fromkeys(("read_text", "write_text", "append_text"), "Std.Fs")
    | dict.fromkeys(("all", "count", "at", "has_flag", "value_after"), "Std.Arg")
    | dict.fromkeys(("sqrt", "abs", "min", "max", "floor", "ceil"), "Std.Math")
    | dict.fromkeys(("parse_csv_line",), "Std.Csv")
)

# Build-only intrinsics under `flx.` (available when the module declares targets).
_FLX_BUILD_OPS = {"check", "test", "run", "expand", "build"}

_ARITH = {"+", "-", "*", "/", "%"}
_BITWISE = {"&", "|", "^", "<<", ">>"}
_COMPARE = {"<", "<=", ">", ">="}
_EQUALITY = {"==", "!="}
_BOOLEAN = {"&&", "||"}

_I64_MAX = 2**63 - 1
_NO_SPAN = Span("<builtin>", Pos(0, 0, 0), Pos(0, 0, 0))


class _InstantiateRet:
    """An _INTRINSICS return type that needs a live checker to build: generic
    builtin ADTs (Option<String>) must be settled instantiations — with their
    variants resolved through the checker's cache — for match to work."""

    def __init__(self, adt: str, args: tuple[Type, ...]) -> None:
        self.adt = adt
        self.args = args


_OPTION_OF_STRING = _InstantiateRet("Option", (STRING,))
_RESULT_STRING_STRING = _InstantiateRet("Result", (STRING, STRING))
_RESULT_UNIT_STRING = _InstantiateRet("Result", (UNIT, STRING))

# (module, method) -> (effect, param types, return type).
_INTRINSICS: dict[tuple[str, str], tuple[str, tuple[Type, ...], Type | _InstantiateRet]] = {
    ("Log", "info"): ("Log", (STRING,), UNIT),
    ("Log", "warn"): ("Log", (STRING,), UNIT),
    ("Log", "error"): ("Log", (STRING,), UNIT),
    ("Log", "print"): ("Log", (STRING,), UNIT),  # no trailing newline
    # One stdin line, trailing newline stripped: Some(line) — Some("") for a
    # blank line — and None at end of input, distinguishably.
    ("Fs", "read_line"): ("Fs", (), _OPTION_OF_STRING),
    ("Fs", "read_text"): ("Fs", (STRING,), _RESULT_STRING_STRING),
    ("Fs", "write_text"): ("Fs", (STRING, STRING), _RESULT_UNIT_STRING),
    ("Fs", "append_text"): ("Fs", (STRING, STRING), _RESULT_UNIT_STRING),
    ("Time", "monotonic_ms"): ("Time", (), I64),
    # Strings are byte strings: byte_at/substr index BYTES (UTF-8 sequences can
    # split; surrogateescape keeps the bytes lossless). Pure ("" = no effect).
    ("Str", "byte_at"): ("", (STRING, I64), I64),  # panics out of bounds
    ("Str", "substr"): ("", (STRING, I64, I64), STRING),  # clamps to the string
    ("Str", "from_byte"): ("", (I64,), STRING),  # panics outside 1..255
    ("Str", "from_bytes"): ("", (ListType(I64),), STRING),  # panics per element
    ("Str", "to_hex"): ("", (I64,), STRING),  # unsigned lowercase hexadecimal
    ("Str", "to_unsigned"): ("", (I64,), STRING),  # unsigned decimal
    ("Bytes", "len"): ("", (BYTES,), I64),
    ("Bytes", "at"): ("", (BYTES, I64), U8),  # panics out of bounds
    ("Bytes", "to_hex"): ("", (BYTES,), STRING),
    # C strtod of the longest valid prefix (0.0 if none) — the SAME libc call
    # on both backends, so the bits match by construction. Std.Str.parse_float
    # validates the strict whole-string grammar before calling this.
    ("Str", "parse_f64"): ("", (STRING,), F64),
    ("Str", "to_str_fixed"): ("", (F64, I64), STRING),  # %.*f; decimals 0..100 or panic
    ("Env", "argv"): ("Process", (), ListType(STRING)),  # user args, no argv[0]
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
    qualified_calls: dict[int, str] = field(default_factory=dict)
    # id(assert_eq/assert_ne CallExpr) -> the Eq impl symbol carrying the
    # comparison. A SEPARATE channel from method_targets: the generic
    # method-dispatch interceptors fire before builtin handling on both
    # backends and would swallow the assertion into a plain (discarded) call.
    assert_impls: dict[int, str] = field(default_factory=dict)
    # C-ABI foreign functions: called by their unmangled symbol name; the native
    # backend declares them, the interpreter dispatches them through ctypes.
    extern_fns: set[str] = field(default_factory=set)
    # Per-extern C-level ABI: (param kinds, return kind), each kind one of
    # "i64" | "i32" | "str" | "unit". I32 exists ONLY at the boundary (C `int`,
    # sign-extended to I64 on the Flex side) — reading a 32-bit return as 64
    # bits is garbage, so the width must be declared, not guessed.
    extern_abi: dict[str, tuple[tuple[str, ...], str]] = field(default_factory=dict)
    # generic function templates (name -> decl) and the instantiations demanded
    # by call sites. The monomorphizer turns these into concrete functions.
    generic_fns: dict[str, ast.FnDecl] = field(default_factory=dict)
    instantiations: set[tuple[str, tuple[str, ...]]] = field(default_factory=set)
    inst_subst: dict[tuple[str, tuple[str, ...]], dict[str, Type]] = field(default_factory=dict)
    file_module: dict[str, str] = field(default_factory=dict)  # file path -> module name
    module_spans: list[tuple[str, Span]] = field(default_factory=list)


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
    def __init__(
        self,
        module: ast.Module,
        decl_module: dict[str, str] | None = None,
        public: set[str] | None = None,
        file_module: dict[str, str] | None = None,
        module_spans: list[tuple[str, Span]] | None = None,
        builtin_records: dict[str, RecordType] | None = None,
    ) -> None:
        # Pre-registered record types (e.g. Manifest/Dependency when checking a
        # package.flx). Kept out of ordinary programs so user record literals
        # can never accidentally resolve to them.
        self._builtin_records = builtin_records or {}
        self.module = module
        # Visibility: which module declared each top-level name, which are `pub`,
        # and which module each FILE belongs to. Empty for a single-file program
        # (then everything is mutually visible).
        self.decl_module = decl_module or {}
        self.public = public or set()
        self.file_module = file_module or {}
        self.module_spans = module_spans or []
        self.current_module: str | None = None
        self.diags: list[Diagnostic] = []
        self.expr_types: dict[int, Type] = {}
        self.functions: dict[str, FnType] = {}
        self.scope = _Scope()
        self.return_type: Type = UNIT
        self.in_test = False
        self.in_target = False
        self.fn_effects: dict[str, set[str]] = {}
        self.declared_effects: set[str] = set()
        self.record_types: dict[str, RecordType] = {}
        # ADT templates: name -> (type params, [(variant name, payload TypeExprs)]).
        self.adt_templates: dict[str, tuple[list[str], list[tuple[str, list[ast.TypeExpr]]]]] = {}
        # One AdtType object per (name, type_args): entries are cached BEFORE
        # their variants resolve, so recursive payloads tie back to the same
        # object instead of recursing forever.
        self._adt_cache: dict[tuple[str, tuple[Type, ...]], AdtType] = {}
        self._inst_depth = 0  # guards polymorphic recursion (TYPE023)
        self.ctors: dict[str, tuple[str, int]] = {}  # variant name -> (adt name, index)
        self._subst: dict[str, Type] = {}  # active type-parameter substitution
        # traits / impls
        self.traits: dict[str, dict[str, ast.TraitMethod]] = {}
        self.method_index: dict[str, set[str]] = {}  # method -> declaring traits
        self.impls: dict[tuple[str, str], dict[str, str]] = {}  # (trait,key) -> method->symbol
        self._impl_spans: dict[tuple[str, str], Span] = {}  # for IMPL006 "first at" notes
        self.method_targets: dict[int, str] = {}  # id(CallExpr) -> impl symbol
        self.qualified_calls: dict[int, str] = {}
        self._impl_fns: list[ast.FnDecl] = []  # renamed impl methods, emitted as functions
        self._self_type: Type | None = None
        # bounded generics: templates kept out of `functions`; instantiations
        # demanded by call sites and the substitution that produced each one.
        self.generic_fns: dict[str, ast.FnDecl] = {}
        self.instantiations: set[tuple[str, tuple[str, ...]]] = set()
        self.inst_subst: dict[tuple[str, tuple[str, ...]], dict[str, Type]] = {}
        self.extern_fns: set[str] = set()
        self.extern_abi: dict[str, tuple[tuple[str, ...], str]] = {}
        # First declaration of each extern: (span, pub), for redeclaration
        # agreement checks and "first declared at ..." notes.
        self._extern_first: dict[str, tuple[Span | None, bool]] = {}
        # Which modules declared each extern: every declaring module may use it
        # (a private extern redeclared in two modules is usable from both).
        self.extern_decl_modules: dict[str, set[str]] = {}
        # Duplicate fn declarations (by id): TYPE002 reported, bodies skipped.
        self._dup_fn_decls: set[int] = set()
        # assert_eq/assert_ne calls routed through an Eq impl (see CheckResult).
        self.assert_impls: dict[int, str] = {}

    # --- entry ----------------------------------------------------------------

    def check(self) -> CheckResult:
        # Builtins first, so user declarations collide INTO them (never silently
        # replace them — `?` and Ok/Err/Some/None assume the prelude shapes).
        for name, template in _BUILTIN_ADTS.items():
            self.adt_templates[name] = template
            for i, (vname, _) in enumerate(template[1]):
                self.ctors[vname] = (name, i)
        for adt in self.module.adts:
            self._check_type_name(adt.name, adt.span)
            if adt.name in _BUILTIN_ADTS:
                continue  # reported by _check_type_name; keep the builtin shape
            if adt.name in self.adt_templates:
                self._err_duplicate("type", adt.name, adt.span)
            # Constructors share ONE program-wide namespace (an unqualified
            # `X(...)` must mean exactly one thing), so collisions — within an
            # ADT, across ADTs, or with the prelude — are errors, not last-wins.
            seen_variants: set[str] = set()
            for i, variant in enumerate(adt.variants):
                if variant.name in seen_variants:
                    self._err(
                        "TYPE002",
                        f"variant {variant.name!r} is declared twice in type {adt.name!r}",
                        variant.span,
                    )
                    continue
                seen_variants.add(variant.name)
                prior = self.ctors.get(variant.name)
                if prior is not None:
                    self._err(
                        "TYPE002",
                        f"constructor {variant.name!r} is already defined by type {prior[0]!r}",
                        variant.span,
                        help="constructor names share one namespace; rename the variant",
                    )
                    continue
                self.ctors[variant.name] = (adt.name, i)
            self.adt_templates[adt.name] = (
                adt.type_params,
                [(v.name, v.payload) for v in adt.variants],
            )

        self.record_types.update(self._builtin_records)
        # Records register in two passes so field types may name records
        # declared later (or recurse through an ADT): names first, fields after.
        user_records: list[ast.RecordDecl] = []
        for record in self.module.records:
            self._check_type_name(record.name, record.span)
            if record.name in self.record_types or record.name in self.adt_templates:
                self._err_duplicate("type", record.name, record.span)
                continue
            self.record_types[record.name] = RecordType(record.name, ())
            user_records.append(record)
        for record in user_records:
            fields = tuple((f.name, self._resolve_type(f.type)) for f in record.fields)
            for fname, fty in fields:
                if isinstance(fty, FnType):
                    self._err(
                        "TYPE025",
                        f"field {fname!r} has a function type; function values "
                        "cannot be stored in records yet",
                        record.span,
                    )
            object.__setattr__(self.record_types[record.name], "fields", fields)
        # A record that reaches itself through record fields alone is infinite
        # in size — only an ADT payload (which boxes) can break the cycle.
        color: dict[str, int] = {}  # 1 = in progress, 2 = done

        def _on_record_cycle(rt: RecordType) -> bool:
            state = color.get(rt.name, 0)
            if state == 1:
                return True  # back-edge: this record is on the current path
            if state == 2:
                return False
            color[rt.name] = 1
            hit = any(isinstance(fty, RecordType) and _on_record_cycle(fty) for _, fty in rt.fields)
            color[rt.name] = 2
            return hit

        for record in user_records:
            if _on_record_cycle(self.record_types[record.name]):
                self._err(
                    "TYPE024",
                    f"record {record.name!r} contains itself (directly or through "
                    "other records), so it would have infinite size",
                    record.span,
                    help="wrap the recursive field in an ADT "
                    "(e.g. `type Link = | End | More(Node)`) to give it a boxed "
                    "representation",
                )
                break  # one report covers the cycle

        # Every type expression in an ADT payload or trait signature is validated
        # NOW, not at first use — an uninstantiated `type T = | A(Bogus)` is still
        # a type error.
        for adt in self.module.adts:
            for variant in adt.variants:
                for payload in variant.payload:
                    self._validate_type_expr(payload, set(adt.type_params), allow_self=False)
        for trait in self.module.traits:
            for sig in trait.methods:
                for param in sig.params:
                    self._validate_type_expr(param.type, set(), allow_self=True)
                if sig.return_type is not None:
                    self._validate_type_expr(sig.return_type, set(), allow_self=True)

        self._register_traits()

        for fn in self.module.functions:
            if fn.name in self.functions or fn.name in self.generic_fns:
                # TYPE002 reported; the FIRST signature stays authoritative and
                # this body is never checked against it — a duplicate's body is
                # consistent with its OWN declaration, and false "wrong return
                # type"/"missing effect" errors would only obscure the collision.
                self._err_duplicate("function", fn.name, fn.span)
                self._dup_fn_decls.add(id(fn))
                continue
            self.current_module = self._module_of(fn.span)  # for signature visibility
            if fn.type_params:
                # A template: type params are unresolved here. Its concrete
                # instantiations are checked (and registered) by the monomorphizer.
                self._register_generic_fn(fn)
                continue
            params = tuple(self._resolve_type(p.type) for p in fn.params)
            ret = self._resolve_type(fn.return_type) if fn.return_type else UNIT
            if isinstance(ret, FnType):
                self._err(
                    "TYPE025",
                    f"{fn.name!r} returns a function type; functions cannot "
                    "return function values yet",
                    fn.span,
                    help="take the would-be result's arguments directly instead",
                )
                ret = ERROR
            self.functions[fn.name] = FnType(params, ret)
            self.fn_effects[fn.name] = set(fn.effects)

        self._register_externs()
        self.current_module = None
        self._register_impls()
        self._register_targets()

        for fn in self.module.functions:
            if fn.name in self.generic_fns or id(fn) in self._dup_fn_decls:
                continue
            self._check_fn(fn)
        for impl_fn in self._impl_fns:
            self._check_fn(impl_fn)
        for test in self.module.tests:
            self._check_test(test)
        for target in self.module.targets:
            self._check_target(target)
        for doc in self.module.docs:
            self._check_doc(doc)

        if self.diags:
            raise FlexError(self.diags)
        # Emit impl methods as ordinary (mangled) functions for the backend, and
        # drop generic templates (the monomorphizer emits concrete copies instead)
        # plus macro declarations (expanded away; kept until here for doc targets).
        kept = [
            it
            for it in self.module.items
            if not (isinstance(it, ast.FnDecl) and it.type_params)
            and not isinstance(it, ast.MacroDecl)
        ]
        module = replace(self.module, items=[*kept, *self._impl_fns])
        return CheckResult(
            module,
            self.expr_types,
            self.functions,
            set(self.ctors),
            self.method_targets,
            qualified_calls=self.qualified_calls,
            assert_impls=self.assert_impls,
            extern_fns=self.extern_fns,
            extern_abi=self.extern_abi,
            generic_fns=self.generic_fns,
            instantiations=self.instantiations,
            inst_subst=self.inst_subst,
            file_module=self.file_module,
            module_spans=self.module_spans,
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
            # Impl signatures are visibility-checked against the impl's own module.
            self.current_module = self._module_of(impl.span)
            if impl.trait not in self.traits:
                self._err("IMPL001", f"unknown trait {impl.trait!r}", impl.span)
                continue
            impl_ty = self._resolve_type(ast.TypeExpr(impl.type_name, [], impl.span))
            if impl_ty is ERROR:
                continue
            key = _type_key(impl_ty)
            if (impl.trait, key) in self.impls:
                first = self._impl_spans.get((impl.trait, key))
                where = f" (first at {first.file}:{first.start.line})" if first else ""
                self._err(
                    "IMPL006",
                    f"conflicting impl {impl.trait} for {impl.type_name}{where}",
                    impl.span,
                )
            table: dict[str, str] = {}
            self.impls[(impl.trait, key)] = table
            self._impl_spans[(impl.trait, key)] = impl.span
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
        self.current_module = None

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
        # Solve the substitution by unifying each declared parameter type with
        # the argument's type: bare `T` binds directly, and `Chain<T>` against
        # Chain<I64> binds T = I64 (same unifier as constructor inference).
        subst: dict[str, Type] = {}
        for param, at in zip(template.params, arg_types, strict=True):
            if at is not ERROR:
                self._unify_typeexpr(param.type, at, tp_names, subst)
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

    def _module_of(self, span: Span | None) -> str | None:
        """The module enclosing a definition, derived from its span's FILE. Spans
        survive monomorphization cloning, impl-method renaming, and derive
        expansion, so synthetic items inherit their source module — generic and
        impl bodies cannot escape visibility checks by being re-checked under a
        mangled name."""
        if span is None:
            return None
        for name, module_span in self.module_spans:
            if (
                module_span.file == span.file
                and module_span.start.offset <= span.start.offset
                and span.end.offset <= module_span.end.offset
            ):
                return name
        return self.file_module.get(span.file)

    def _loaded_modules(self) -> set[str]:
        loaded = set(self.decl_module.values())
        loaded.update(name for name, _ in self.module_spans)
        if self.module.name:
            loaded.add(self.module.name)
        return loaded

    def _member_path(self, expr: ast.Expr) -> list[str] | None:
        if isinstance(expr, ast.NameExpr):
            return [expr.name]
        if isinstance(expr, ast.MemberExpr):
            prefix = self._member_path(expr.obj)
            if prefix is None:
                return None
            return [*prefix, expr.name]
        return None

    def _name_visible(self, name: str) -> bool:
        if self.current_module is None or name in self.public:
            return True
        if self.current_module in self.extern_decl_modules.get(name, ()):
            return True  # this module declared the extern itself
        owner = self.decl_module.get(name)
        return owner is None or owner == self.current_module

    def _check_visible(self, name: str, span: Span | None) -> None:
        """Flag a reference from `current_module` to another module's private name.
        No-op for single-file programs, builtins, public names, and own-module
        references. Span-less references are SYNTHETIC (monomorphization
        substituting a caller's type argument into a template's module) — the
        caller already passed visibility at the call site, so they're exempt."""
        if span is None:
            return
        if name in _BUILTIN_ADTS:
            # Option/Result are global. A user redefinition is TYPE002-rejected;
            # it must not also make stdlib signatures "private to" the user.
            return
        if not self._name_visible(name):
            self._err(
                "VIS001",
                f"{name!r} is private to module {self.decl_module.get(name)!r}",
                span,
            )

    def _check_fn(self, fn: ast.FnDecl) -> None:
        self.scope = _Scope()
        self.in_test = False
        self.current_module = self._module_of(fn.span)
        self.declared_effects = set(fn.effects)
        fn_ty = self.functions[fn.name]
        if len(fn_ty.params) != len(fn.params):
            # A duplicate definition kept the first signature; TYPE002 was
            # already reported, so don't check this body against the wrong type.
            return
        seen: set[str] = set()
        for param, ptype in zip(fn.params, fn_ty.params, strict=True):
            if param.name in seen:
                self._err("NAME002", f"duplicate parameter name {param.name!r}", param.span)
            seen.add(param.name)
            self.scope.define(param.name, _Binding(ptype, mutable=False))
        self.return_type = fn_ty.ret
        # A Unit function's tail value is discarded, so a statement-position
        # if/match there needs no agreeing branch types.
        body_ty = self._check_block(fn.body, fn_ty.ret, fn_ty.ret is not UNIT)
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

    def _check_doc(self, doc: ast.DocDecl) -> None:
        """Static doc validation, run on EVERY compile: a doc that references a
        missing symbol (DOC001) or claims a status reality contradicts (DOC004)
        fails the build. Example execution belongs to `flx docs check`."""
        refs = list(doc.sees)
        if doc.target is not None and doc.target != "module":
            refs.append(doc.target)
        for ref in refs:
            if not self._doc_ref_known(ref):
                self._err(
                    "DOC001",
                    f"doc references {ref!r}, which does not exist",
                    doc.span,
                    help="docs are checked declarations; fix the reference or remove the doc",
                )
        if (
            doc.status == "not_yet"
            and doc.target is not None
            and doc.target != "module"
            and self._doc_symbol_exists(doc.target.rsplit(".", 1)[-1])
        ):
            self._err(
                "DOC004",
                f"doc for {doc.target!r} says status not_yet, but the symbol exists",
                doc.span,
                help="update the status to implemented (or partial)",
            )

    def _doc_symbol_exists(self, name: str) -> bool:
        """Whether a bare name is a declared symbol of any documentable kind."""
        return (
            name in self.functions
            or name in self.generic_fns
            or name in self.extern_fns
            or name in self.record_types
            or name in self.adt_templates
            or name in self.traits
            or name in self.ctors
            or name in _PRELUDE_DOC_NAMES
            or any(m.name == name for m in self.module.macros)
        )

    def _doc_ref_known(self, ref: str) -> bool:
        """Resolve a doc target or `see` reference. Dotted references validate
        the QUALIFIER too: `Bogus.helper` must not pass just because some
        `helper` exists somewhere — stale module prefixes are exactly what
        DOC001 is for."""
        loaded = set(self.decl_module.values())
        if self.module.name:
            loaded.add(self.module.name)
        if "." not in ref:
            return self._doc_symbol_exists(ref) or ref in loaded
        if ref in loaded or self._is_std_module(ref):
            return True  # a module reference
        qual, leaf = ref.rsplit(".", 1)
        if qual in loaded:
            # A loaded module's symbol: the merged tables know its declarer.
            if self.decl_module.get(leaf) == qual:
                return True
            return qual == self.module.name and self._doc_symbol_exists(leaf)
        std_symbols = _std_module_symbols(qual)
        if std_symbols is not None:
            return leaf in std_symbols
        return False

    @staticmethod
    def _is_std_module(ref: str) -> bool:
        """Whether `ref` names a bundled stdlib module (Std.Str, ...). Docs may
        `see` sibling modules that the current program never imports; the
        bundle on disk is the truth for those."""
        from flx.modules import std_root

        parts = ref.split(".")
        if len(parts) < 2 or parts[0] != "Std":
            return False
        candidate = std_root().joinpath(*parts).with_suffix(".flx")
        return candidate.is_file()

    def _check_test(self, test: ast.TestDecl) -> None:
        self.scope = _Scope()
        self.in_test = True
        self.current_module = self._module_of(test.span)
        self.declared_effects = set(test.effects)
        self.return_type = UNIT
        self._check_block(test.body, None, False)

    def _register_externs(self) -> None:
        for ext in self.module.externs:
            self.current_module = self._module_of(ext.span)
            self._check_extern_name(ext)
            # The C ABI surface is deliberately small: I64 (long long), I32
            # (C `int`, sign-extended to I64 on the Flex side), and String
            # (NUL-terminated char*) in; those plus Unit out. Everything else is
            # rejected rather than mis-marshalled. I32 exists only here — a C
            # function returning `int` MUST be declared I32, or its high 32 bits
            # are garbage.
            params: list[Type] = []
            param_kinds: list[str] = []
            for param in ext.params:
                if param.type.name == "I32" and not param.type.args:
                    params.append(I64)
                    param_kinds.append("i32")
                    continue
                pty = self._resolve_type(param.type)
                if pty is I64:
                    param_kinds.append("i64")
                elif pty is F64:
                    param_kinds.append("f64")
                elif pty is STRING:
                    param_kinds.append("str")
                else:
                    if pty is not ERROR:
                        self._err(
                            "FFI002",
                            f"extern parameter {param.name!r} has type {pty}; "
                            "only I64, I32, F64, and String cross the C ABI",
                            param.span,
                        )
                    pty = ERROR
                    param_kinds.append("i64")
                params.append(pty)
            if (
                ext.return_type is not None
                and ext.return_type.name == "I32"
                and (not ext.return_type.args)
            ):
                ret: Type = I64
                ret_kind = "i32"
            else:
                ret = self._resolve_type(ext.return_type) if ext.return_type else UNIT
                if ret is I64:
                    ret_kind = "i64"
                elif ret is F64:
                    ret_kind = "f64"
                elif ret is STRING:
                    ret_kind = "str"
                elif ret is UNIT:
                    ret_kind = "unit"
                else:
                    if ret is not ERROR:
                        self._err(
                            "FFI002",
                            f"extern return type {ret} is not supported; "
                            "only I64, I32, F64, String, and Unit cross the C ABI",
                            ext.span,
                        )
                    ret = ERROR
                    ret_kind = "i64"
            fn_ty = FnType(tuple(params), ret)
            abi = (tuple(param_kinds), ret_kind)
            if ext.name in self.extern_fns:
                # C-style redeclaration: declaring the same symbol again is fine
                # iff the signature, ABI widths, asserted effects, AND visibility
                # agree exactly — two modules may each privately declare `strlen`,
                # but a `pub` redeclaration must not unlock someone else's
                # private extern program-wide.
                first = self._extern_first.get(ext.name)
                if (
                    self.functions.get(ext.name) != fn_ty
                    or self.extern_abi.get(ext.name) != abi
                    or self.fn_effects.get(ext.name) != set(ext.effects)
                    or (first is not None and first[1] != ext.pub)
                ):
                    where = ""
                    if first is not None and first[0] is not None:
                        where = f"; first declared at {first[0].file}:{first[0].start.line}"
                    self._err(
                        "FFI004",
                        f"conflicting declarations for extern {ext.name!r}",
                        ext.span,
                        help="every declaration of a C symbol must agree on "
                        f"signature, effects, and pub{where}",
                    )
                if self.current_module is not None:
                    self.extern_decl_modules.setdefault(ext.name, set()).add(self.current_module)
                continue
            if ext.name in self.functions or ext.name in self.generic_fns:
                self._err_duplicate("function", ext.name, ext.span)
            self.functions[ext.name] = fn_ty
            self.fn_effects[ext.name] = set(ext.effects)
            self.extern_fns.add(ext.name)
            self.extern_abi[ext.name] = abi
            self._extern_first[ext.name] = (ext.span, ext.pub)
            if self.current_module is not None:
                self.extern_decl_modules.setdefault(ext.name, set()).add(self.current_module)

    def _check_type_name(self, name: str, span: Span | None) -> None:
        if name in PRIMITIVES or name in ("List", "Map") or name in _BUILTIN_ADTS:
            self._err(
                "TYPE002",
                f"cannot redefine the builtin type {name!r}",
                span,
                help="primitives, List, Map, Result, and Option are part of the "
                "prelude; pick another name",
            )

    def _validate_type_expr(self, te: ast.TypeExpr, params: set[str], *, allow_self: bool) -> None:
        """Eagerly check that every name in a type expression exists (type
        parameters, primitives, List, records, ADTs, and optionally Self)."""
        name = te.name
        known = (
            name in params
            or name in PRIMITIVES
            or name in ("List", "Map")
            or name == "->"
            or name in self.record_types
            or name in self.adt_templates
            or (allow_self and name == "Self")
        )
        if not known:
            self._err("TYPE001", f"unknown type {name!r}", te.span)
            return
        arity: int | None = None
        if name == "->":
            arity = None  # any param count; args are [params..., ret]
        elif name == "List":
            arity = 1
        elif name == "Map":
            arity = 2
        elif name in self.adt_templates:
            arity = len(self.adt_templates[name][0])
        elif name in PRIMITIVES or name in self.record_types or name in params:
            arity = 0
        if arity is not None and len(te.args) != arity:
            self._err(
                "TYPE013",
                f"type {name!r} expects {arity} type argument(s), got {len(te.args)}",
                te.span,
            )
        for arg in te.args:
            self._validate_type_expr(arg, params, allow_self=allow_self)

    def _check_extern_name(self, ext: ast.ExternFnDecl) -> None:
        """An extern's name IS the C symbol it links: it must be a plain C
        identifier and must not collide with the Flex runtime, the prelude, or
        a constructor — those would silently rebind or miscompile."""
        name = ext.name
        if not name.isascii() or not all(c.isalnum() or c == "_" for c in name):
            self._err("FFI003", f"{name!r} is not a valid C symbol name", ext.span)
        elif name.startswith(("flx_", "__")) or name == "main":
            self._err(
                "FFI003",
                f"extern name {name!r} collides with the Flex runtime namespace",
                ext.span,
            )
        elif name in _BUILTINS or name in _CONVERSIONS or name in ("sh", "flx"):
            self._err("FFI003", f"extern name {name!r} collides with a Flex builtin", ext.span)
        elif name in self.ctors:
            self._err("FFI003", f"extern name {name!r} collides with a constructor", ext.span)

    def _target_result(self, span: Span | None) -> Type:
        """The value a target (or build intrinsic) call yields: Result<Unit, String>."""
        return self._instantiate("Result", [UNIT, STRING], span)

    def _register_targets(self) -> None:
        for target in self.module.targets:
            if target.name in ("sh", "flx", "default"):
                self._err(
                    "BUILD006",
                    f"{target.name!r} is reserved and cannot be a target name",
                    target.span,
                )
                continue
            if target.name in self.functions or target.name in self.generic_fns:
                self._err_duplicate("target", target.name, target.span)
            # A target is callable from other targets as `name()?`; calling it
            # demands its declared effects, so effects propagate up the build
            # graph exactly like ordinary calls.
            self.functions[target.name] = FnType((), self._target_result(target.span))
            self.fn_effects[target.name] = set(target.effects)
        defaults = [it for it in self.module.items if isinstance(it, ast.DefaultTargetDecl)]
        if len(defaults) > 1:
            self._err("BUILD007", "`target default` is declared more than once", defaults[1].span)
        default = self.module.default_target
        if default is not None and default not in {t.name for t in self.module.targets}:
            self._err("BUILD002", f"default target {default!r} is not a target", self.module.span)

    def _check_target(self, target: ast.TargetDecl) -> None:
        # Targets check like tests: `?` propagates failure out of the body, and
        # every effectful call must be covered by the target's `uses { ... }`.
        self.scope = _Scope()
        self.in_test = True
        self.in_target = True
        self.current_module = self._module_of(target.span)
        self.declared_effects = set(target.effects)
        self.return_type = UNIT
        self._check_block(target.body)
        self.in_test = False
        self.in_target = False

    # --- statements / blocks --------------------------------------------------

    def _check_block(
        self, block: ast.Block, expected: Type | None = None, value_used: bool = True
    ) -> Type:
        self.scope.push()
        result: Type = UNIT
        last = block.stmts[-1] if block.stmts else None
        for stmt in block.stmts:
            is_last = stmt is last
            # Only the tail statement's value can be consumed; whether it IS
            # consumed is the block's own value_used.
            result = self._check_stmt(
                stmt, expected if is_last else None, value_used if is_last else False
            )
        # The block's value is its trailing expression, else Unit.
        value = result if block.tail is not None else UNIT
        self.scope.pop()
        return value

    def _check_stmt(
        self, stmt: ast.Stmt, expected: Type | None = None, value_used: bool = True
    ) -> Type:
        if isinstance(stmt, (ast.LetStmt, ast.MutStmt)):
            annotated: Type | None = None
            if stmt.annotation is not None:
                annotated = self._resolve_type(stmt.annotation)
            value_ty = self._check_expr(stmt.value, annotated)
            if annotated is not None:
                self._expect(annotated, value_ty, stmt.value.span, "bound value")
                value_ty = annotated  # the annotation wins (e.g. `[]` stays typed)
            if isinstance(stmt, ast.MutStmt) and isinstance(value_ty, FnType):
                self._err(
                    "TYPE025",
                    "function values cannot be stored in `mut` bindings yet",
                    stmt.value.span,
                    help="bind with `let`, or pass the function as an argument",
                )
            self.scope.define(stmt.name, _Binding(value_ty, mutable=isinstance(stmt, ast.MutStmt)))
            return UNIT
        if isinstance(stmt, ast.AssignStmt):
            self._check_assign(stmt)
            return UNIT
        if isinstance(stmt, ast.IndexAssignStmt):
            self._check_index_assign(stmt)
            return UNIT
        if isinstance(stmt, ast.WhileStmt):
            self._expect(BOOL, self._check_expr(stmt.cond), stmt.cond.span, "while condition")
            self._check_block(stmt.body, None, False)
            return UNIT
        if isinstance(stmt, ast.ForStmt):
            iter_ty = self._check_expr(stmt.iter)
            elem: Type = ERROR
            if isinstance(iter_ty, ListType):
                elem = iter_ty.elem
            elif iter_ty is not ERROR:
                self._err("TYPE021", f"`for` iterates a List, found {iter_ty}", stmt.iter.span)
            self.scope.push()
            self.scope.define(stmt.name, _Binding(elem, mutable=False))
            self._check_block(stmt.body, None, False)
            self.scope.pop()
            return UNIT
        if isinstance(stmt, ast.ReturnStmt):
            actual = (
                self._check_expr(stmt.value, self.return_type) if stmt.value is not None else UNIT
            )
            span = stmt.value.span if stmt.value is not None else stmt.span
            self._expect(self.return_type, actual, span, "return value")
            return UNIT
        if isinstance(stmt, ast.ExprStmt):
            return self._check_expr(stmt.expr, expected, value_used)
        return UNIT

    def _check_assign(self, stmt: ast.AssignStmt) -> None:
        binding = self.scope.lookup(stmt.name)
        # The declared type is inference context for the RHS, so `xs = []` and
        # `r = Err("boom")` work on a typed `mut` binding.
        value_ty = self._check_expr(stmt.value, binding.type if binding else None)
        if binding is None:
            self._err(
                "NAME001",
                f"cannot assign to undefined binding {stmt.name!r}",
                stmt.span,
                help="a lone `{ x = e }` is a block with an assignment; for a "
                "one-field record literal, parenthesize it: ({ x = e })",
            )
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

    def _check_index_assign(self, stmt: ast.IndexAssignStmt) -> None:
        obj = self._check_expr(stmt.obj)
        index = self._check_expr(stmt.index)
        self._expect(I64, index, stmt.index.span, "index")
        if not isinstance(obj, ListType):
            if obj is not ERROR:
                self._err(
                    "TYPE017",
                    f"indexed assignment requires a List, found {obj}",
                    stmt.obj.span,
                    help="use Map.set(m, key, value) for maps",
                )
            self._check_expr(stmt.value)
            return
        value = self._check_expr(stmt.value, obj.elem)
        self._expect(obj.elem, value, stmt.value.span, "assigned element")

    # --- expressions ----------------------------------------------------------

    def _check_expr(
        self, expr: ast.Expr, expected: Type | None = None, value_used: bool = True
    ) -> Type:
        ty = self._infer(expr, expected, value_used)
        self.expr_types[id(expr)] = ty
        return ty

    def _infer(self, expr: ast.Expr, expected: Type | None, value_used: bool = True) -> Type:
        if isinstance(expr, ast.IntLit):
            return self._infer_int_lit(expr, expected)
        if isinstance(expr, ast.FloatLit):
            return F64
        if isinstance(expr, ast.BoolLit):
            return BOOL
        if isinstance(expr, ast.StringLit):
            return STRING
        if isinstance(expr, ast.BytesLit):
            return self._infer_bytes(expr)
        if isinstance(expr, ast.NameExpr):
            return self._infer_name(expr, expected)
        if isinstance(expr, ast.UnaryExpr):
            return self._infer_unary(expr, expected)
        if isinstance(expr, ast.BinaryExpr):
            return self._infer_binary(expr)
        if isinstance(expr, ast.CallExpr):
            return self._infer_call(expr, expected)
        if isinstance(expr, ast.IfExpr):
            return self._infer_if(expr, expected, value_used)
        if isinstance(expr, ast.BlockExpr):
            return self._check_block(expr.body, expected, value_used)
        if isinstance(expr, ast.RegionExpr):
            return self._infer_region(expr)
        if isinstance(expr, ast.RecordExpr):
            return self._infer_record(expr)
        if isinstance(expr, ast.RecordUpdateExpr):
            return self._infer_record_update(expr)
        if isinstance(expr, ast.UnitLit):
            return UNIT
        if isinstance(expr, ast.ListExpr):
            return self._infer_list(expr, expected)
        if isinstance(expr, ast.IndexExpr):
            return self._infer_index(expr)
        if isinstance(expr, ast.MemberExpr):
            return self._infer_member(expr)
        if isinstance(expr, ast.MatchExpr):
            return self._infer_match(expr, expected, value_used)
        if isinstance(expr, ast.TryExpr):
            return self._infer_try(expr)
        return ERROR

    def _infer_int_lit(self, expr: ast.IntLit, expected: Type | None) -> Type:
        target = expected if is_int_type(expected) else I64
        self._check_int_literal(expr.value, target, expr.span)
        return target

    def _check_int_literal(self, value: int, target: Type, span: Span) -> None:
        lo, hi = int_bounds(target)
        if lo <= value <= hi:
            return
        if target is I64 and value > hi:
            message = f"integer literal {value} is out of range for I64 (max {_I64_MAX})"
        else:
            message = f"integer literal {value} is out of range for {target} ({lo}..{hi})"
        self._err("TYPE011", message, span)

    def _infer_bytes(self, expr: ast.BytesLit) -> Type:
        for part in expr.parts:
            if isinstance(part, ast.StringLit):
                self.expr_types[id(part)] = STRING
                continue
            part_ty = self._check_expr(part)
            if not is_int_type(part_ty):
                self._err(
                    "TYPE003",
                    f"byte literal segment has type {part_ty}, expected an integer",
                    part.span,
                )
                continue
            if isinstance(part, ast.IntLit) and not (0 <= part.value <= 255):
                self._err(
                    "TYPE011",
                    f"integer literal {part.value} is out of range for a byte (0..255)",
                    part.span,
                )
        return BYTES

    def _infer_index(self, expr: ast.IndexExpr) -> Type:
        obj = self._check_expr(expr.obj)
        index = self._check_expr(expr.index)
        if isinstance(obj, MapType):
            # m["k"] is the natural dict-syntax mistake; the I64-index error
            # would steer exactly the wrong way (the key type IS String).
            self._err(
                "TYPE017",
                f"cannot index a value of type {obj}",
                expr.span,
                help="use Map.get(m, key) — it returns Option<V>",
            )
            return ERROR
        self._expect(I64, index, expr.index.span, "index")
        if isinstance(obj, ListType):
            return obj.elem
        if obj is not ERROR:
            help_text = "import Std.Str and use char_at/byte_at/substr" if obj is STRING else None
            self._err("TYPE017", f"cannot index a value of type {obj}", expr.span, help=help_text)
        return ERROR

    def _infer_list(self, expr: ast.ListExpr, expected: Type | None) -> Type:
        if expected is ERROR and not expr.items:
            return ERROR  # the annotation was already diagnosed; no cascade
        elem_expected = expected.elem if isinstance(expected, ListType) else None
        if not expr.items:
            if elem_expected is not None:
                return ListType(elem_expected)
            self._err(
                "TYPE023",
                "cannot infer the element type of an empty list here",
                expr.span,
                help="give it context, e.g. a parameter or return type of List<T>",
            )
            return ERROR
        first = self._check_expr(expr.items[0], elem_expected)
        for item in expr.items[1:]:
            self._expect(first, self._check_expr(item, first), item.span, "list element")
        if isinstance(first, FnType):
            self._err(
                "TYPE025",
                "function values cannot be stored in lists yet",
                expr.span,
                help="pass functions as arguments; storing them is the roadmap",
            )
            return ERROR
        return ListType(first) if first is not ERROR else ERROR

    def _infer_record(self, expr: ast.RecordExpr) -> Type:
        self._check_duplicate_fields(expr.fields)
        names = {f.name for f in expr.fields}
        # A record literal is effectively a constructor call on the type it
        # resolves to, so other modules' private record types neither match
        # (constructing one would be a visibility hole) nor create ambiguity.
        matches = [
            rt
            for rt in self.record_types.values()
            if {n for n, _ in rt.fields} == names and self._name_visible(rt.name)
        ]
        if len(matches) != 1:
            for f in expr.fields:
                self._check_expr(f.value)
            detail = "ambiguous" if matches else "no record type matches"
            self._err("TYPE014", f"cannot determine record type ({detail})", expr.span)
            return ERROR
        rt = matches[0]
        field_types = dict(rt.fields)
        for f in expr.fields:
            want = field_types[f.name]
            self._expect(want, self._check_expr(f.value, want), f.value.span, f"field {f.name!r}")
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
            adt_name = expr.obj.name
            owner = self.ctors.get(expr.name)
            if owner is None or owner[0] != adt_name:
                # Not that type's variant: a clean diagnostic, never a KeyError
                # ICE — and when the type's name shadows an intrinsic module
                # (a user `type Fs` breaks Std.IO's own `Fs.read_line()`),
                # say exactly that.
                help_text = None
                if (adt_name, expr.name) in _INTRINSICS:
                    help_text = (
                        f"a type named {adt_name!r} shadows the {adt_name} intrinsic "
                        f"module, so {adt_name}.{expr.name} no longer resolves; "
                        "rename the type"
                    )
                self._err(
                    "TYPE010",
                    f"type {adt_name!r} has no variant {expr.name!r}",
                    expr.span,
                    help=help_text,
                )
                return ERROR
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
        help_text = None
        if expr.name in self.functions or expr.name in self.generic_fns:
            # Dot syntax is trait-methods-only; a same-named free function is
            # almost certainly what was meant.
            help_text = (
                f"dot syntax is only for trait methods; call the free function: "
                f"{expr.name}(value, ...)"
            )
        self._err(
            "TYPE010", f"cannot access field .{expr.name} on {obj_ty}", expr.span, help=help_text
        )
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
        if expr.name in self.extern_fns:
            self._err(
                "NAME003",
                f"{expr.name!r} is an extern and cannot be passed as a value "
                "(its C ABI marshalling only happens at direct calls)",
                expr.span,
                help=f"wrap it: fn my_{expr.name}(...) = {{ {expr.name}(...) }}",
            )
            return ERROR
        if expr.name in self.functions:
            # A PURE top-level function is a value of its function type — an
            # indirect call through it can demand no effects, so the effect
            # system stays sound. Effectful functions stay call-only (a value
            # would launder their effects past the checker). Direct calls
            # never reach here (they resolve inside _infer_call).
            if self.fn_effects.get(expr.name):
                effects = ", ".join(sorted(self.fn_effects[expr.name]))
                self._err(
                    "NAME003",
                    f"{expr.name!r} uses {{ {effects} }} and cannot be passed as "
                    "a value (function types are pure)",
                    expr.span,
                    help=f"call it directly: {expr.name}(...)",
                )
                return ERROR
            self._check_visible(expr.name, expr.span)
            return self.functions[expr.name]
        if expr.name in self.generic_fns:
            self._err(
                "NAME003",
                f"{expr.name!r} is generic and cannot be passed as a value "
                "(pass a monomorphic function)",
                expr.span,
                help=f"call it directly: {expr.name}(...)",
            )
            return ERROR
        if expr.name in _BUILTINS or expr.name in _CONVERSIONS:
            self._err(
                "NAME003",
                f"{expr.name!r} is a builtin and cannot be passed as a value",
                expr.span,
                help=f"wrap it: fn my_{expr.name}(...) = {{ {expr.name}(...) }}",
            )
            return ERROR
        absent = _KNOWN_ABSENT.get(expr.name)
        if absent is not None:
            self._err(
                "NAME001",
                f"Flex does not have {expr.name!r} yet",
                expr.span,
                help=absent,
            )
            return ERROR
        spelled = _SPELLED_DIFFERENTLY.get(expr.name)
        if spelled is not None:
            self._err(
                "NAME001",
                f"unknown name {expr.name!r} — Flex spells it differently",
                expr.span,
                help=spelled,
            )
            return ERROR
        needs = _NEEDS_IMPORT.get(expr.name)
        if needs is not None:
            self._err(
                "NAME001",
                f"unknown name {expr.name!r}",
                expr.span,
                help=f"`import {needs}` provides {expr.name}",
            )
            return ERROR
        self._err("NAME001", f"unknown name {expr.name!r}", expr.span)
        return ERROR

    def _infer_unary(self, expr: ast.UnaryExpr, expected: Type | None) -> Type:
        if expr.op == "-" and isinstance(expr.operand, ast.IntLit):
            target = expected if is_int_type(expected) else I64
            # INT64_MIN and the smaller signed widths: the positive magnitude
            # alone may overflow, but the negated literal can still fit.
            self._check_int_literal(-expr.operand.value, target, expr.span)
            self.expr_types[id(expr.operand)] = target
            return target
        operand = self._check_expr(expr.operand)
        if expr.op == "-":
            if operand is F64:
                return F64
            if not is_int_type(operand):
                self._expect(I64, operand, expr.operand.span, "operand of unary `-`")
                return ERROR
            return operand
        self._expect(BOOL, operand, expr.operand.span, "operand of `!`")
        return BOOL

    def _infer_binary(self, expr: ast.BinaryExpr) -> Type:
        left = self._check_expr(expr.left)
        # The right operand sees the left's type, so `r == Ok(3)` can infer the
        # constructor's type arguments from what it's compared against.
        right = self._check_expr(
            expr.right, left if expr.op in _EQUALITY and left is not ERROR else None
        )
        op = expr.op
        if op == "++":
            self._expect(STRING, left, expr.left.span, "left operand of `++`")
            self._expect(STRING, right, expr.right.span, "right operand of `++`")
            return STRING
        if op in _ARITH:
            if op == "+" and (left is STRING or right is STRING):
                self._err(
                    "TYPE003",
                    "`+` does not concatenate strings",
                    expr.span,
                    help='use `++`: "a" ++ "b"',
                )
                return STRING if left is STRING and right is STRING else ERROR
            if left is F64 or right is F64:
                self._expect_numeric(F64, left, expr.left.span, op)
                self._expect_numeric(F64, right, expr.right.span, op)
                return F64
            self._expect_numeric(I64, left, expr.left.span, op)
            self._expect_numeric(I64, right, expr.right.span, op)
            return I64
        if op in _BITWISE:
            self._expect(I64, left, expr.left.span, f"left operand of `{op}`")
            self._expect(I64, right, expr.right.span, f"right operand of `{op}`")
            return I64
        if op in _COMPARE:
            operand = F64 if left is F64 or right is F64 else I64
            self._expect_numeric(operand, left, expr.left.span, op)
            self._expect_numeric(operand, right, expr.right.span, op)
            return BOOL
        if op in _BOOLEAN:
            self._expect(BOOL, left, expr.left.span, f"left operand of `{op}`")
            self._expect(BOOL, right, expr.right.span, f"right operand of `{op}`")
            return BOOL
        if op in _EQUALITY:
            if not _same(left, right):
                self._err("TYPE003", f"cannot compare {left} with {right}", expr.span)
            elif not self._is_comparable_type(left):
                help_text = "import Std.Str and compare with .eq()" if left is STRING else None
                self._err(
                    "TYPE019", f"`{op}` is not supported for {left}", expr.span, help=help_text
                )
            return BOOL
        return ERROR

    def _expect_numeric(self, expected: Type, actual: Type, span: Span | None, op: str) -> None:
        if actual is ERROR or actual is expected:
            return
        help_text = None
        if {expected, actual} == {I64, F64}:
            help_text = "Flex has no implicit numeric conversion; use to_f64(n) or to_i64(x)"
        self._err(
            "TYPE003",
            f"operand of `{op}` has type {actual}, expected {expected}",
            span,
            help=help_text,
        )

    def _literal_int_value(self, expr: ast.Expr) -> int | None:
        if isinstance(expr, ast.IntLit):
            return expr.value
        if (
            isinstance(expr, ast.UnaryExpr)
            and expr.op == "-"
            and isinstance(expr.operand, ast.IntLit)
        ):
            return -expr.operand.value
        return None

    def _infer_int_conversion(self, name: str, target: Type, expr: ast.CallExpr) -> Type:
        if len(expr.args) == 1:
            arg = expr.args[0]
            if self._literal_int_value(arg) is not None:
                # The target type is inference context for integer literals, so
                # to_u64(18446744073709551615) is valid while to_u8(256) is a
                # compile-time range error.
                self._expect(target, self._check_expr(arg, target), arg.span, "argument")
            else:
                actual = self._check_expr(arg)
                if actual is not F64 and not is_int_type(actual) and actual is not ERROR:
                    self._err(
                        "TYPE003",
                        f"{name} takes an integer or F64, found {actual}",
                        arg.span,
                    )
        else:
            self._err("TYPE006", f"{name} expects 1 argument", expr.span)
            for extra in expr.args:
                self._check_expr(extra)
        return target

    def _infer_call(self, expr: ast.CallExpr, expected: Type | None) -> Type:
        callee = expr.callee
        if isinstance(callee, ast.MemberExpr):
            qualified = self._infer_qualified_call(callee, expr, expected)
            if qualified is not None:
                return qualified
        # A local binding shadows every name-based dispatch (builtins, ctors,
        # global functions): a function-typed parameter named like a global
        # must call the PARAMETER, on every backend.
        if isinstance(callee, ast.NameExpr) and self.scope.lookup(callee.name) is None:
            if callee.name in _BUILTINS:
                return self._check_builtin(callee.name, expr)
            if callee.name in _INT_CONVERSIONS:
                return self._infer_int_conversion(callee.name, _INT_CONVERSIONS[callee.name], expr)
            if callee.name == "to_str":  # prelude: integer | F64 -> String
                if len(expr.args) == 1:
                    arg_ty = self._check_expr(expr.args[0])
                    if arg_ty not in (F64, ERROR) and not is_int_type(arg_ty):
                        self._err(
                            "TYPE003",
                            f"to_str takes an integer or F64, found {arg_ty}",
                            expr.args[0].span,
                        )
                else:
                    self._err("TYPE006", "to_str expects 1 argument", expr.span)
                return STRING
            if callee.name == "to_f64":  # prelude: integer -> F64
                if len(expr.args) == 1:
                    actual = self._check_expr(expr.args[0])
                    if not is_int_type(actual) and actual is not ERROR:
                        self._err(
                            "TYPE003",
                            f"argument has type {actual}, expected an integer",
                            expr.args[0].span,
                        )
                else:
                    self._err("TYPE006", "to_f64 expects 1 argument", expr.span)
                return F64
            if callee.name == "sh" and self.in_target:
                # Build intrinsic: run a shell command; Ok on exit 0, Err otherwise.
                if len(expr.args) == 1:
                    self._expect(
                        STRING, self._check_expr(expr.args[0]), expr.args[0].span, "argument"
                    )
                else:
                    self._err("TYPE006", "sh expects 1 argument (the command)", expr.span)
                self._require_effects({"Process", "Unsafe"}, expr.span)
                return self._target_result(expr.span)
            if callee.name == "exec" and self.in_target:
                # Build intrinsic: run an argv vector without going through a shell.
                if len(expr.args) == 1:
                    self._expect(
                        ListType(STRING),
                        self._check_expr(expr.args[0]),
                        expr.args[0].span,
                        "argument",
                    )
                else:
                    self._err("TYPE006", "exec expects 1 argument (the argv list)", expr.span)
                self._require_effects({"Process"}, expr.span)
                return self._target_result(expr.span)
            if callee.name in self.ctors:  # constructor call, e.g. Ok(x)
                return self._infer_ctor(callee.name, expr.args, expected, expr.span)
            if callee.name in self.generic_fns:  # bounded generic, monomorphized
                self._check_visible(callee.name, expr.span)
                return self._infer_generic_call(callee.name, expr, expected)
            if callee.name in self.functions:
                self._check_visible(callee.name, expr.span)
                fn_ty = self.functions[callee.name]
                self._check_args(callee.name, fn_ty, expr)
                self._require_effects(self.fn_effects.get(callee.name, set()), expr.span)
                return fn_ty.ret
        if isinstance(callee, ast.MemberExpr) and isinstance(callee.obj, ast.NameExpr):
            if callee.obj.name == "flx" and self.in_target:
                # Build intrinsics that drive the compiler itself on a glob of
                # files: flx.check / flx.test / flx.run / flx.expand / flx.build.
                if callee.name not in _FLX_BUILD_OPS:
                    self._err("BUILD003", f"unknown build operation flx.{callee.name}", expr.span)
                    return ERROR
                if len(expr.args) == 1:
                    self._expect(
                        STRING, self._check_expr(expr.args[0]), expr.args[0].span, "argument"
                    )
                else:
                    self._err(
                        "TYPE006", f"flx.{callee.name} expects 1 argument (a glob)", expr.span
                    )
                self._require_effects({"Fs"}, expr.span)
                return self._target_result(expr.span)
            head = callee.obj.name
            shadowed = (
                head in self.adt_templates
                or head in self.record_types
                or self.scope.lookup(head) is not None
            )
            if head == "List" and not shadowed:
                return self._infer_list_op(callee.name, expr)
            if head == "Map" and not shadowed:
                return self._infer_map_op(callee.name, expr, expected)
            if (head in _EFFECT_MODULES or head in ("Str", "Env", "Bytes")) and not shadowed:
                # A user type or binding named Str/Env/Log/... wins over the
                # intrinsic module (qualified ctors and locals stay reachable).
                return self._infer_intrinsic(head, callee.name, expr)
            if callee.obj.name in self.adt_templates and callee.name in self.ctors:
                # Qualified constructor with payload, e.g. E.Code(x).
                return self._infer_ctor(callee.name, expr.args, expected, expr.span)
        if isinstance(callee, ast.MemberExpr):
            recv_ty = self._check_expr(callee.obj)
            if isinstance(recv_ty, (ListType, MapType)) and callee.name in ("eq", "show"):
                return self._infer_container_method(callee, recv_ty, expr)
            if self._is_method_call(recv_ty, callee.name):
                return self._infer_method_call(callee, recv_ty, expr)
        callee_ty = self._check_expr(callee)
        if isinstance(callee_ty, FnType):
            self._check_args("call", callee_ty, expr)
            return callee_ty.ret
        if callee_ty is not ERROR:
            self._err("TYPE004", "expression is not callable", callee.span)
        return ERROR

    def _infer_qualified_call(
        self, callee: ast.MemberExpr, expr: ast.CallExpr, expected: Type | None
    ) -> Type | None:
        path = self._member_path(callee)
        if path is None or len(path) < 2:
            return None
        if self.scope.lookup(path[0]) is not None:
            return None
        module_name = ".".join(path[:-1])
        function_name = path[-1]
        if module_name not in self._loaded_modules():
            return None
        if self.decl_module.get(function_name) != module_name:
            for arg in expr.args:
                self._check_expr(arg)
            self._err(
                "NAME001",
                f"module {module_name!r} has no function {function_name!r}",
                expr.span,
            )
            return ERROR
        self._check_visible(function_name, callee.span)
        if function_name in self.generic_fns:
            ret = self._infer_generic_call(function_name, expr, expected)
            symbol = self.method_targets.pop(id(expr), None)
            if symbol is not None:
                self.qualified_calls[id(expr)] = symbol
            return ret
        if function_name in self.functions:
            fn_ty = self.functions[function_name]
            self._check_args(f"{module_name}.{function_name}", fn_ty, expr)
            self._require_effects(self.fn_effects.get(function_name, set()), expr.span)
            self.qualified_calls[id(expr)] = function_name
            return fn_ty.ret
        for arg in expr.args:
            self._check_expr(arg)
        self._err(
            "NAME001",
            f"module {module_name!r} has no function {function_name!r}",
            expr.span,
        )
        return ERROR

    def _infer_container_method(
        self, callee: ast.MemberExpr, recv_ty: ListType | MapType, call: ast.CallExpr
    ) -> Type:
        if callee.name == "eq":
            if len(call.args) != 1:
                for arg in call.args:
                    self._check_expr(arg)
                self._err("TYPE005", "container eq expects 1 argument", call.span)
                return BOOL
            other = self._check_expr(call.args[0], recv_ty)
            self._expect(recv_ty, other, call.args[0].span, "argument")
            if not self._is_comparable_type(recv_ty):
                self._err("TYPE019", f"eq is not supported for {recv_ty}", call.span)
            return BOOL
        if len(call.args) != 0:
            for arg in call.args:
                self._check_expr(arg)
            self._err("TYPE005", "container show expects 0 arguments", call.span)
        if not self._is_showable_type(recv_ty):
            self._err("TYPE019", f"show is not supported for {recv_ty}", call.span)
        return STRING

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
            # The param type is inference context too, so from_bytes([]) infers
            # List<I64> instead of tripping over the empty literal.
            self._expect(expected, self._check_expr(arg, expected), arg.span, "argument")
        for extra in call.args[len(params) :]:
            self._check_expr(extra)
        if effect:
            self._require_effects({effect}, call.span)
        if isinstance(ret, _InstantiateRet):
            return self._instantiate(ret.adt, list(ret.args), call.span)
        return ret

    def _infer_list_op(self, op: str, call: ast.CallExpr) -> Type:
        """The built-in growable-list operations, generic over the element type
        (so typed here, not in the monomorphic intrinsics table). Lists have
        reference semantics; mutating one needs no effect, like `mut`."""
        arity = {"len": 1, "push": 2, "set": 3, "pop": 1}.get(op)
        if arity is None:
            for arg in call.args:
                self._check_expr(arg)
            self._err(
                "TYPE010",
                f"unknown operation List.{op}",
                call.span,
                help="the list operations are List.len, List.push, List.set, and List.pop",
            )
            return ERROR
        if len(call.args) != arity:
            for arg in call.args:
                self._check_expr(arg)
            self._err("TYPE005", f"List.{op} expects {arity} argument(s)", call.span)
            return ERROR
        obj = self._check_expr(call.args[0])
        if not isinstance(obj, ListType):
            if obj is not ERROR:
                self._err(
                    "TYPE003",
                    f"List.{op} operates on a List, found {obj}",
                    call.args[0].span,
                )
            for arg in call.args[1:]:
                self._check_expr(arg)
            return ERROR
        if op == "len":
            return I64
        if op == "pop":
            # Remove and return the LAST element: Some(last), or None when empty.
            return self._instantiate("Option", [obj.elem], call.span)
        if op == "push":
            value = self._check_expr(call.args[1], obj.elem)
            self._expect(obj.elem, value, call.args[1].span, "pushed value")
            return UNIT
        index = self._check_expr(call.args[1])
        self._expect(I64, index, call.args[1].span, "index")
        value = self._check_expr(call.args[2], obj.elem)
        self._expect(obj.elem, value, call.args[2].span, "assigned element")
        return UNIT

    def _infer_map_op(self, op: str, call: ast.CallExpr, expected: Type | None) -> Type:
        """The built-in Map<String, V> operations, generic over the value type
        (so typed here, like the list ops). Maps have reference semantics and
        insertion order; mutating one needs no effect, like `mut`."""
        arity = {
            "new": 0,
            "set": 3,
            "get": 2,
            "has": 2,
            "len": 1,
            "remove": 2,
            "keys": 1,
            "values": 1,
        }.get(op)
        if arity is None:
            for arg in call.args:
                self._check_expr(arg)
            self._err(
                "TYPE010",
                f"unknown operation Map.{op}",
                call.span,
                help="the map operations are Map.new, Map.set, Map.get, Map.has, "
                "Map.len, Map.remove, Map.keys, and Map.values",
            )
            return ERROR
        if len(call.args) != arity:
            for arg in call.args:
                self._check_expr(arg)
            self._err("TYPE005", f"Map.{op} expects {arity} argument(s)", call.span)
            return ERROR
        if op == "new":
            # Like an empty list literal, an empty map needs its type from
            # context: an annotation or the parameter it's passed to.
            if isinstance(expected, MapType):
                return expected
            if expected is ERROR:
                return ERROR  # the annotation was already diagnosed; no cascade
            self._err(
                "TYPE023",
                "cannot infer the value type of an empty map here",
                call.span,
                help="annotate the binding: let m: Map<String, I64> = Map.new()",
            )
            return ERROR
        obj = self._check_expr(call.args[0])
        if not isinstance(obj, MapType):
            if obj is not ERROR:
                self._err(
                    "TYPE003",
                    f"Map.{op} operates on a Map, found {obj}",
                    call.args[0].span,
                )
            for arg in call.args[1:]:
                self._check_expr(arg)
            return ERROR
        if op in ("set", "get", "has", "remove"):
            key = self._check_expr(call.args[1])
            self._expect(STRING, key, call.args[1].span, "map key")
        if op == "set":
            value = self._check_expr(call.args[2], obj.value)
            self._expect(obj.value, value, call.args[2].span, "map value")
            return UNIT
        if op == "get":
            return self._instantiate("Option", [obj.value], call.span)
        if op == "has":
            return BOOL
        if op == "len":
            return I64
        if op == "remove":
            return UNIT
        if op == "keys":
            return ListType(STRING)
        assert op == "values"
        return ListType(obj.value)

    def _require_effects(self, effects: set[str], span: Span) -> None:
        site = "target" if self.in_target else ("test" if self.in_test else "function")
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
        self._check_visible(adt_name, span)  # a variant is as visible as its ADT
        params, variants = self.adt_templates[adt_name]
        payload_exprs = variants[vidx][1]
        if len(args) != len(payload_exprs):
            for a in args:
                self._check_expr(a)
            self._err(
                "TYPE005",
                f"{name!r} expects {len(payload_exprs)} argument(s), got {len(args)}",
                span,
            )
            return ERROR
        # Resolve type parameters from the expected type first: when that pins
        # them all, arguments are checked WITH their expected payload types, so
        # nested constructors (recursive types especially) keep inferring.
        subst: dict[str, Type] = {}
        if (
            isinstance(expected, AdtType)
            and expected.name == adt_name
            and len(expected.type_args) == len(params)
        ):
            subst = dict(zip(params, expected.type_args, strict=True))
        if all(p in subst for p in params):
            adt = self._instantiate(adt_name, [subst[p] for p in params], span)
            payload = adt.variants[vidx].payload
            for arg, pty in zip(args, payload, strict=False):
                at = self._check_expr(arg, pty)
                self._expect(pty, at, arg.span, f"argument to {name!r}")
            return adt
        # Otherwise infer them from the arguments, unifying each payload type
        # expression against the argument's type (`Chain<T>` against Chain<I64>
        # binds T = I64, not just bare `T` parameters).
        arg_types = [self._check_expr(a) for a in args]
        for pe, at in zip(payload_exprs, arg_types, strict=False):
            self._unify_typeexpr(pe, at, params, subst)
        if any(p not in subst for p in params):
            self._err(
                "TYPE016",
                f"cannot infer type arguments for {name!r} from context",
                span,
                help="use the value where its full type is known, or annotate it "
                "(e.g. `fn f() -> Result<I64, String> = { Ok(2) }`)",
            )
            return ERROR
        adt = self._instantiate(adt_name, [subst[p] for p in params], span)
        for arg, at, pty in zip(args, arg_types, adt.variants[vidx].payload, strict=False):
            self._expect(pty, at, arg.span, f"argument to {name!r}")
        return adt

    def _unify_typeexpr(
        self, pe: ast.TypeExpr, at: Type, params: list[str] | set[str], subst: dict[str, Type]
    ) -> None:
        """Bind type parameters in `pe` by matching it structurally against `at`.
        Best-effort: mismatched heads bind nothing (the later _expect reports)."""
        if pe.name in params and not pe.args:
            subst.setdefault(pe.name, at)
            return
        if isinstance(at, AdtType) and pe.name == at.name and len(pe.args) == len(at.type_args):
            for sub_pe, sub_at in zip(pe.args, at.type_args, strict=True):
                self._unify_typeexpr(sub_pe, sub_at, params, subst)
            return
        if isinstance(at, ListType) and pe.name == "List" and len(pe.args) == 1:
            self._unify_typeexpr(pe.args[0], at.elem, params, subst)
            return
        if isinstance(at, MapType) and pe.name == "Map" and len(pe.args) == 2:
            self._unify_typeexpr(pe.args[0], STRING, params, subst)
            self._unify_typeexpr(pe.args[1], at.value, params, subst)
            return
        if isinstance(at, FnType) and pe.name == "->" and len(pe.args) == len(at.params) + 1:
            for sub_pe, sub_at in zip(pe.args, [*at.params, at.ret], strict=True):
                self._unify_typeexpr(sub_pe, sub_at, params, subst)

    def _infer_try(self, expr: ast.TryExpr) -> Type:
        inner = self._check_expr(expr.expr)
        if not (
            isinstance(inner, AdtType) and inner.name == "Result" and len(inner.type_args) == 2
        ):
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

    def _infer_match(
        self, expr: ast.MatchExpr, expected: Type | None, value_used: bool = True
    ) -> Type:
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
            if catchall:
                self._err(
                    "MATCH002",
                    "unreachable arm: a catch-all pattern precedes it",
                    arm.pattern.span,
                )
            self.scope.push()
            if self._bind_pattern(arm.pattern, scrut, variants, covered):
                catchall = True
            body_ty = self._check_expr(arm.body, expected if value_used else None, value_used)
            self.scope.pop()
            if not value_used:
                continue  # statement position: arm types need not agree
            if isinstance(body_ty, FnType):
                self._err(
                    "TYPE025",
                    "a `match` cannot yield a function value yet",
                    arm.span,
                )
                body_ty = ERROR
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
            self._err(
                "MATCH001",
                f"non-exhaustive match; missing {missing}",
                expr.span,
                help="arms with literal or nested sub-patterns don't count toward "
                "coverage; add a catch-all arm (`_ => ...`) or an all-binders arm",
            )
        if not value_used:
            return UNIT
        return result if result is not None else UNIT

    def _bind_pattern(
        self,
        pattern: ast.Pattern,
        scrut_ty: Type,
        variants: dict[str, VariantDef],
        covered: set[str],
    ) -> bool:
        """Type-check one arm's pattern, bind its names, and track coverage.
        Returns True when the pattern is irrefutable (a catch-all)."""
        if isinstance(pattern, ast.WildcardPattern):
            return True
        if isinstance(pattern, ast.BindPattern):
            self.scope.define(pattern.name, _Binding(scrut_ty, mutable=False))
            return True
        if isinstance(pattern, ast.LiteralPattern):
            self._check_literal_pattern(pattern, scrut_ty)
            return False
        if isinstance(pattern, ast.CtorPattern):
            variant = variants.get(pattern.name)
            if variant is None:
                if scrut_ty is not ERROR:
                    # An ERROR scrutinee was already diagnosed; binding the
                    # pattern's names as ERROR keeps the arm body quiet too.
                    self._err(
                        "MATCH003",
                        f"{pattern.name!r} is not a variant of {scrut_ty}",
                        pattern.span,
                    )
                for arg in pattern.args:
                    if isinstance(arg, ast.BindPattern):
                        self.scope.define(arg.name, _Binding(ERROR, mutable=False))
                return False
            # Naming a constructor in a pattern is a reference to its ADT: a
            # variant is as visible as its type, in patterns as in expressions.
            owner = self.ctors.get(pattern.name)
            if owner is not None:
                self._check_visible(owner[0], pattern.span)
            if len(pattern.args) != len(variant.payload):
                self._err(
                    "TYPE005",
                    f"{pattern.name!r} expects {len(variant.payload)} pattern argument(s)",
                    pattern.span,
                )
            bound: set[str] = set()
            for sub, pty in zip(pattern.args, variant.payload, strict=False):
                self._check_subpattern(sub, pty, bound)
            # Only an arm that takes the WHOLE variant (every argument
            # irrefutable) counts toward exhaustiveness; `Succ(Zero)` does not
            # cover Succ. Refutable arms for an already-covered variant are
            # unreachable, which MATCH002 reports as a duplicate.
            if _irrefutable_args(pattern):
                if pattern.name in covered:
                    self._err("MATCH002", f"duplicate match arm for {pattern.name!r}", pattern.span)
                covered.add(pattern.name)
            elif pattern.name in covered:
                self._err(
                    "MATCH002",
                    f"unreachable arm: {pattern.name!r} is already fully covered",
                    pattern.span,
                )
        return False

    def _check_subpattern(self, pattern: ast.Pattern, ty: Type, bound: set[str]) -> None:
        if isinstance(pattern, ast.BindPattern):
            if pattern.name in bound:
                self._err(
                    "NAME002",
                    f"pattern binds {pattern.name!r} more than once",
                    pattern.span,
                    help="rename one of the binders (patterns are not equality constraints)",
                )
            bound.add(pattern.name)
            self.scope.define(pattern.name, _Binding(ty, mutable=False))
        elif isinstance(pattern, ast.LiteralPattern):
            self._check_literal_pattern(pattern, ty)
        elif isinstance(pattern, ast.CtorPattern):
            if not isinstance(ty, AdtType):
                if ty is not ERROR:
                    self._err(
                        "MATCH003",
                        f"constructor pattern {pattern.name!r} cannot match a value of type {ty}",
                        pattern.span,
                    )
                return
            variant = next((v for v in ty.variants if v.name == pattern.name), None)
            if variant is None:
                self._err("MATCH003", f"{pattern.name!r} is not a variant of {ty}", pattern.span)
                return
            if len(pattern.args) != len(variant.payload):
                self._err(
                    "TYPE005",
                    f"{pattern.name!r} expects {len(variant.payload)} pattern argument(s)",
                    pattern.span,
                )
            for sub, pty in zip(pattern.args, variant.payload, strict=False):
                self._check_subpattern(sub, pty, bound)

    def _check_literal_pattern(self, pattern: ast.LiteralPattern, ty: Type) -> None:
        lit_ty = BOOL if isinstance(pattern.value, bool) else (ty if is_int_type(ty) else I64)
        if not _same(ty, lit_ty):
            self._err(
                "TYPE003",
                f"pattern has type {lit_ty}, but matches a value of type {ty}",
                pattern.span,
            )
        if not isinstance(pattern.value, bool):
            self._check_int_literal(pattern.value, lit_ty, pattern.span)

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
                elif a is STRING and ("Eq", "String") in self.impls:
                    pass  # compared through the Eq trait (import Std.Str)
                elif not self._is_comparable_type(a):
                    key = _type_key(a)
                    if key != "?" and ("Eq", key) in self.impls:
                        # Not structurally comparable, but the type carries an
                        # Eq impl (derive(Eq) or hand-written): both backends
                        # dispatch the assertion through it.
                        self.assert_impls[id(call)] = self.impls[("Eq", key)]["eq"]
                    else:
                        help_text = None
                        if a is STRING:
                            help_text = "import Std.Str to compare strings"
                        elif isinstance(a, (RecordType, AdtType)):
                            help_text = (
                                f"derive(Eq) on {key!r} enables {name} "
                                "(import Std.Str if it carries strings)"
                            )
                        self._err(
                            "TYPE019",
                            f"{name} is not supported for {a}",
                            call.span,
                            help=help_text,
                        )
        elif name in ("fail", "panic"):
            for arg in call.args:
                self._check_expr(arg)
            if len(call.args) != 1:
                self._err("TYPE006", f"{name} expects 1 argument", call.span)
            elif self.expr_types[id(call.args[0])] not in (STRING, ERROR):
                self._err("TYPE007", f"{name} expects a String message", call.args[0].span)
        return UNIT

    def _infer_if(self, expr: ast.IfExpr, expected: Type | None, value_used: bool = True) -> Type:
        cond = self._check_expr(expr.cond)
        self._expect(BOOL, cond, expr.cond.span, "if condition")
        if not value_used:
            # Statement position: nobody consumes the value, so the branches
            # need not agree on a type. The whole expression is Unit.
            self._check_block(expr.then_block, None, False)
            if expr.else_block is not None:
                self._check_block(expr.else_block, None, False)
            return UNIT
        then_ty = self._check_block(expr.then_block, expected)
        if isinstance(then_ty, FnType):
            self._err(
                "TYPE025",
                "an `if` cannot yield a function value yet",
                expr.span,
                help="select with a direct call in each branch instead",
            )
            return ERROR
        if expr.else_block is None:
            # Value position with no else: there is no value on the false path.
            if then_ty is not UNIT and then_ty is not ERROR and not _diverges(expr.then_block):
                self._err(
                    "TYPE008",
                    f"`if` without `else` has no value, but its branch yields {then_ty}",
                    expr.span,
                    help="add an `else` branch to use the `if` as a value",
                )
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

    def _is_comparable_type(self, ty: Type, seen: frozenset[Type] = frozenset()) -> bool:
        """Whether equality can be lowered in this module.

        String equality is intentionally import-scoped: `Std.Str` provides the
        `Eq String` impl used by native lowering. Containers and records compose
        through this check so `List<String>` and records with string fields work
        after that import, but fail with the existing hint otherwise.
        """
        if ty is STRING:
            return ("Eq", "String") in self.impls
        if ty is BYTES or isinstance(ty, FnType):
            return False
        if isinstance(ty, ListType):
            return self._is_comparable_type(ty.elem, seen)
        if isinstance(ty, MapType):
            return self._is_comparable_type(ty.value, seen)
        if ty in seen:
            return False
        if isinstance(ty, RecordType):
            return all(self._is_comparable_type(t, seen | {ty}) for _, t in ty.fields)
        if isinstance(ty, AdtType):
            return all(
                all(t is STRING or self._is_comparable_type(t, seen | {ty}) for t in v.payload)
                for v in ty.variants
            )
        return True

    def _is_showable_type(self, ty: Type) -> bool:
        if is_int_type(ty) or ty in (F64, BOOL, UNIT, STRING):
            return True
        if isinstance(ty, ListType):
            return self._is_showable_type(ty.elem)
        if isinstance(ty, MapType):
            return self._is_showable_type(ty.value)
        key = _type_key(ty)
        return key != "?" and ("Show", key) in self.impls

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
        if type_expr.name == "->":
            resolved = [self._resolve_type(a) for a in type_expr.args]
            return FnType(tuple(resolved[:-1]), resolved[-1])
        if type_expr.name == "List":
            if len(type_expr.args) != 1:
                self._err("TYPE013", "List expects exactly 1 type argument", type_expr.span)
                return ERROR
            elem = self._resolve_type(type_expr.args[0])
            if isinstance(elem, FnType):
                self._err(
                    "TYPE025",
                    "function values cannot be stored in lists yet",
                    type_expr.span,
                    help="pass functions as arguments; storing them is the roadmap",
                )
                return ERROR
            return ListType(elem)
        if type_expr.name == "Map":
            if len(type_expr.args) != 2:
                self._err(
                    "TYPE013",
                    "Map expects exactly 2 type arguments: Map<String, V>",
                    type_expr.span,
                )
                return ERROR
            key = self._resolve_type(type_expr.args[0])
            if key is not STRING and key is not ERROR:
                self._err(
                    "TYPE003",
                    f"Map keys are String (for now), found {key}",
                    type_expr.args[0].span,
                    help="render other key types with to_str",
                )
                return ERROR
            value = self._resolve_type(type_expr.args[1])
            if isinstance(value, FnType):
                self._err(
                    "TYPE025",
                    "function values cannot be stored in maps yet",
                    type_expr.span,
                    help="pass functions as arguments; storing them is the roadmap",
                )
                return ERROR
            return MapType(value)
        if type_expr.name in self.record_types and not type_expr.args:
            self._check_visible(type_expr.name, type_expr.span)
            return self.record_types[type_expr.name]
        if type_expr.name in self.adt_templates:
            self._check_visible(type_expr.name, type_expr.span)
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
        key = (adt_name, tuple(type_args))
        cached = self._adt_cache.get(key)
        if cached is not None:
            return cached
        if self._inst_depth > 64:
            # Regular recursion hits the cache above; only a payload whose type
            # arguments GROW (polymorphic recursion, e.g. `| Wrap(Bad<Option<T>>)`)
            # gets here. Fail fast: every unwind level would otherwise retry.
            raise FlexError(
                [
                    Diagnostic(
                        "TYPE024",
                        f"instantiating type {adt_name!r} does not converge "
                        "(polymorphic recursion is not supported)",
                        span,
                        help="a recursive payload must use the type's own parameters "
                        "unchanged, e.g. `| Wrap(Bad<T>)`, not `| Wrap(Bad<Option<T>>)`",
                    )
                ]
            )
        # Cache the instantiation BEFORE resolving its payloads: a recursive
        # payload re-demands this same key and gets this same object, so the
        # recursion bottoms out and the knot ties back to one node.
        adt = AdtType(adt_name, (), tuple(type_args))
        self._adt_cache[key] = adt
        saved = self._subst
        self._subst = dict(zip(params, type_args, strict=True))
        self._inst_depth += 1
        try:
            defs = tuple(
                VariantDef(vname, tuple(self._resolve_type(pe) for pe in payload))
                for vname, payload in variants
            )
            for vdef in defs:
                if any(isinstance(t, FnType) for t in vdef.payload):
                    self._err(
                        "TYPE025",
                        f"variant {vdef.name!r} carries a function type; function "
                        "values cannot be stored in ADT payloads yet",
                        span,
                    )
        finally:
            self._inst_depth -= 1
            self._subst = saved
        object.__setattr__(adt, "variants", defs)  # settle the frozen placeholder
        return adt

    def _expect(self, expected: Type, actual: Type, span: Span, what: str) -> None:
        if expected is ERROR or actual is ERROR:
            return
        if not _same(expected, actual):
            self._err("TYPE003", f"{what} has type {actual}, expected {expected}", span)

    def _err(self, code: str, message: str, span: Span | None, *, help: str | None = None) -> None:
        diag = Diagnostic(code, message, span, help=help)
        if diag not in self.diags:  # the same fault re-derived is reported once
            self.diags.append(diag)

    def _err_duplicate(self, kind: str, name: str, span: Span | None) -> None:
        """A top-level redefinition. Top-level names share one program-wide
        namespace in this release, so name the colliding modules when known."""
        first = self.decl_module.get(name)
        again = self._module_of(span)
        where = ""
        if first is not None and again is not None and first != again:
            where = f" (first in module {first!r}, again in module {again!r})"
        self._err(
            "TYPE002",
            f"{kind} {name!r} is already defined{where}",
            span,
            help="top-level names share one namespace; rename one of them" if where else None,
        )


def _same(a: Type, b: Type) -> bool:
    return a is ERROR or b is ERROR or a == b


def _irrefutable_args(pattern: ast.CtorPattern) -> bool:
    """Whether every constructor argument always matches (binders/wildcards
    only), so the arm takes the whole variant. Nested constructor or literal
    arguments are refutable — `Succ(Zero)` does not cover Succ."""
    return all(isinstance(a, (ast.BindPattern, ast.WildcardPattern)) for a in pattern.args)


def _slot_inline(ty: Type) -> bool:
    """Whether an ADT payload of this type lives in the i64 payload slot by
    VALUE natively (anything else is boxed behind a pointer). F64 qualifies —
    its bits ride the slot, and the native equality lowering compares those
    variants as FLOATS (so Some(0.0) == Some(-0.0) and Some(nan) != Some(nan),
    matching the interpreter)."""
    return (
        is_int_type(ty)
        or ty is BOOL
        or ty is UNIT
        or ty is F64
        or (isinstance(ty, AdtType) and all(not v.payload for v in ty.variants))
    )


def _is_comparable(ty: Type) -> bool:
    """Whether `==`/`!=`/assert_eq can be lowered for this type (no strings yet).
    ADTs qualify only when every payload is slot-inline: a boxed payload would
    compare as a pointer natively, which is not structural equality."""
    if ty in (STRING, BYTES) or isinstance(ty, FnType):
        return False
    if isinstance(ty, ListType):
        return _is_comparable(ty.elem)
    if isinstance(ty, MapType):
        return _is_comparable(ty.value)
    if isinstance(ty, RecordType):
        return all(_is_comparable(t) for _, t in ty.fields)
    if isinstance(ty, AdtType):
        # String payloads compare by CONTENT (a runtime helper natively, str
        # equality in the interpreter) — Option<String> equality is what every
        # Map.get/read_line call site wants. Other boxed payloads would
        # compare as pointers, so they stay out.
        return all(
            len(v.payload) <= 1 and all(_slot_inline(t) or t is STRING for t in v.payload)
            for v in ty.variants
        )
    return True


def _is_showable(ty: Type) -> bool:
    if is_int_type(ty) or ty in (F64, BOOL, UNIT, STRING):
        return True
    if isinstance(ty, ListType):
        return _is_showable(ty.elem)
    if isinstance(ty, MapType):
        return _is_showable(ty.value)
    return False


def _diverges(block: ast.Block) -> bool:
    """Whether the block is guaranteed to return (so it needs no tail value)."""
    if not block.stmts:
        return False
    last = block.stmts[-1]
    if isinstance(last, ast.ReturnStmt):
        return True
    if isinstance(last, ast.ExprStmt):
        return _expr_diverges(last.expr)
    return False


def _expr_diverges(expr: ast.Expr) -> bool:
    if isinstance(expr, ast.IfExpr):
        return (
            expr.else_block is not None
            and _diverges(expr.then_block)
            and _diverges(expr.else_block)
        )
    if isinstance(expr, ast.MatchExpr):
        return bool(expr.arms) and all(_expr_diverges(arm.body) for arm in expr.arms)
    if isinstance(expr, ast.BlockExpr):
        return _diverges(expr.body)
    return False


def check(
    module: ast.Module,
    decl_module: dict[str, str] | None = None,
    public: set[str] | None = None,
    file_module: dict[str, str] | None = None,
    module_spans: list[tuple[str, Span]] | None = None,
    builtin_records: dict[str, RecordType] | None = None,
) -> CheckResult:
    return Checker(module, decl_module, public, file_module, module_spans, builtin_records).check()
