"""Tests for name resolution and type checking."""

from __future__ import annotations

from pathlib import Path

import pytest

from flx.diagnostics import FlexError
from flx.sema.check import check
from flx.syntax.parser import parse
from flx.types import BOOL, I64


def _check(src: str) -> None:
    check(parse(src))


def _expr_type(src: str, fn_name: str = "f") -> object:
    module = parse(src)
    result = check(module)
    fn = next(f for f in module.functions if f.name == fn_name)
    tail = fn.body.tail
    assert tail is not None
    return result.expr_types[id(tail)]


def test_add_example_checks() -> None:
    check(parse(Path("examples/add.flx").read_text(encoding="utf-8"), "examples/add.flx"))


def test_arithmetic_is_i64() -> None:
    assert _expr_type("fn f(a: I64, b: I64) -> I64 = { a + b * 2 }") is I64


def test_comparison_is_bool() -> None:
    assert _expr_type("fn f(x: I64) -> Bool = { x < 10 }") is BOOL


def test_if_branches_must_match() -> None:
    with pytest.raises(FlexError) as exc:
        _check("fn f(x: I64) -> I64 = { if x > 0 { 1 } else { true } }")
    assert any(d.code == "TYPE008" for d in exc.value.diagnostics)


def test_return_type_mismatch() -> None:
    with pytest.raises(FlexError) as exc:
        _check("fn f() -> Bool = { 1 + 2 }")
    assert any(d.code == "TYPE003" for d in exc.value.diagnostics)


def test_assign_to_immutable_fails() -> None:
    with pytest.raises(FlexError) as exc:
        _check("fn f() -> I64 = { let x = 1\n x = 2\n x }")
    assert any(d.code == "MUT001" for d in exc.value.diagnostics)


def test_mut_assignment_ok() -> None:
    _check("fn f() -> I64 = { mut x = 1\n x = 2\n x }")


def test_unknown_name() -> None:
    with pytest.raises(FlexError) as exc:
        _check("fn f() -> I64 = { y }")
    assert any(d.code == "NAME001" for d in exc.value.diagnostics)


def test_unknown_type() -> None:
    with pytest.raises(FlexError) as exc:
        _check("fn f(x: Widget) -> I64 = { 0 }")
    assert any(d.code == "TYPE001" for d in exc.value.diagnostics)


def test_call_arity() -> None:
    with pytest.raises(FlexError) as exc:
        _check("fn g(a: I64) -> I64 = { a }\nfn f() -> I64 = { g(1, 2) }")
    assert any(d.code == "TYPE005" for d in exc.value.diagnostics)


def test_assert_eq_type_mismatch() -> None:
    with pytest.raises(FlexError) as exc:
        _check('test "t" { assert_eq(1, true) }')
    assert any(d.code == "TYPE003" for d in exc.value.diagnostics)


def test_wrong_arg_type() -> None:
    with pytest.raises(FlexError) as exc:
        _check("fn g(a: I64) -> I64 = { a }\nfn f() -> I64 = { g(true) }")
    assert any(d.code == "TYPE003" for d in exc.value.diagnostics)
