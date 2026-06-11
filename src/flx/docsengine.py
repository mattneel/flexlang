"""The `flx docs` engine: documentation as a checked compiler artifact.

Docs in Flex are first-class declarations (`doc ... { ... }`), not comments or
detached markdown. This module is the toolchain side of that bargain:

* ``flx docs check`` — proves the documentation. Every nested ``test`` in every
  doc declaration is synthesized into a real test program and executed (on the
  interpreter, and natively with ``--both``); every ``expect_error`` example is
  compiled and must fail with exactly the documented diagnostic (DOC003
  otherwise); every public stdlib symbol must carry a doc (DOC005).
* ``flx docs build`` — renders the doc declarations into the mdBook source
  (API pages, guide pages, diagnostics pages). ``--check`` verifies the
  committed output is current, so the site cannot drift from the compiler.
* ``flx docs explain CODE`` — renders the doc page for a diagnostic code in
  the terminal.

The static halves (a doc referencing a missing symbol is DOC001, a status that
contradicts reality is DOC004) run on EVERY compile, inside the checker.
"""

from __future__ import annotations

import difflib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from flx.diagnostics import FlexError
from flx.modules import std_root
from flx.syntax import ast
from flx.syntax.parser import parse


def docsrc_root() -> Path:
    """Compiler-level docs (guide pages, diagnostics) — doc declarations in
    Flex files bundled with the toolchain, exactly like the stdlib."""
    return Path(__file__).resolve().parent / "docsrc"


@dataclass
class DocUnit:
    module: str  # declaring module name
    file: Path
    decl: ast.DocDecl


def _std_files() -> list[Path]:
    return sorted((std_root() / "Std").glob("*.flx"))


def _docsrc_files() -> list[Path]:
    root = docsrc_root()
    return sorted(root.glob("*.flx")) if root.is_dir() else []


def _std_module_names() -> list[str]:
    return [f"Std.{p.stem}" for p in _std_files()]


def collect(files: list[Path] | None = None) -> list[DocUnit]:
    """Parse the given files (default: the bundled stdlib + docsrc) and gather
    their doc declarations."""
    units: list[DocUnit] = []
    for path in files if files is not None else [*_std_files(), *_docsrc_files()]:
        module = parse(path.read_text(encoding="utf-8"), str(path))
        for decl in module.docs:
            units.append(DocUnit(module.name, path, decl))
    return units


# --- checking -------------------------------------------------------------------


def _flx_str(value: str) -> str:
    """Render a string VALUE back into Flex string-literal syntax. Doc test
    names are parsed values; embedding them verbatim into a synthesized
    program would let a backslash or newline break the lexer."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _synth_test_program(
    harness: str, tests: list[tuple[str, ast.DocTest]], extra_import: str | None = None
) -> str:
    """One runnable program holding a module's doc tests, importing the whole
    stdlib so examples are written from the USER's perspective (plus the
    declaring module itself when it isn't part of the stdlib)."""
    lines = [f"module {harness}"]
    for name in _std_module_names():
        lines.append(f"import {name}")
    if extra_import is not None:
        lines.append(f"import {extra_import}")
    lines.append("")
    for anchor, dt in tests:
        uses = f" uses {{ {', '.join(dt.effects)} }}" if dt.effects else ""
        lines.append(f"test {_flx_str(f'{anchor}: {dt.name}')}{uses} {{")
        for src_line in dt.source.splitlines():
            lines.append(f"  {src_line}")
        lines.append("}")
        lines.append("")
    # No main: `flx test` only needs the test blocks, and a copied user module
    # may carry its own main.
    return "\n".join(lines) + "\n"


def _synth_error_program(dt: ast.DocTest) -> str:
    """An expect_error example is a whole program expected to FAIL."""
    lines = ["module DocErr"]
    for name in _std_module_names():
        lines.append(f"import {name}")
    lines.append("")
    lines.append(dt.source)
    return "\n".join(lines) + "\n"


def _anchor(decl: ast.DocDecl) -> str:
    return decl.target if decl.target is not None else (decl.title or "?")


def _doc_tests(
    units: list[DocUnit],
) -> dict[str, tuple[Path, list[tuple[str, ast.DocTest]]]]:
    """Runnable doc tests grouped by declaring module (skips expect_error)."""
    grouped: dict[str, tuple[Path, list[tuple[str, ast.DocTest]]]] = {}
    for unit in units:
        for entry in unit.decl.content:
            if isinstance(entry, ast.DocTest) and entry.expect_error is None:
                grouped.setdefault(unit.module, (unit.file, []))[1].append(
                    (_anchor(unit.decl), entry)
                )
    return grouped


def _copy_module_closure(file: Path, module_name: str, tmp: Path) -> set[str]:
    """Copy a non-stdlib module AND its non-Std transitive imports into `tmp`,
    mirroring the loader's root-relative layout (root = the file's directory
    minus the module path's depth), so the synthesized program resolves the
    same modules the user's own `flx test` would. Returns the copied names."""
    root = file.resolve().parent
    for _ in module_name.split(".")[:-1]:
        root = root.parent
    copied: set[str] = set()

    def visit(name: str, src: Path) -> None:
        if name in copied or not src.is_file():
            return  # a missing import is the compiler's MOD001 to report, not ours
        copied.add(name)
        text = src.read_text(encoding="utf-8")
        dest = tmp.joinpath(*name.split(".")).with_suffix(".flx")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        for imp in parse(text, str(src)).imports:
            if not imp.startswith("Std."):
                visit(imp, root.joinpath(*imp.split(".")).with_suffix(".flx"))

    visit(module_name, file)
    return copied


def _run_module_doc_tests(
    module_name: str, file: Path, tests: list[tuple[str, ast.DocTest]], native: bool
) -> int:
    """Synthesize and execute one module's doc examples. A non-stdlib module
    is copied (with its import closure) beside the synthesized program so its
    own helpers resolve."""
    from flx import driver

    failures = 0
    is_std = module_name.startswith("Std.")
    extra = None if is_std else module_name
    with tempfile.TemporaryDirectory() as tmp:
        copied: set[str] = set()
        if extra is not None:
            copied = _copy_module_closure(file, module_name, Path(tmp))
        harness = "DocRun"
        while harness in copied:  # a user module may be named DocRun itself
            harness += "_"
        source = _synth_test_program(harness, tests, extra)
        path = str(Path(tmp) / "doc_run.flx")
        Path(path).write_text(source, encoding="utf-8")
        print(f"== doc tests: {module_name} ({len(tests)} examples)")
        backends = [("interpreter", dict(interpret=True))]
        if native:
            backends.append(("native", dict(native=True)))
        for label, kwargs in backends:
            code = driver.cmd_test(path, **kwargs)  # type: ignore[arg-type]
            if code != 0:
                print(
                    f"error[DOC002]: a doc example in {module_name} failed ({label})",
                    file=sys.stderr,
                )
                failures += 1
    return failures


def _error_tests(units: list[DocUnit]) -> list[tuple[str, ast.DocTest]]:
    out = []
    for unit in units:
        for entry in unit.decl.content:
            if isinstance(entry, ast.DocTest) and entry.expect_error is not None:
                out.append((_anchor(unit.decl), entry))
    return out


def _documented_targets(units: list[DocUnit]) -> set[str]:
    return {
        unit.decl.target.rsplit(".", 1)[-1]
        for unit in units
        if unit.decl.target is not None and unit.decl.target != "module"
    }


def cmd_docs_check(native: bool = False) -> int:
    """Prove the documentation: DOC002 (an example fails), DOC003 (an expected
    diagnostic doesn't happen), DOC005 (an undocumented public stdlib symbol)."""
    failures = 0
    units = collect()

    # DOC005: every public stdlib symbol carries a doc declaration.
    documented = _documented_targets(units)
    for path in _std_files():
        module = parse(path.read_text(encoding="utf-8"), str(path))
        for item in module.items:
            if (
                isinstance(item, (ast.FnDecl, ast.ExternFnDecl))
                and item.pub
                and item.name not in documented
            ):
                print(
                    f"error[DOC005]: public symbol {module.name}.{item.name} "
                    "has no doc declaration",
                    file=sys.stderr,
                )
                failures += 1

    # DOC002: every runnable doc example passes, as a real test program.
    for module_name, (file, tests) in sorted(_doc_tests(units).items()):
        failures += _run_module_doc_tests(module_name, file, tests, native)

    # DOC003: every documented diagnostic example fails with exactly that code.
    failures += _check_error_examples(_error_tests(units))

    if failures:
        print(f"\ndocs check failed: {failures} problem(s)", file=sys.stderr)
        return 1
    print("\ndocs check: all examples proven")
    return 0


def _check_error_examples(error_tests: list[tuple[str, ast.DocTest]]) -> int:
    """DOC003: each expect_error example must fail with EXACTLY the documented
    code — an example that smuggles unrelated errors proves nothing. Returns
    the failure count. Compile-fail checking is backend-independent."""
    from flx import driver

    failures = 0
    for anchor, dt in error_tests:
        source = _synth_error_program(dt)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc_err.flx"
            path.write_text(source, encoding="utf-8")
            result, _sources = driver._frontend(str(path))
            if isinstance(result, FlexError):
                codes = {d.code for d in result.diagnostics}
                if codes == {dt.expect_error}:
                    print(f"ok doc error example {anchor!r}: {dt.expect_error}")
                    continue
                print(
                    f"error[DOC003]: {anchor!r} expected exactly {dt.expect_error} "
                    f"but got {', '.join(sorted(codes))}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"error[DOC003]: {anchor!r} expected {dt.expect_error} but the "
                    "example compiled cleanly",
                    file=sys.stderr,
                )
            failures += 1
    return failures


def run_file_docs(path: str, native: bool = False) -> int:
    """`flx test --docs`: run the doc examples declared in a user file (or in
    every .flx file under a directory) — nested tests execute, expect_error
    examples must fail with exactly their documented code."""
    target = Path(path)
    files = sorted(target.rglob("*.flx")) if target.is_dir() else [target]
    try:
        units = collect(files)
    except FlexError as err:
        for diag in err.diagnostics:
            print(f"error[{diag.code}]: {diag.message}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"flx test --docs: {exc}", file=sys.stderr)
        return 1
    grouped = _doc_tests(units)
    error_tests = _error_tests(units)
    if not grouped and not error_tests:
        print("no doc tests found")
        return 0
    code = 0
    for module_name, (file, tests) in sorted(grouped.items()):
        rc = _run_module_doc_tests(module_name, file, tests, native)
        code = code or rc
    if _check_error_examples(error_tests):
        code = code or 1
    return code


# --- rendering ------------------------------------------------------------------

_API_START = "<!-- flx-api:start (generated by `flx docs build`; do not edit) -->"
_API_END = "<!-- flx-api:end -->"
# Stamped into every generated page, so orphans (pages whose doc declaration
# was removed or renamed) are recognizable and never linger as stale truth.
_GENERATED_MARK = "<!-- generated by `flx docs build`; do not edit -->"


def _type_str(t: ast.TypeExpr) -> str:
    if t.name == "->":
        params = ", ".join(_type_str(a) for a in t.args[:-1])
        return f"({params}) -> {_type_str(t.args[-1])}"
    if t.args:
        return f"{t.name}<{', '.join(_type_str(a) for a in t.args)}>"
    return t.name


def _signature(item: ast.FnDecl | ast.ExternFnDecl) -> str:
    params = ", ".join(f"{p.name}: {_type_str(p.type)}" for p in item.params)
    ret = _type_str(item.return_type) if item.return_type else "Unit"
    uses = f" uses {{ {', '.join(item.effects)} }}" if item.effects else ""
    tps = ""
    if isinstance(item, ast.FnDecl) and item.type_params:
        tps = "<" + ", ".join(tp.name for tp in item.type_params) + ">"
    kind = "extern fn" if isinstance(item, ast.ExternFnDecl) else "fn"
    return f"{kind} {item.name}{tps}({params}) -> {ret}{uses}"


def _render_doc_body(decl: ast.DocDecl, out: list[str]) -> None:
    if decl.summary:
        out.append(decl.summary)
        out.append("")
    for entry in decl.content:
        if isinstance(entry, ast.DocText):
            out.append(entry.text)
            out.append("")
        elif isinstance(entry, ast.DocTest):
            if entry.expect_error is not None:
                out.append(
                    f"**Example: {entry.name}** — expected to fail with "
                    f"`{entry.expect_error}` (proven by `flx docs check`):"
                )
            else:
                out.append(f"**Example: {entry.name}** — ✓ checked by `flx docs check`:")
            out.append("")
            out.append("```flx")
            out.append(entry.source)
            out.append("```")
            out.append("")
        elif isinstance(entry, ast.DocSnippet):
            out.append(f"**Sketch: {entry.name}** — illustration only, not checked:")
            out.append("")
            out.append("```flx")
            out.append(entry.source)
            out.append("```")
            out.append("")
    meta = []
    if decl.since:
        meta.append(f"since {decl.since}")
    if decl.status:
        meta.append(f"status: {decl.status}")
    if meta:
        out.append(f"*{' · '.join(meta)}*")
        out.append("")
    if decl.sees:
        out.append("See also: " + ", ".join(f"`{s}`" for s in decl.sees))
        out.append("")


def render_api_pages() -> dict[str, str]:
    """{relative docs path: content} for every generated page."""
    pages: dict[str, str] = {}
    for path in _std_files():
        module = parse(path.read_text(encoding="utf-8"), str(path))
        docs_by_target: dict[str | None, ast.DocDecl] = {}
        for d in module.docs:
            if d.target is not None and d.target != "module":
                docs_by_target[d.target.rsplit(".", 1)[-1]] = d
            else:
                docs_by_target[d.target] = d
        out: list[str] = [f"# {module.name}", "", _GENERATED_MARK, ""]
        out.append(
            "*Generated from the `doc` declarations in "
            f"`{Path(path).name}` by `flx docs build`. Examples are executed by "
            "`flx docs check`.*"
        )
        out.append("")
        mod_doc = docs_by_target.get("module")
        if mod_doc is not None:
            _render_doc_body(mod_doc, out)
        for item in module.items:
            if not isinstance(item, (ast.FnDecl, ast.ExternFnDecl)) or not item.pub:
                continue
            out.append(f"## {item.name}")
            out.append("")
            out.append("```flx")
            out.append(_signature(item))
            out.append("```")
            out.append("")
            doc = docs_by_target.get(item.name)
            if doc is not None:
                _render_doc_body(doc, out)
        pages[f"api/{module.name}.md"] = "\n".join(out).rstrip() + "\n"

    # Free-standing docsrc pages render under their slug.
    for path in _docsrc_files():
        module = parse(path.read_text(encoding="utf-8"), str(path))
        for d in module.docs:
            if d.target is not None or d.title is None:
                continue
            slug = d.slug or d.title.lower().replace(" ", "-")
            out = [f"# {d.title}", "", _GENERATED_MARK, ""]
            _render_doc_body(d, out)
            pages[f"{slug}.md"] = "\n".join(out).rstrip() + "\n"
    return pages


def _summary_section(pages: dict[str, str]) -> list[str]:
    lines = [_API_START]
    reference = sorted(p for p in pages if p.startswith("reference/"))
    for rel in reference:
        title = Path(rel).stem.replace("-", " ").title()
        lines.append(f"- [{title}]({rel})")
    lines.append("- [API Reference](api/index.md)")
    for rel in sorted(p for p in pages if p.startswith("api/") and p != "api/index.md"):
        lines.append(f"  - [{Path(rel).stem}]({rel})")
    diags = sorted(p for p in pages if p.startswith("diagnostics/") and p != "diagnostics/index.md")
    if diags:
        lines.append("- [Diagnostics](diagnostics/index.md)")
        for rel in diags:
            lines.append(f"  - [{Path(rel).stem}]({rel})")
    lines.append(_API_END)
    return lines


def cmd_docs_build(check_only: bool = False, docs_dir: Path | None = None) -> int:
    """Render doc declarations into the book source. With --check, verify the
    committed pages are current (the CI gate against drift)."""
    docs = docs_dir if docs_dir is not None else Path("docs")
    if not docs.is_dir():
        print(f"flx docs: no {docs}/ directory here", file=sys.stderr)
        return 1
    # Ownership check FIRST: this command rewrites pages and SUMMARY.md, so it
    # must never touch a docs/ tree that isn't set up for it (a user project's
    # book is not ours to scribble in).
    summary_path = docs / "SUMMARY.md"
    if not summary_path.is_file():
        print(f"flx docs: {summary_path} does not exist", file=sys.stderr)
        return 1
    summary = summary_path.read_text(encoding="utf-8")
    if _API_START not in summary or _API_END not in summary:
        print(
            f"flx docs: {summary_path} has no flx-api markers; refusing to "
            "modify a docs tree not managed by `flx docs build`",
            file=sys.stderr,
        )
        print(
            f"help: to opt in, add `{_API_START}` and `{_API_END}` where the "
            "generated section belongs",
            file=sys.stderr,
        )
        return 1

    pages = render_api_pages()
    index = ["# API Reference", "", _GENERATED_MARK, ""]
    index.append("*Generated from `doc` declarations by `flx docs build`.*")
    index.append("")
    for rel in sorted(p for p in pages if p.startswith("api/")):
        index.append(f"- [{Path(rel).stem}]({Path(rel).name})")
    pages["api/index.md"] = "\n".join(index) + "\n"
    diags = sorted(p for p in pages if p.startswith("diagnostics/"))
    if diags:
        dindex = ["# Diagnostics", "", _GENERATED_MARK, ""]
        dindex.append(
            "*Each page is proven by an `expect_error` example that `flx docs check` "
            "compiles and requires to fail with exactly this code. "
            "`flx docs explain <CODE>` shows these in the terminal.*"
        )
        dindex.append("")
        for rel in diags:
            if rel != "diagnostics/index.md":
                dindex.append(f"- [{Path(rel).stem}]({Path(rel).name})")
        pages["diagnostics/index.md"] = "\n".join(dindex) + "\n"

    section = "\n".join(_summary_section(pages))
    head, rest = summary.split(_API_START, 1)
    _, tail = rest.split(_API_END, 1)
    new_summary = head + section + tail

    stale: list[str] = []
    for rel, content in {**pages, "SUMMARY.md": new_summary}.items():
        target = docs / rel
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
        if current != content:
            stale.append(rel)
            if check_only:
                diff = difflib.unified_diff(
                    current.splitlines(), content.splitlines(), rel, rel, lineterm=""
                )
                print("\n".join(list(diff)[:40]), file=sys.stderr)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
    # Orphans: previously generated pages whose doc declaration is gone. They
    # carry the generated mark but are no longer in the render set — stale
    # truth the diff above can't see.
    for md in sorted(docs.rglob("*.md")):
        rel = md.relative_to(docs).as_posix()
        if rel in pages or rel == "SUMMARY.md":
            continue
        if _GENERATED_MARK in md.read_text(encoding="utf-8"):
            stale.append(rel)
            if check_only:
                print(
                    f"{rel}: generated page with no current doc declaration (orphan)",
                    file=sys.stderr,
                )
            else:
                md.unlink()
    if check_only and stale:
        print(
            f"error[DOCS001]: generated docs are stale ({', '.join(stale)}); "
            "run `flx docs build` and commit",
            file=sys.stderr,
        )
        return 1
    if not check_only:
        print(f"docs build: {len(pages)} generated page(s) current")
        if _mdbook_available():
            return subprocess.run(["mdbook", "build"]).returncode
    return 0


def _mdbook_available() -> bool:
    import shutil

    return shutil.which("mdbook") is not None


def cmd_docs_explain(code: str) -> int:
    """Render the doc page for a diagnostic code in the terminal. Codes match
    exactly (case-insensitively) — `001` is not a code, and a guide page's
    slug must not satisfy a diagnostic lookup."""
    want = code.strip().upper()
    if want:
        for unit in collect():
            d = unit.decl
            slug = d.slug or ""
            slug_match = slug.startswith("diagnostics/") and slug.split("/")[-1].upper() == want
            if (d.title or "").upper() == want or slug_match:
                print(f"# {d.title}")
                out: list[str] = []
                _render_doc_body(d, out)
                print("\n".join(out))
                return 0
    print(f"flx docs: no documentation for {code!r} (yet)", file=sys.stderr)
    return 1
