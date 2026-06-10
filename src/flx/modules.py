"""Multi-file program loading.

`import A.B` resolves to ``<root>/A/B.flx`` (root = the entry file's directory),
loaded transitively. All reachable files are parsed and their top-level items
concatenated into a single :class:`~flx.syntax.ast.Module`, which then flows
through the existing expand -> check -> monomorphize -> backend pipeline unchanged.

Merging at the AST level *before* macro expansion means one expander (so gensyms
never collide across files) and cross-file macros/derives resolve for free. Each
definition's origin module and visibility are recorded in :class:`ProgramInfo` so
the checker can enforce `pub`/private without a separate name-resolution pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from flx.diagnostics import Diagnostic, FlexError, Span
from flx.syntax import ast
from flx.syntax.parser import parse


@dataclass
class ProgramInfo:
    module: ast.Module  # the merged program
    sources: dict[str, str]  # file path -> source text, for diagnostics
    decl_module: dict[str, str] = field(default_factory=dict)  # top-level name -> module
    public: set[str] = field(default_factory=set)  # names declared `pub`


def _decl_name(item: ast.Item) -> str | None:
    if isinstance(item, (ast.FnDecl, ast.RecordDecl, ast.AdtDecl, ast.TraitDecl, ast.MacroDecl)):
        return item.name
    return None


def load_program(entry_path: str) -> ProgramInfo:
    root = Path(entry_path).resolve().parent
    sources: dict[str, str] = {}
    order: list[ast.Module] = []
    seen: set[Path] = set()

    def visit(path: Path, import_span: Span | None) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return  # already loaded (also breaks import cycles)
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            what = "cannot read file" if import_span is None else "cannot find imported module"
            raise FlexError([Diagnostic("MOD001", f"{what} {path}", import_span)]) from None
        seen.add(resolved)
        key = str(resolved)
        sources[key] = src
        mod = parse(src, key)
        order.append(mod)
        for imp in mod.imports:
            child = root / Path(*imp.split(".")).with_suffix(".flx")
            visit(child, mod.span)

    visit(Path(entry_path), None)

    decl_module: dict[str, str] = {}
    public: set[str] = set()
    merged_items: list[ast.Item] = []
    for mod in order:
        for item in mod.items:
            merged_items.append(item)
            name = _decl_name(item)
            if name is not None:
                decl_module.setdefault(name, mod.name)
                if getattr(item, "pub", False):
                    public.add(name)

    merged = replace(order[0], imports=[], items=merged_items)
    return ProgramInfo(merged, sources, decl_module, public)
