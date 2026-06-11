"""M5 adversarial-review findings, pinned: DOC001/DOC004 resolution holes,
doc-test synthesis escaping, DOC003 exactness, user-file expect_error
verification, docs build ownership/orphans, explain strictness, the SIGPIPE
contract on every entry point, and the triple-string leading-line rule."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flx import docsengine, driver
from flx.diagnostics import FlexError
from flx.macro import expand
from flx.sema.specialize import check_and_monomorphize
from flx.syntax.lexer import tokenize
from flx.syntax.parser import parse

REPO = Path(__file__).resolve().parent.parent


def _diag(src: str) -> list:
    with pytest.raises(FlexError) as exc:
        check_and_monomorphize(expand(parse(src)))
    return exc.value.diagnostics


def _check_ok(src: str) -> None:
    check_and_monomorphize(expand(parse(src)))


def _flx(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "flx", *args], capture_output=True, text=True, cwd=cwd
    )


# --- DOC001/DOC004 resolution -----------------------------------------------------


def test_macro_can_be_documented(tmp_path: Path) -> None:
    src = (
        "macro square(x) = quote { unquote(x) * unquote(x) }\n"
        'doc square { summary "squares at compile time"\n'
        '  test "squares" { assert_eq(square(5), 25) } }\n'
        "fn main() -> I64 = { square(4) }\n"
    )
    path = tmp_path / "mac.flx"
    path.write_text(src, encoding="utf-8")
    assert driver.cmd_run(str(path), interpret=True) == 16


def test_macro_not_yet_is_doc004() -> None:
    diags = _diag(
        "macro square(x) = quote { unquote(x) }\n"
        "doc square { status not_yet }\nfn main() -> I64 = { 0 }\n"
    )
    assert any(d.code == "DOC004" for d in diags)


def test_doc001_bogus_dotted_target() -> None:
    diags = _diag(
        'doc Totally.Bogus.Path.helper { summary "x" }\n'
        "fn helper() -> I64 = { 1 }\nfn main() -> I64 = { 0 }\n"
    )
    assert any(d.code == "DOC001" for d in diags)


def test_doc001_bogus_see_qualifier() -> None:
    diags = _diag("doc main { see Bogus.main }\nfn main() -> I64 = { 0 }\n")
    assert any(d.code == "DOC001" for d in diags)


def test_doc001_wrong_std_module() -> None:
    # `split` exists — in Std.Str, not Std.IO. The qualifier must be validated.
    diags = _diag("doc main { see Std.IO.split }\nfn main() -> I64 = { 0 }\n")
    assert any(d.code == "DOC001" for d in diags)


def test_doc_refs_resolve_when_real() -> None:
    _check_ok(
        "module Main\n"
        "doc main { see Std.Str.split  see Main.helper  see to_str }\n"
        "fn helper() -> I64 = { 1 }\nfn main() -> I64 = { 0 }\n"
    )


def test_doc004_trait_and_ctor() -> None:
    diags = _diag(
        "trait Greet = {\n  fn hello(x: I64) -> I64\n}\n"
        "doc Greet { status not_yet }\n"
        "doc Some { status not_yet }\n"
        "fn main() -> I64 = { 0 }\n"
    )
    assert sum(1 for d in diags if d.code == "DOC004") == 2


# --- the lexer rule ----------------------------------------------------------------


def test_triple_string_trailing_whitespace_first_line() -> None:
    # An invisible trailing space after the opening quotes must not inject "\n".
    tokens = tokenize('let s = """ \n  hello\n  """')
    strings = [t for t in tokens if t.kind.name == "STRING"]
    assert strings[0].text == "hello"


# --- synthesis soundness -------------------------------------------------------------


def test_doc_test_names_with_escapes_run(tmp_path: Path) -> None:
    src = (
        "module Name1\n"
        "pub fn helper() -> I64 = { 1 }\n"
        "doc helper {\n"
        '  test "path C:\\\\temp\\\\file" { assert_eq(helper(), 1) }\n'
        '  test "two\\nlines and a \\"quote\\" and a\\ttab" { assert_eq(1, 1) }\n'
        "}\n"
        "fn main() -> I64 = { 0 }\n"
    )
    path = tmp_path / "name1.flx"
    path.write_text(src, encoding="utf-8")
    proc = _flx(["test", "--docs", str(path)])
    assert proc.returncode == 0, proc.stderr
    assert "path C:\\temp\\file" in proc.stdout  # the label round-trips exactly


def test_doc_tests_follow_transitive_imports(tmp_path: Path) -> None:
    (tmp_path / "Helper.flx").write_text(
        "module Helper\npub fn add_one(n: I64) -> I64 = { n + 1 }\n", encoding="utf-8"
    )
    (tmp_path / "App.flx").write_text(
        "module App\nimport Helper\n"
        "pub fn double_plus(n: I64) -> I64 = { add_one(n * 2) }\n"
        "fn main() -> I64 = { 0 }\n"
        'doc double_plus { test "works" { assert_eq(double_plus(20), 41) } }\n',
        encoding="utf-8",
    )
    proc = _flx(["test", "--docs", str(tmp_path / "App.flx")])
    assert proc.returncode == 0, proc.stderr


def test_module_named_docrun(tmp_path: Path) -> None:
    # The harness module must not collide with a user module named DocRun.
    path = tmp_path / "DocRun.flx"
    path.write_text(
        "module DocRun\npub fn one() -> I64 = { 1 }\nfn main() -> I64 = { 0 }\n"
        'doc one { test "is one" { assert_eq(one(), 1) } }\n',
        encoding="utf-8",
    )
    proc = _flx(["test", "--docs", str(path)])
    assert proc.returncode == 0, proc.stderr


def test_docs_directory_never_tracebacks(tmp_path: Path) -> None:
    (tmp_path / "mydocs.flx").write_text(
        "module MyDocs\npub fn double(x: I64) -> I64 = { x * 2 }\n"
        'doc double { test "doubles" { assert_eq(double(21), 42) } }\n'
        "fn main() -> I64 = { 0 }\n",
        encoding="utf-8",
    )
    proc = _flx(["test", "--docs", str(tmp_path)])
    assert "Traceback" not in proc.stderr
    assert "doubles" in proc.stdout  # the directory's doc tests actually ran


# --- expect_error proof for user files ------------------------------------------------


def test_user_expect_error_wrong_code_fails(tmp_path: Path) -> None:
    path = tmp_path / "experr.flx"
    path.write_text(
        "module ExpErr\npub fn id(x: I64) -> I64 = { x }\n"
        "doc id {\n"
        '  test "wrong claim" expect_error TOTALLYWRONG999 {\n'
        "    fn main() -> I64 = { 0 }\n  }\n}\n"
        "fn main() -> I64 = { 0 }\n",
        encoding="utf-8",
    )
    proc = _flx(["test", "--docs", str(path)])
    assert proc.returncode == 1
    assert "DOC003" in proc.stderr
    assert "no doc tests found" not in proc.stdout


def test_user_expect_error_right_code_passes(tmp_path: Path) -> None:
    path = tmp_path / "experr2.flx"
    path.write_text(
        "module ExpErr2\npub fn id(x: I64) -> I64 = { x }\n"
        "doc id {\n"
        '  test "true claim" expect_error EFFECT001 {\n'
        '    fn bad() -> Unit = { Log.info("x") }\n'
        "    fn main() -> I64 = { 0 }\n  }\n}\n"
        "fn main() -> I64 = { 0 }\n",
        encoding="utf-8",
    )
    proc = _flx(["test", "--docs", str(path)])
    assert proc.returncode == 0, proc.stderr


def test_doc003_requires_exactly_the_code(tmp_path: Path) -> None:
    # An example that fails with the documented code AND an unrelated error
    # proves nothing; the gate demands exactly the documented diagnostic.
    path = tmp_path / "smuggle.flx"
    path.write_text(
        "module Smuggle\npub fn id(x: I64) -> I64 = { x }\n"
        "doc id {\n"
        '  test "smuggled" expect_error EFFECT001 {\n'
        "    fn bad() -> Unit = { Log.info(totally_undefined_name) }\n"
        "    fn main() -> I64 = { 0 }\n  }\n}\n"
        "fn main() -> I64 = { 0 }\n",
        encoding="utf-8",
    )
    proc = _flx(["test", "--docs", str(path)])
    assert proc.returncode == 1
    assert "expected exactly" in proc.stderr


# --- docs build: ownership + orphans ---------------------------------------------------


def test_docs_build_refuses_foreign_tree(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "SUMMARY.md").write_text("# Summary\n\n- [Intro](intro.md)\n", encoding="utf-8")
    (docs / "intro.md").write_text("hello\n", encoding="utf-8")
    assert docsengine.cmd_docs_build(check_only=False, docs_dir=docs) == 1
    assert sorted(p.name for p in docs.iterdir()) == ["SUMMARY.md", "intro.md"]
    assert (docs / "intro.md").read_text(encoding="utf-8") == "hello\n"


def test_docs_build_missing_summary_clean_error(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    assert docsengine.cmd_docs_build(check_only=False, docs_dir=docs) == 1


def test_docs_check_flags_orphaned_generated_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    shutil.copytree(REPO / "docs", docs)
    orphan = docs / "diagnostics" / "ZZFAKE999.md"
    orphan.write_text(
        f"# ZZFAKE999\n\n{docsengine._GENERATED_MARK}\n\nstale truth\n", encoding="utf-8"
    )
    assert docsengine.cmd_docs_build(check_only=True, docs_dir=docs) == 1
    # ... and write mode deletes it.
    monkeypatch.setattr(docsengine, "_mdbook_available", lambda: False)
    assert docsengine.cmd_docs_build(check_only=False, docs_dir=docs) == 0
    assert not orphan.exists()


# --- explain strictness ------------------------------------------------------------------


def test_explain_case_insensitive_exact(capfd: pytest.CaptureFixture[str]) -> None:
    assert docsengine.cmd_docs_explain("effect001") == 0
    assert "EFFECT001" in capfd.readouterr().out
    assert docsengine.cmd_docs_explain("001") == 1
    assert docsengine.cmd_docs_explain("syntax") == 1
    assert docsengine.cmd_docs_explain("") == 1


# --- the SIGPIPE contract everywhere -------------------------------------------------------


def test_test_runner_sigpipe_exits_141(tmp_path: Path) -> None:
    lines = ["import Std.IO", ""]
    lines += [
        f'test "t{i:05d}_padding_padding_padding_padding" {{ assert_eq({i}, {i}) }}'
        for i in range(2500)
    ]
    path = tmp_path / "manytests.flx"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pipeline = f"{sys.executable} -m flx test {path} | head -1"
    proc = subprocess.run(
        ["bash", "-c", f"set -o pipefail; {pipeline}"], capture_output=True, text=True
    )
    assert proc.returncode == 141
    assert "Traceback" not in proc.stderr


# --- the published brace rule ---------------------------------------------------------------


def test_value_position_braces_are_records(tmp_path: Path) -> None:
    # The syntax-reference claim, as a user would hit it: a let initializer
    # `{ x = 5 }` is a record literal, while body braces stay blocks.
    path = tmp_path / "braces.flx"
    path.write_text(
        "type One = { x: I64 }\n"
        "fn get(p: One) -> I64 = { p.x }\n"
        "fn main() -> I64 = {\n"
        "  let q = { x = 5 }\n"
        "  mut total = 0\n"
        "  while total < 3 { total = total + 1 }\n"
        "  q.x + get({ x = 9 }) + total\n"
        "}\n",
        encoding="utf-8",
    )
    assert driver.cmd_run(str(path), interpret=True) == 17
