"""Tests for the Flex parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from flx.diagnostics import FlexError
from flx.syntax import ast
from flx.syntax.dump import dump_module
from flx.syntax.parser import parse


def test_parse_add_example() -> None:
    module = parse(Path("examples/add.flx").read_text(encoding="utf-8"), "examples/add.flx")
    assert module.name == "Main"
    assert [f.name for f in module.functions] == ["add", "main"]
    assert [t.name for t in module.tests] == ["add works"]
    add = module.functions[0]
    assert [(p.name, p.type.name) for p in add.params] == [("a", "I64"), ("b", "I64")]
    assert add.return_type is not None and add.return_type.name == "I64"
    assert isinstance(add.body.tail, ast.BinaryExpr)


def test_default_module_name_is_main() -> None:
    module = parse("fn main() -> I64 = { 0 }")
    assert module.name == "Main"


def test_precedence_and_associativity() -> None:
    module = parse("fn f() -> I64 = { 1 + 2 * 3 - 4 }")
    # ((1 + (2 * 3)) - 4)
    assert dump_module(module).endswith("Expr ((1 + (2 * 3)) - 4)")


def test_comparison_and_boolean() -> None:
    module = parse("fn f(x: I64) -> Bool = { x > 0 && x < 10 }")
    assert dump_module(module).endswith("Expr ((x > 0) && (x < 10))")


def test_pipe_desugars_to_call() -> None:
    module = parse("fn f(x: I64) -> I64 = { x |> add(1) }")
    assert dump_module(module).endswith("Expr add(x, 1)")


def test_import_alias_and_selective_import_parse() -> None:
    module = parse(
        "module Main\n"
        "import Lib.Math as Math\n"
        "import Lib.Text.{trim, split}\n"
        "fn main() -> I64 = { 0 }"
    )
    assert module.imports == ["Lib.Math", "Lib.Text"]
    assert module.import_decls[0].alias == "Math"
    assert module.import_decls[1].names == ("trim", "split")
    dumped = dump_module(module)
    assert "import Lib.Math as Math" in dumped
    assert "import Lib.Text.{trim, split}" in dumped


def test_mut_while_assignment() -> None:
    parse(Path("examples/hello.flx").read_text(encoding="utf-8"))  # smoke
    src = """
    fn sum(n: I64) -> I64 = {
      mut total = 0
      while total < n { total = total + 1 }
      total
    }
    """
    fn = parse(src).functions[0]
    assert [type(s).__name__ for s in fn.body.stmts] == ["MutStmt", "WhileStmt", "ExprStmt"]
    assert isinstance(fn.body.stmts[1], ast.WhileStmt)
    assert isinstance(fn.body.stmts[1].body.stmts[0], ast.AssignStmt)


def test_if_else_expression() -> None:
    fn = parse("fn f(x: I64) -> I64 = { if x > 0 { x } else { 0 - x } }").functions[0]
    assert isinstance(fn.body.tail, ast.IfExpr)
    assert fn.body.tail.else_block is not None


def test_test_block_with_effects() -> None:
    module = parse('test "t" uses { Log, Fs } { assert(true) }')
    assert module.tests[0].effects == ["Log", "Fs"]


def test_error_on_missing_paren() -> None:
    with pytest.raises(FlexError):
        parse("fn f( -> I64 = { 0 }")
