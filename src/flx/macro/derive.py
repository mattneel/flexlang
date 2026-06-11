"""`derive(Eq)` / `derive(Show)` code generation.

Generates real `FnDecl`s (field-by-field for records, match-based for ADTs)
that flow through the normal checker/backend. They appear in `flx expand`.
Generic types and unsupported field types are reported, not mis-generated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flx.diagnostics import Diagnostic, FlexError, Span
from flx.syntax import ast

if TYPE_CHECKING:
    from flx.macro.expand import Expander

_SUPPORTED = {"Eq", "Show"}


def run_derives(module: ast.Module, exp: Expander) -> list[ast.Item]:
    out: list[ast.Item] = []
    for record in module.records:
        out.extend(_derive_type(record.name, record.type_params, record.span, record.derives, exp))
    for adt in module.adts:
        out.extend(_derive_type(adt.name, adt.type_params, adt.span, adt.derives, exp))
    return out


def _derive_type(
    name: str, type_params: list[str], span: Span, derives: list[str], exp: Expander
) -> list[ast.ImplDecl]:
    out: list[ast.ImplDecl] = []
    for trait in derives:
        if trait not in _SUPPORTED:
            raise _err("DER001", f"cannot derive {trait!r} (only Eq, Show)", span)
        if type_params:
            raise _err("DER004", f"cannot derive {trait!r} on a generic type yet", span)
        method = _derive_eq(name, span, exp) if trait == "Eq" else _derive_show(name, span, exp)
        out.append(ast.ImplDecl(trait, name, [method], span))
    return out


def _string_field_names(name: str, exp: Expander) -> bool:
    record = exp.ctx.records.get(name)
    if record is not None:
        return any(f.type.name == "String" for f in record.fields)
    adt = exp.ctx.adts.get(name)
    if adt is not None:
        return any(p.name == "String" for v in adt.variants for p in v.payload)
    return False


# --- AST builders -------------------------------------------------------------


def _name(n: str, sp: Span) -> ast.NameExpr:
    return ast.NameExpr(n, sp)


def _ty(n: str, sp: Span) -> ast.TypeExpr:
    return ast.TypeExpr(n, [], sp)


def _fn(name: str, params: list[tuple[str, str]], ret: str, body: ast.Expr, sp: Span) -> ast.FnDecl:
    ps = [ast.Param(p, _ty(t, sp), sp) for p, t in params]
    return ast.FnDecl(name, ps, _ty(ret, sp), [], ast.Block([ast.ExprStmt(body, sp)], sp), sp)


def _concat(parts: list[ast.Expr], sp: Span) -> ast.Expr:
    result = parts[0]
    for part in parts[1:]:
        result = ast.BinaryExpr("++", result, part, sp)
    return result


# --- Eq -----------------------------------------------------------------------


def _derive_eq(type_name: str, sp: Span, exp: Expander) -> ast.FnDecl:
    record = exp.ctx.records.get(type_name)
    if record is not None and any(f.type.name == "String" for f in record.fields):
        # Field-wise comparison: `==` for structural fields, the Eq trait for
        # String fields — so `impl Eq for String` (import Std.Str) must be in
        # scope, or the use site reports DISP001.
        comparisons: list[ast.Expr] = []
        for fld in record.fields:
            mine: ast.Expr = ast.MemberExpr(_name("self", sp), fld.name, sp)
            theirs: ast.Expr = ast.MemberExpr(_name("other", sp), fld.name, sp)
            if fld.type.name == "String":
                comparisons.append(ast.CallExpr(ast.MemberExpr(mine, "eq", sp), [theirs], sp))
            else:
                comparisons.append(ast.BinaryExpr("==", mine, theirs, sp))
        body: ast.Expr = comparisons[0] if comparisons else ast.BoolLit(True, sp)
        for nxt in comparisons[1:]:
            body = ast.BinaryExpr("&&", body, nxt, sp)
        return _fn("eq", [("self", type_name), ("other", type_name)], "Bool", body, sp)
    if _string_field_names(type_name, exp):
        raise _err("DER001", f"cannot derive Eq for {type_name!r}: a variant carries a String", sp)
    # Delegate to structural equality, which the backend already lowers.
    body = ast.BinaryExpr("==", _name("self", sp), _name("other", sp), sp)
    return _fn("eq", [("self", type_name), ("other", type_name)], "Bool", body, sp)


# --- Show ---------------------------------------------------------------------


def _render(value: ast.Expr, type_name: str, sp: Span) -> ast.Expr:
    if type_name == "I64":
        return ast.CallExpr(_name("to_str", sp), [value], sp)
    if type_name == "Bool":
        then = ast.Block([ast.ExprStmt(ast.StringLit("true", sp), sp)], sp)
        els = ast.Block([ast.ExprStmt(ast.StringLit("false", sp), sp)], sp)
        return ast.IfExpr(value, then, els, sp)
    if type_name == "String":
        return value
    return ast.StringLit("<?>", sp)


def _derive_show(type_name: str, sp: Span, exp: Expander) -> ast.FnDecl:
    record = exp.ctx.records.get(type_name)
    if record is not None:
        return _show_record(record, sp)
    adt = exp.ctx.adts.get(type_name)
    if adt is not None:
        return _show_adt(adt, sp)
    raise _err("DER001", f"cannot derive Show for {type_name!r}", sp)


def _show_record(record: ast.RecordDecl, sp: Span) -> ast.FnDecl:
    parts: list[ast.Expr] = [ast.StringLit(record.name + " { ", sp)]
    for i, fld in enumerate(record.fields):
        prefix = ("" if i == 0 else ", ") + fld.name + " = "
        access = ast.MemberExpr(_name("self", sp), fld.name, sp)
        parts.append(ast.StringLit(prefix, sp))
        parts.append(_render(access, fld.type.name, sp))
    parts.append(ast.StringLit(" }", sp))
    return _fn("show", [("self", record.name)], "String", _concat(parts, sp), sp)


def _show_adt(adt: ast.AdtDecl, sp: Span) -> ast.FnDecl:
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
                parts.append(_render(_name(f"x{i}", sp), pty.name, sp))
            parts.append(ast.StringLit(")", sp))
            body = _concat(parts, sp)
        else:
            bind = ast.CtorPattern(variant.name, [], sp)
            body = ast.StringLit(variant.name, sp)
        arms.append(ast.MatchArm(bind, body, sp))
    match = ast.MatchExpr(_name("self", sp), arms, sp)
    return _fn("show", [("self", adt.name)], "String", match, sp)


def _err(code: str, message: str, span: Span) -> FlexError:
    return FlexError([Diagnostic(code, message, span)])
