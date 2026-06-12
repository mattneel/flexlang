"""`derive(Eq)` / `derive(Show)` code generation.

Generates real `FnDecl`s that flow through the normal checker/backend (they
appear in `flx expand`). The generators are TYPE-DRIVEN over the declared
field/payload type expressions:

* slot-comparable types (scalars, enums, inline-payload ADTs, records of
  those) compare structurally with `==` — exactly what the backends lower;
* String compares through the Eq trait (`import Std.Str`);
* records/ADTs that are NOT structurally comparable compare through THEIR
  `eq` impl, so nested derives compose;
* List fields get generated structural helper functions (element-wise loops)
  — list types cannot carry impls, so derive writes the code instead;
* String-carrying ADTs get a match-based equality, arm by arm.

Generic types are reported (DER004), not mis-generated; Map fields are
rejected with the reason (reference semantics).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flx.diagnostics import Diagnostic, FlexError, Span
from flx.syntax import ast

if TYPE_CHECKING:
    from flx.macro.expand import Expander

_SUPPORTED = {"Eq", "Show"}

_SCALARS = {"I64", "F64", "Bool", "Unit"}


def run_derives(module: ast.Module, exp: Expander) -> list[ast.Item]:
    out: list[ast.Item] = []
    gen = _HelperGen(exp)
    for record in module.records:
        out.extend(
            _derive_type(record.name, record.type_params, record.span, record.derives, exp, gen)
        )
    for adt in module.adts:
        out.extend(_derive_type(adt.name, adt.type_params, adt.span, adt.derives, exp, gen))
    out.extend(gen.helpers.values())
    return out


def _derive_type(
    name: str,
    type_params: list[str],
    span: Span,
    derives: list[str],
    exp: Expander,
    gen: _HelperGen,
) -> list[ast.ImplDecl]:
    out: list[ast.ImplDecl] = []
    for trait in derives:
        if trait not in _SUPPORTED:
            raise _err("DER001", f"cannot derive {trait!r} (only Eq, Show)", span)
        if type_params:
            raise _err("DER004", f"cannot derive {trait!r} on a generic type yet", span)
        method = (
            _derive_eq(name, span, exp, gen)
            if trait == "Eq"
            else _derive_show(name, span, exp, gen)
        )
        out.append(ast.ImplDecl(trait, name, [method], span))
    return out


# --- syntactic comparability ----------------------------------------------------
# Mirrors the checker's _is_comparable/_slot_inline over declared TypeExprs, so
# the generator can choose structural `==` exactly where the backends lower it.


def _payload_inline_syntactic(te: ast.TypeExpr, exp: Expander) -> bool:
    if te.name in _SCALARS and not te.args:
        return True
    adt = exp.ctx.adts.get(te.name)
    # A payloadless enum rides the slot as its tag.
    return adt is not None and all(not v.payload for v in adt.variants)


def _comparable_syntactic(te: ast.TypeExpr, exp: Expander, visited: frozenset[str]) -> bool:
    name = te.name
    if name in _SCALARS and not te.args:
        return True
    if name in ("String", "List", "Map", "->"):
        return False
    if name in visited:
        return True  # cycles are rejected elsewhere; don't recurse forever
    record = exp.ctx.records.get(name)
    if record is not None:
        return all(_comparable_syntactic(f.type, exp, visited | {name}) for f in record.fields)
    adt = exp.ctx.adts.get(name)
    if adt is not None:
        # Mirrors the checker's _is_comparable: single String payloads compare
        # by content, so they ride structural equality too.
        return all(
            len(v.payload) <= 1
            and all(
                _payload_inline_syntactic(p, exp) or (p.name == "String" and not p.args)
                for p in v.payload
            )
            for v in adt.variants
        )
    if name in ("Option", "Result"):
        # Builtins: payloads are exactly the type arguments.
        return all(
            _payload_inline_syntactic(a, exp) or (a.name == "String" and not a.args)
            for a in te.args
        )
    return False  # unknown (type params etc.): route through .eq() instead


# --- AST builders -------------------------------------------------------------


def _name(n: str, sp: Span) -> ast.NameExpr:
    return ast.NameExpr(n, sp)


def _ty(n: str, sp: Span) -> ast.TypeExpr:
    return ast.TypeExpr(n, [], sp)


def _fn(
    name: str,
    params: list[tuple[str, ast.TypeExpr]],
    ret: str,
    body: list[ast.Stmt],
    sp: Span,
) -> ast.FnDecl:
    ps = [ast.Param(p, t, sp) for p, t in params]
    return ast.FnDecl(name, ps, _ty(ret, sp), [], ast.Block(body, sp), sp)


def _expr_fn(
    name: str, params: list[tuple[str, ast.TypeExpr]], ret: str, body: ast.Expr, sp: Span
) -> ast.FnDecl:
    return _fn(name, params, ret, [ast.ExprStmt(body, sp)], sp)


def _concat(parts: list[ast.Expr], sp: Span) -> ast.Expr:
    result = parts[0]
    for part in parts[1:]:
        result = ast.BinaryExpr("++", result, part, sp)
    return result


def _and_all(parts: list[ast.Expr], sp: Span) -> ast.Expr:
    if not parts:
        return ast.BoolLit(True, sp)
    result = parts[0]
    for part in parts[1:]:
        result = ast.BinaryExpr("&&", result, part, sp)
    return result


def _call_method(recv: ast.Expr, method: str, args: list[ast.Expr], sp: Span) -> ast.Expr:
    return ast.CallExpr(ast.MemberExpr(recv, method, sp), args, sp)


# --- generated list helpers ------------------------------------------------------


def _enc(te: ast.TypeExpr) -> str:
    """An identifier-safe, INJECTIVE encoding of a type expression for helper
    names: arity-prefixed like the checker's _type_enc, case preserved — so
    `Foo` and `FOO` (or `List<I64>` and a record named List_I64) never share
    a generated helper."""
    name = te.name.replace("->", "fn")
    parts = [f"{len(te.args)}_{name}"]
    parts.extend(_enc(a) for a in te.args)
    return "_".join(parts)


class _HelperGen:
    """Structural helpers for List fields, generated once per element type.
    Derives run on the MERGED module, so one dedupe table covers the program."""

    def __init__(self, exp: Expander) -> None:
        self.exp = exp
        self.helpers: dict[str, ast.FnDecl] = {}

    def list_eq(self, elem: ast.TypeExpr, sp: Span) -> str:
        fname = f"__derive_list_eq_{_enc(elem)}"
        if fname in self.helpers:
            return fname
        self.helpers[fname] = _fn("", [], "Bool", [], sp)  # placeholder breaks recursion
        list_te = ast.TypeExpr("List", [elem], sp)
        a, b, i = _name("a", sp), _name("b", sp), _name("i", sp)
        elem_cmp = _eq_expr(
            ast.IndexExpr(a, i, sp), ast.IndexExpr(b, i, sp), elem, sp, self.exp, self
        )

        def len_of(v: ast.Expr) -> ast.Expr:
            return ast.CallExpr(ast.MemberExpr(_name("List", sp), "len", sp), [v], sp)

        body: list[ast.Stmt] = [
            ast.ExprStmt(
                ast.IfExpr(
                    ast.BinaryExpr("!=", len_of(a), len_of(b), sp),
                    ast.Block([ast.ReturnStmt(ast.BoolLit(False, sp), sp)], sp),
                    None,
                    sp,
                ),
                sp,
            ),
            ast.MutStmt("i", ast.IntLit(0, sp), sp),
            ast.WhileStmt(
                ast.BinaryExpr("<", i, len_of(a), sp),
                ast.Block(
                    [
                        ast.ExprStmt(
                            ast.IfExpr(
                                ast.UnaryExpr("!", elem_cmp, sp),
                                ast.Block([ast.ReturnStmt(ast.BoolLit(False, sp), sp)], sp),
                                None,
                                sp,
                            ),
                            sp,
                        ),
                        ast.AssignStmt("i", ast.BinaryExpr("+", i, ast.IntLit(1, sp), sp), sp),
                    ],
                    sp,
                ),
                sp,
            ),
            ast.ExprStmt(ast.BoolLit(True, sp), sp),
        ]
        self.helpers[fname] = _fn(fname, [("a", list_te), ("b", list_te)], "Bool", body, sp)
        return fname

    def _variant_eq_fn(
        self,
        fname: str,
        te: ast.TypeExpr,
        variants: list[tuple[str, list[ast.TypeExpr]]],
        sp: Span,
    ) -> None:
        """fn fname(a: te, b: te) -> Bool comparing builtin-ADT values arm by
        arm (Option/Result fields can't carry impls — generic instantiations
        aren't impl targets — so derive writes the match instead)."""
        multi = len(variants) > 1
        outer_arms: list[ast.MatchArm] = []
        for vname, payload in variants:
            n = len(payload)
            x_binds: list[ast.Pattern] = [ast.BindPattern(f"x{i}", sp) for i in range(n)]
            y_binds: list[ast.Pattern] = [ast.BindPattern(f"y{i}", sp) for i in range(n)]
            cmps = [
                _eq_expr(_name(f"x{i}", sp), _name(f"y{i}", sp), pty, sp, self.exp, self)
                for i, pty in enumerate(payload)
            ]
            inner_arms = [ast.MatchArm(ast.CtorPattern(vname, y_binds, sp), _and_all(cmps, sp), sp)]
            if multi:
                inner_arms.append(ast.MatchArm(ast.WildcardPattern(sp), ast.BoolLit(False, sp), sp))
            inner = ast.MatchExpr(_name("b", sp), inner_arms, sp)
            outer_arms.append(ast.MatchArm(ast.CtorPattern(vname, x_binds, sp), inner, sp))
        match = ast.MatchExpr(_name("a", sp), outer_arms, sp)
        self.helpers[fname] = _expr_fn(fname, [("a", te), ("b", te)], "Bool", match, sp)

    def _variant_show_fn(
        self,
        fname: str,
        te: ast.TypeExpr,
        variants: list[tuple[str, list[ast.TypeExpr]]],
        sp: Span,
    ) -> None:
        """fn fname(v: te) -> String rendering builtin-ADT values arm by arm."""
        arms: list[ast.MatchArm] = []
        for vname, payload in variants:
            if payload:
                binds: list[ast.Pattern] = [
                    ast.BindPattern(f"x{i}", sp) for i in range(len(payload))
                ]
                parts: list[ast.Expr] = [ast.StringLit(vname + "(", sp)]
                for i, pty in enumerate(payload):
                    if i:
                        parts.append(ast.StringLit(", ", sp))
                    parts.append(_show_expr(_name(f"x{i}", sp), pty, sp, self.exp, self))
                parts.append(ast.StringLit(")", sp))
                body: ast.Expr = _concat(parts, sp)
                pattern: ast.Pattern = ast.CtorPattern(vname, binds, sp)
            else:
                pattern = ast.CtorPattern(vname, [], sp)
                body = ast.StringLit(vname, sp)
            arms.append(ast.MatchArm(pattern, body, sp))
        match = ast.MatchExpr(_name("v", sp), arms, sp)
        self.helpers[fname] = _expr_fn(fname, [("v", te)], "String", match, sp)

    def builtin_adt_eq(self, te: ast.TypeExpr, sp: Span) -> str:
        fname = f"__derive_eq_{_enc(te)}"
        if fname not in self.helpers:
            self.helpers[fname] = _fn("", [], "Bool", [], sp)  # placeholder
            self._variant_eq_fn(fname, te, _builtin_variants(te), sp)
        return fname

    def builtin_adt_show(self, te: ast.TypeExpr, sp: Span) -> str:
        fname = f"__derive_show_{_enc(te)}"
        if fname not in self.helpers:
            self.helpers[fname] = _fn("", [], "String", [], sp)  # placeholder
            self._variant_show_fn(fname, te, _builtin_variants(te), sp)
        return fname

    def list_show(self, elem: ast.TypeExpr, sp: Span) -> str:
        fname = f"__derive_list_show_{_enc(elem)}"
        if fname in self.helpers:
            return fname
        self.helpers[fname] = _fn("", [], "String", [], sp)  # placeholder breaks recursion
        list_te = ast.TypeExpr("List", [elem], sp)
        xs, i, out = _name("xs", sp), _name("i", sp), _name("out", sp)
        elem_render = _show_expr(ast.IndexExpr(xs, i, sp), elem, sp, self.exp, self)
        len_of = ast.CallExpr(ast.MemberExpr(_name("List", sp), "len", sp), [xs], sp)
        body: list[ast.Stmt] = [
            ast.MutStmt("out", ast.StringLit("[", sp), sp),
            ast.MutStmt("i", ast.IntLit(0, sp), sp),
            ast.WhileStmt(
                ast.BinaryExpr("<", i, len_of, sp),
                ast.Block(
                    [
                        ast.ExprStmt(
                            ast.IfExpr(
                                ast.BinaryExpr(">", i, ast.IntLit(0, sp), sp),
                                ast.Block(
                                    [
                                        ast.AssignStmt(
                                            "out",
                                            ast.BinaryExpr("++", out, ast.StringLit(", ", sp), sp),
                                            sp,
                                        )
                                    ],
                                    sp,
                                ),
                                None,
                                sp,
                            ),
                            sp,
                        ),
                        ast.AssignStmt("out", ast.BinaryExpr("++", out, elem_render, sp), sp),
                        ast.AssignStmt("i", ast.BinaryExpr("+", i, ast.IntLit(1, sp), sp), sp),
                    ],
                    sp,
                ),
                sp,
            ),
            ast.ExprStmt(ast.BinaryExpr("++", out, ast.StringLit("]", sp), sp), sp),
        ]
        self.helpers[fname] = _fn(fname, [("xs", list_te)], "String", body, sp)
        return fname


# --- per-type comparison / rendering expressions ----------------------------------


def _builtin_variants(te: ast.TypeExpr) -> list[tuple[str, list[ast.TypeExpr]]]:
    """Variant shapes of the builtin generic ADTs, instantiated at te's args."""
    if te.name == "Option":
        return [("None", []), ("Some", [te.args[0]])]
    assert te.name == "Result"
    return [("Ok", [te.args[0]]), ("Err", [te.args[1]])]


def _eq_expr(
    mine: ast.Expr, theirs: ast.Expr, te: ast.TypeExpr, sp: Span, exp: Expander, gen: _HelperGen
) -> ast.Expr:
    if te.name == "String":
        return _call_method(mine, "eq", [theirs], sp)
    if te.name == "List":
        helper = gen.list_eq(te.args[0], sp)
        return ast.CallExpr(_name(helper, sp), [mine, theirs], sp)
    if te.name == "Map":
        raise _err(
            "DER001",
            "cannot derive Eq over a Map field: maps have reference semantics",
            sp,
        )
    if _comparable_syntactic(te, exp, frozenset()):
        return ast.BinaryExpr("==", mine, theirs, sp)
    if te.name in ("Option", "Result") and te.args:
        # Generic builtin instantiations can't carry impls; derive writes the
        # match instead, recursing into the payload comparison.
        helper = gen.builtin_adt_eq(te, sp)
        return ast.CallExpr(_name(helper, sp), [mine, theirs], sp)
    # A composite that isn't structurally comparable: go through ITS Eq impl
    # (its own derive, or a hand-written one) — nested derives compose.
    return _call_method(mine, "eq", [theirs], sp)


def _show_expr(
    value: ast.Expr, te: ast.TypeExpr, sp: Span, exp: Expander, gen: _HelperGen
) -> ast.Expr:
    if te.name in ("I64", "F64") and not te.args:
        return ast.CallExpr(_name("to_str", sp), [value], sp)
    if te.name == "Bool":
        then = ast.Block([ast.ExprStmt(ast.StringLit("true", sp), sp)], sp)
        els = ast.Block([ast.ExprStmt(ast.StringLit("false", sp), sp)], sp)
        return ast.IfExpr(value, then, els, sp)
    if te.name == "Unit" and not te.args:
        return ast.StringLit("()", sp)
    if te.name == "String":
        return value
    if te.name == "List":
        helper = gen.list_show(te.args[0], sp)
        return ast.CallExpr(_name(helper, sp), [value], sp)
    if te.name == "Map":
        raise _err(
            "DER001",
            "cannot derive Show over a Map field: render its contents explicitly "
            "(Map.keys + Map.get)",
            sp,
        )
    if te.name in ("Option", "Result") and te.args:
        helper = gen.builtin_adt_show(te, sp)
        return ast.CallExpr(_name(helper, sp), [value], sp)
    # Any other type renders through ITS Show impl — recursive derives work,
    # and a type with no Show in scope reports DISP001 at the derive line.
    return _call_method(value, "show", [], sp)


# --- Eq -----------------------------------------------------------------------


def _derive_eq(type_name: str, sp: Span, exp: Expander, gen: _HelperGen) -> ast.FnDecl:
    self_te = _ty(type_name, sp)
    record = exp.ctx.records.get(type_name)
    if record is not None:
        comparisons = [
            _eq_expr(
                ast.MemberExpr(_name("self", sp), fld.name, sp),
                ast.MemberExpr(_name("other", sp), fld.name, sp),
                fld.type,
                sp,
                exp,
                gen,
            )
            for fld in record.fields
        ]
        return _expr_fn(
            "eq",
            [("self", self_te), ("other", self_te)],
            "Bool",
            _and_all(comparisons, sp),
            sp,
        )
    adt = exp.ctx.adts.get(type_name)
    if adt is not None and not _comparable_syntactic(self_te, exp, frozenset()):
        return _eq_adt(adt, sp, exp, gen)
    # Structurally comparable (enums, inline payloads): the backends lower
    # whole-value equality directly.
    body = ast.BinaryExpr("==", _name("self", sp), _name("other", sp), sp)
    return _expr_fn("eq", [("self", self_te), ("other", self_te)], "Bool", body, sp)


def _eq_adt(adt: ast.AdtDecl, sp: Span, exp: Expander, gen: _HelperGen) -> ast.FnDecl:
    """Match-based equality: same variant AND equal payloads, arm by arm —
    what the DER001 String-payload ban used to make people write by hand."""
    multi = len(adt.variants) > 1
    outer_arms: list[ast.MatchArm] = []
    for variant in adt.variants:
        n = len(variant.payload)
        x_binds: list[ast.Pattern] = [ast.BindPattern(f"x{i}", sp) for i in range(n)]
        y_binds: list[ast.Pattern] = [ast.BindPattern(f"y{i}", sp) for i in range(n)]
        cmps = [
            _eq_expr(_name(f"x{i}", sp), _name(f"y{i}", sp), pty, sp, exp, gen)
            for i, pty in enumerate(variant.payload)
        ]
        inner_arms = [
            ast.MatchArm(ast.CtorPattern(variant.name, y_binds, sp), _and_all(cmps, sp), sp)
        ]
        if multi:
            inner_arms.append(ast.MatchArm(ast.WildcardPattern(sp), ast.BoolLit(False, sp), sp))
        inner = ast.MatchExpr(_name("other", sp), inner_arms, sp)
        outer_arms.append(ast.MatchArm(ast.CtorPattern(variant.name, x_binds, sp), inner, sp))
    match = ast.MatchExpr(_name("self", sp), outer_arms, sp)
    self_te = _ty(adt.name, sp)
    return _expr_fn("eq", [("self", self_te), ("other", self_te)], "Bool", match, sp)


# --- Show ---------------------------------------------------------------------


def _derive_show(type_name: str, sp: Span, exp: Expander, gen: _HelperGen) -> ast.FnDecl:
    record = exp.ctx.records.get(type_name)
    if record is not None:
        return _show_record(record, sp, exp, gen)
    adt = exp.ctx.adts.get(type_name)
    if adt is not None:
        return _show_adt(adt, sp, exp, gen)
    raise _err("DER001", f"cannot derive Show for {type_name!r}", sp)


def _show_record(record: ast.RecordDecl, sp: Span, exp: Expander, gen: _HelperGen) -> ast.FnDecl:
    parts: list[ast.Expr] = [ast.StringLit(record.name + " { ", sp)]
    for i, fld in enumerate(record.fields):
        prefix = ("" if i == 0 else ", ") + fld.name + " = "
        access = ast.MemberExpr(_name("self", sp), fld.name, sp)
        parts.append(ast.StringLit(prefix, sp))
        parts.append(_show_expr(access, fld.type, sp, exp, gen))
    parts.append(ast.StringLit(" }", sp))
    return _expr_fn("show", [("self", _ty(record.name, sp))], "String", _concat(parts, sp), sp)


def _show_adt(adt: ast.AdtDecl, sp: Span, exp: Expander, gen: _HelperGen) -> ast.FnDecl:
    arms: list[ast.MatchArm] = []
    for variant in adt.variants:
        if variant.payload:
            binds: list[ast.Pattern] = [
                ast.BindPattern(f"x{i}", sp) for i in range(len(variant.payload))
            ]
            bind = ast.CtorPattern(variant.name, binds, sp)
            parts: list[ast.Expr] = [ast.StringLit(variant.name + "(", sp)]
            for i, pty in enumerate(variant.payload):
                if i:
                    parts.append(ast.StringLit(", ", sp))
                parts.append(_show_expr(_name(f"x{i}", sp), pty, sp, exp, gen))
            parts.append(ast.StringLit(")", sp))
            body = _concat(parts, sp)
        else:
            bind = ast.CtorPattern(variant.name, [], sp)
            body = ast.StringLit(variant.name, sp)
        arms.append(ast.MatchArm(bind, body, sp))
    match = ast.MatchExpr(_name("self", sp), arms, sp)
    return _expr_fn("show", [("self", _ty(adt.name, sp))], "String", match, sp)


def _err(code: str, message: str, span: Span) -> FlexError:
    return FlexError([Diagnostic(code, message, span)])
