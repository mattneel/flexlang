"""Monomorphization of bounded generic functions.

The checker leaves generic functions as templates (kept out of the callable
table) and records, at every call site, which concrete instantiation is needed.
This pass turns each demanded instantiation into an ordinary concrete function
named `fn$Key...` (via `spec_symbol`) and re-runs the checker until no new
instantiations appear. Specialized functions flow through the normal checker and
backend with no further special-casing.

A specialization is a clone of the template with every `TypeExpr` rewritten under
the call site's substitution (so `T` becomes `Point`, `List<T>` becomes
`List<Point>`, ...) and fresh AST node identities so each copy gets its own
`expr_types`. Call sites reach their specialization through `method_targets`,
exactly like trait method calls, so the backend needs zero changes.
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from typing import Any, cast

from flx.diagnostics import Diagnostic, FlexError
from flx.sema.check import CheckResult, check, spec_symbol
from flx.syntax import ast
from flx.types import AdtType, PrimType, RecordType, Type

# A generous ceiling: real programs need a handful of specializations. A runaway
# count means polymorphic recursion (a generic calling itself at an ever-growing
# type), which monomorphization cannot resolve.
_MONO_LIMIT = 256


def check_and_monomorphize(
    module: ast.Module,
    decl_module: dict[str, str] | None = None,
    public: set[str] | None = None,
) -> CheckResult:
    """Type-check `module`, then specialize every generic instantiation it
    demands, re-checking until the set of instantiations is closed."""
    result = check(module, decl_module, public)
    if not result.generic_fns:
        return result  # nothing generic: the first check is already complete

    current = module
    for _ in range(_MONO_LIMIT):
        result = check(current, decl_module, public)
        pending = [
            (name, key)
            for (name, key) in result.instantiations
            if spec_symbol(name, key) not in result.functions
        ]
        if not pending:
            return result
        if len(current.items) + len(pending) > _MONO_LIMIT:
            break
        specs = [
            _specialize(
                result.generic_fns[name], result.inst_subst[(name, key)], spec_symbol(name, key)
            )
            for (name, key) in pending
        ]
        current = replace(current, items=[*current.items, *specs])

    raise FlexError(
        [
            Diagnostic(
                "MONO001",
                "monomorphization did not converge (polymorphic recursion?)",
                None,
            )
        ]
    )


def _specialize(template: ast.FnDecl, subst: dict[str, Type], new_name: str) -> ast.FnDecl:
    subst_te = {name: _type_to_typeexpr(ty) for name, ty in subst.items()}
    clone = cast(ast.FnDecl, _rewrite(template, subst_te))
    return replace(clone, name=new_name, type_params=[])


def _type_to_typeexpr(ty: Type) -> ast.TypeExpr:
    if isinstance(ty, AdtType):
        return ast.TypeExpr(ty.name, [_type_to_typeexpr(a) for a in ty.type_args])
    if isinstance(ty, (PrimType, RecordType)):
        return ast.TypeExpr(ty.name)
    raise FlexError([Diagnostic("MONO002", f"cannot monomorphize a generic over type {ty}", None)])


def _subst_typeexpr(te: ast.TypeExpr, subst: dict[str, ast.TypeExpr]) -> ast.TypeExpr:
    if te.name in subst and not te.args:
        return subst[te.name]
    if te.args:
        return replace(te, args=[_subst_typeexpr(a, subst) for a in te.args])
    return te


def _rewrite(node: object, subst: dict[str, ast.TypeExpr]) -> object:
    """Deep-clone an AST fragment (fresh node identities) while substituting
    every `TypeExpr` it contains under `subst`."""
    if isinstance(node, ast.TypeExpr):
        return _subst_typeexpr(node, subst)
    if isinstance(node, list):
        return [_rewrite(x, subst) for x in node]
    if isinstance(node, tuple):
        return tuple(_rewrite(x, subst) for x in node)
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        changes = {f.name: _rewrite(getattr(node, f.name), subst) for f in dataclasses.fields(node)}
        return dataclasses.replace(cast(Any, node), **changes)
    return node
