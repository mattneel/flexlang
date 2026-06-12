"""Multi-file program loading.

`import A.B` resolves to ``<root>/A/B.flx`` (root = the entry file's directory),
loaded transitively. All reachable files are parsed and their top-level items
concatenated into a single :class:`~flx.syntax.ast.Module`, which then flows
through the existing expand -> check -> monomorphize -> backend pipeline unchanged.

Merging at the AST level *before* macro expansion means one expander (so gensyms
never collide across files) and cross-file macros/derives resolve for free.

Module identity is validated, not trusted: an imported file's ``module`` header
must equal its import path (MOD002), and no two files may declare the same module
name (MOD003) — so a file cannot inject definitions into another module. Each
definition's origin module (by name AND by file) and its visibility are recorded
in :class:`ProgramInfo` so the checker can enforce `pub`/private.
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
    file_module: dict[str, str] = field(default_factory=dict)  # file path -> module name
    module_spans: list[tuple[str, Span]] = field(default_factory=list)
    module_imports: dict[str, list[ast.ImportDecl]] = field(default_factory=dict)


def _decl_name(item: ast.Item) -> str | None:
    if isinstance(
        item,
        (ast.FnDecl, ast.ExternFnDecl, ast.RecordDecl, ast.AdtDecl, ast.TraitDecl, ast.MacroDecl),
    ):
        return item.name
    return None


def std_root() -> Path:
    """The standard library shipped inside the compiler package: a plain module
    tree (`Std/...`, written in Flex) that is always importable."""
    return Path(__file__).resolve().parent / "std"


def _contains(outer: Span, inner: Span | None) -> bool:
    return (
        inner is not None
        and outer.file == inner.file
        and outer.start.offset <= inner.start.offset
        and inner.end.offset <= outer.end.offset
    )


def _module_for_item(blocks: list[ast.ModuleBlock], item: ast.Item, fallback: str) -> str:
    for block in blocks:
        if _contains(block.span, item.span):
            return block.name
    return fallback


def load_program(entry_path: str, extra_roots: tuple[Path, ...] = ()) -> ProgramInfo:
    """Load the program rooted at `entry_path`. Imports resolve against the entry
    file's directory first, then each extra root (package dependency directories,
    in manifest order). A module found in more than one root is MOD004. The
    bundled standard library is the lowest-precedence fallback root: user code
    and dependencies can shadow `Std.*` deliberately, never ambiguously."""
    roots = [Path(entry_path).resolve().parent, *[Path(r).resolve() for r in extra_roots]]
    sources: dict[str, str] = {}
    order: list[ast.Module] = []
    seen: set[Path] = set()
    file_module: dict[str, str] = {}
    module_file: dict[str, str] = {}  # declared module name -> file (MOD003)
    module_spans: list[tuple[str, Span]] = []
    module_imports: dict[str, list[ast.ImportDecl]] = {}

    def defines_module(path: Path, module_name: str) -> bool:
        try:
            src = path.read_text(encoding="utf-8")
            mod = parse(src, str(path.resolve()))
        except OSError, UnicodeDecodeError, FlexError:
            return False
        return any(block.name == module_name for block in mod.blocks)

    def find_block_module(module_name: str) -> list[Path]:
        matches: list[Path] = []
        for root in roots:
            for path in sorted(root.rglob("*.flx")):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                if defines_module(path, module_name):
                    matches.append(path)
        return matches

    def visit(path: Path, expected_name: str | None, import_span: Span | None) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return  # already loaded (also breaks import cycles)
        try:
            src = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise FlexError(
                [Diagnostic("MOD001", f"{path} is not valid UTF-8", import_span)]
            ) from None
        except OSError:
            what = "cannot read file" if import_span is None else "cannot find imported module"
            raise FlexError([Diagnostic("MOD001", f"{what} {path}", import_span)]) from None
        seen.add(resolved)
        key = str(resolved)
        sources[key] = src
        mod = parse(src, key)
        order.append(mod)
        block_names = {block.name for block in mod.blocks}
        if expected_name is not None and expected_name not in block_names:
            raise FlexError(
                [
                    Diagnostic(
                        "MOD002",
                        f"{path} does not define imported module {expected_name!r}",
                        import_span,
                    )
                ]
            )
        for block in mod.blocks:
            other = module_file.get(block.name)
            if other is not None:
                raise FlexError(
                    [
                        Diagnostic(
                            "MOD003",
                            f"module {block.name!r} is declared by both {other} and {key}",
                            import_span,
                        )
                    ]
                )
            module_file[block.name] = key
            module_spans.append((block.name, block.span))
        file_module[key] = mod.name
        in_std = std_root() in resolved.parents
        for block in mod.blocks:
            decls = block.import_decls or [
                ast.ImportDecl(imp, span)
                for imp, span in zip(
                    block.imports,
                    list(block.import_spans) + [block.span] * len(block.imports),
                    strict=False,
                )
            ]
            module_imports.setdefault(block.name, []).extend(decls)
            for decl in decls:
                imp = decl.module
                span = decl.span
                if imp in module_file:
                    continue
                rel = Path(*imp.split(".")).with_suffix(".flx")
                if in_std and (std_root() / rel).is_file():
                    # The stdlib's own dependency graph is pinned: a user/dep file at
                    # Std/X.flx shadows what the USER imports, never what the bundled
                    # std modules import from each other.
                    visit(std_root() / rel, imp, span)
                    continue
                found = [r / rel for r in roots if (r / rel).is_file()]
                if not found:
                    found = find_block_module(imp)
                if len(found) > 1:
                    listing = " and ".join(str(c) for c in found)
                    raise FlexError(
                        [Diagnostic("MOD004", f"import {imp!r} is ambiguous: {listing}", span)]
                    )
                if not found and (std_root() / rel).is_file():
                    found = [std_root() / rel]  # the stdlib is the fallback root
                if not found and imp.startswith("Std."):
                    raise FlexError(
                        [
                            Diagnostic(
                                "MOD001",
                                f"the standard library has no module {imp!r} (yet)",
                                span,
                            )
                        ]
                    )
                child = found[0] if found else roots[0] / rel
                visit(child, imp, span)

    visit(Path(entry_path), None, None)

    decl_module: dict[str, str] = {}
    public: set[str] = set()
    merged_items: list[ast.Item] = []
    for mod in order:
        for item in mod.items:
            merged_items.append(item)
            name = _decl_name(item)
            if name is not None:
                decl_module.setdefault(name, _module_for_item(mod.blocks, item, mod.name))
                if getattr(item, "pub", False):
                    public.add(name)

    merged = replace(order[0], imports=[], items=merged_items, import_spans=[], import_decls=[])
    return ProgramInfo(
        merged,
        sources,
        decl_module,
        public,
        file_module,
        module_spans,
        module_imports,
    )
