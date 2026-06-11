"""Milestone 5: docs as checked declarations — the `doc` language form, the
flx docs engine (check/build/explain), the "not yet, use Y" diagnostics batch,
and the v3 contract fixes (parse_int overflow, SIGPIPE)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from flx import docsengine, driver
from flx.diagnostics import FlexError
from flx.macro import expand
from flx.sema.specialize import check_and_monomorphize
from flx.syntax.parser import parse


def _diag(src: str) -> list:
    with pytest.raises(FlexError) as exc:
        check_and_monomorphize(expand(parse(src)))
    return exc.value.diagnostics


def _write(tmp_path: Path, src: str) -> str:
    flx = tmp_path / "main.flx"
    flx.write_text(src, encoding="utf-8")
    return str(flx)


# --- the doc declaration form -------------------------------------------------------

DOC_PROGRAM = '''
module Demo

pub fn double(x: I64) -> I64 = { x * 2 }

doc module {
  summary "A demo module."
  since "0.0.1"
  status implemented
}

doc double {
  summary "Double an integer."
  text """
  Multi-line prose,
  dedented.
  """
  test "doubles" {
    assert_eq(double(21), 42)
  }
  see Std.Math
}

doc "A Guide Page" {
  slug "guide/demo"
  snippet "future syntax" {
    let m = Map.empty<String, I64>()
  }
  test "documented failure" expect_error EFFECT001 {
    fn bad() -> Unit = { Log.info("x") }
    fn main() -> I64 = { 0 }
  }
}

fn main() -> I64 = { double(21) }
'''


def test_doc_declarations_parse_and_run(tmp_path: Path) -> None:
    path = _write(tmp_path, DOC_PROGRAM)
    assert driver.cmd_run(path, interpret=True) == 42


def test_doc_decl_structure() -> None:
    module = parse(DOC_PROGRAM)
    docs = module.docs
    assert len(docs) == 3
    mod_doc, sym_doc, page = docs
    assert mod_doc.target == "module" and mod_doc.status == "implemented"
    assert sym_doc.target == "double" and sym_doc.summary == "Double an integer."
    texts = [c for c in sym_doc.content if type(c).__name__ == "DocText"]
    assert texts[0].text == "Multi-line prose,\ndedented."  # \"\"\" dedents
    tests = [c for c in sym_doc.content if type(c).__name__ == "DocTest"]
    assert tests[0].name == "doubles" and "assert_eq" in tests[0].source
    assert page.title == "A Guide Page" and page.slug == "guide/demo"
    err_tests = [
        c for c in page.content if type(c).__name__ == "DocTest" and c.expect_error is not None
    ]
    assert err_tests[0].expect_error == "EFFECT001"
    assert "fn bad" in err_tests[0].source  # raw capture, never type-checked here
    snippets = [c for c in page.content if type(c).__name__ == "DocSnippet"]
    assert "Map.empty" in snippets[0].source  # sketches may show future syntax


def test_doc001_missing_symbol() -> None:
    diags = _diag('doc ghost { summary "x" }\nfn main() -> I64 = { 0 }\n')
    assert any(d.code == "DOC001" for d in diags)


def test_doc001_missing_see_ref() -> None:
    diags = _diag("fn real() -> I64 = { 1 }\ndoc real { see ghost }\nfn main() -> I64 = { 0 }\n")
    assert any(d.code == "DOC001" for d in diags)


def test_doc_see_std_module_ok() -> None:
    src = "fn real() -> I64 = { 1 }\ndoc real { see Std.Str }\nfn main() -> I64 = { 0 }\n"
    check_and_monomorphize(expand(parse(src)))  # no error


def test_doc004_status_contradicts_reality() -> None:
    diags = _diag(
        "fn real() -> I64 = { 1 }\ndoc real { status not_yet }\nfn main() -> I64 = { 0 }\n"
    )
    assert any(d.code == "DOC004" for d in diags)


def test_flx_test_docs_runs_doc_examples(tmp_path: Path) -> None:
    path = _write(tmp_path, DOC_PROGRAM)
    proc = subprocess.run(
        [sys.executable, "-m", "flx", "test", "--docs", path],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "double: doubles" in proc.stdout


# --- the docs engine ------------------------------------------------------------------


def test_stdlib_docs_collect() -> None:
    units = docsengine.collect()
    modules = {u.module for u in units}
    assert {"Std.Str", "Std.List", "Std.IO", "Std.Math"} <= modules
    # every public stdlib symbol is documented (DOC005's invariant)
    documented = docsengine._documented_targets(units)
    assert {"split", "parse_int", "map", "fold", "sqrt", "read_line"} <= documented


def test_render_api_pages_carry_signatures() -> None:
    pages = docsengine.render_api_pages()
    page = pages["api/Std.Str.md"]
    assert "fn split(s: String, sep: String) -> List<String>" in page
    assert "checked by `flx docs check`" in page
    assert "reference/syntax.md" in pages  # the guide page renders from docsrc
    assert any(p.startswith("diagnostics/EFFECT001") for p in pages)


def test_docs_build_is_fresh() -> None:
    # The committed generated pages must match a fresh render (the CI gate).
    assert docsengine.cmd_docs_build(check_only=True) == 0


def test_docs_explain(capfd: pytest.CaptureFixture[str]) -> None:
    assert docsengine.cmd_docs_explain("EFFECT001") == 0
    out = capfd.readouterr().out
    assert "uses { ... }" in out
    assert docsengine.cmd_docs_explain("NOPE999") == 1


# --- the "not yet, use Y" diagnostics batch -------------------------------------------


def test_break_says_not_yet() -> None:
    diags = _diag("fn main() -> I64 = { mut i = 0\n while i < 9 { break }\n i }\n")
    assert any("does not have 'break'" in d.message for d in diags)


def test_tuple_hint() -> None:
    with pytest.raises(FlexError) as exc:
        parse("fn main() -> I64 = { let t = (1, 2)\n 0 }")
    assert any("no tuples yet" in d.message for d in exc.value.diagnostics)


def test_lambda_hint() -> None:
    with pytest.raises(FlexError) as exc:
        parse("fn main() -> I64 = { let f = fn(x) => x\n 0 }")
    assert any("no lambdas" in d.message for d in exc.value.diagnostics)


def test_index_assign_hint() -> None:
    with pytest.raises(FlexError) as exc:
        parse("fn main() -> I64 = { let xs = [1]\n xs[0] = 5\n 0 }")
    assert any("indexed assignment" in d.message for d in exc.value.diagnostics)


def test_unknown_escape_is_an_error() -> None:
    with pytest.raises(FlexError) as exc:
        parse('fn main() -> I64 uses { Log } = { Log.info("a\\x41b")\n 0 }')
    assert any("unknown string escape" in d.message for d in exc.value.diagnostics)


def test_member_call_hints_free_function(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    # Needs real module loading so Std.Str's `split` is a known free function.
    path = _write(
        tmp_path,
        'module Main\nimport Std.Str\nfn main() -> I64 = { let p = "a b".split(" ")\n 0 }\n',
    )
    assert driver.cmd_check(path) == 1
    err = capfd.readouterr().err
    assert "TYPE010" in err and "free function" in err


def test_parse_float_absent_hint() -> None:
    diags = _diag('fn main() -> I64 = { let x = parse_float("1.5")\n 0 }\n')
    assert any("atof" in (d.help or "") for d in diags)


# --- contract fixes ---------------------------------------------------------------------


def test_parse_int_overflow_returns_none(tmp_path: Path) -> None:
    src = (
        "module Main\nimport Std.Str\n"
        "fn main() -> I64 = {\n"
        '  match parse_int("99999999999999999999") { Some(n) => 1  None => 0 }\n}\n'
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 0


def test_parse_int_int64_min_parses(tmp_path: Path) -> None:
    src = (
        "module Main\nimport Std.Str\n"
        "fn main() -> I64 = {\n"
        '  match parse_int("-9223372036854775808") {\n'
        "    Some(n) => { if n == -9223372036854775808 { 0 } else { 1 } }\n"
        "    None => 2\n  }\n}\n"
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 0


def test_sigpipe_exits_141(tmp_path: Path) -> None:
    src = (
        "module Main\nimport Std.IO\n"
        "fn main() -> I64 uses { Log } = {\n"
        "  mut i = 0\n  while i < 100000 { println(to_str(i))\n  i = i + 1 }\n  0\n}\n"
    )
    path = _write(tmp_path, src)
    reader = f"{sys.executable} -m flx run {path} | head -1"
    proc = subprocess.run(
        ["bash", "-c", f"set -o pipefail; {reader}"], capture_output=True, text=True
    )
    assert proc.returncode == 141
    assert "Traceback" not in proc.stderr
