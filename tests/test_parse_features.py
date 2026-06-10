"""Parser tests for records, ADTs, match, regions, and `?`."""

from __future__ import annotations

from flx.syntax import ast
from flx.syntax.dump import dump_module
from flx.syntax.parser import parse


def test_adt_declaration() -> None:
    module = parse("type Result<T, E> =\n  | Ok(T)\n  | Err(E)")
    adt = module.adts[0]
    assert adt.name == "Result"
    assert adt.type_params == ["T", "E"]
    assert [(v.name, [t.name for t in v.payload]) for v in adt.variants] == [
        ("Ok", ["T"]),
        ("Err", ["E"]),
    ]


def test_enum_without_payloads() -> None:
    module = parse("type Color = | Red | Green | Blue")
    assert [v.name for v in module.adts[0].variants] == ["Red", "Green", "Blue"]
    assert all(v.payload == [] for v in module.adts[0].variants)


def test_record_declaration() -> None:
    module = parse("type User =\n{\n  id: U64\n  email: String\n  active: Bool\n}")
    rec = module.records[0]
    assert rec.name == "User"
    assert [(f.name, f.type.name) for f in rec.fields] == [
        ("id", "U64"),
        ("email", "String"),
        ("active", "Bool"),
    ]


def test_record_construction() -> None:
    fn = parse('fn f() -> User = { id = 1, email = "x", active = true }').functions[0]
    assert isinstance(fn.body.tail, ast.RecordExpr)
    assert [f.name for f in fn.body.tail.fields] == ["id", "email", "active"]


def test_record_update() -> None:
    fn = parse("fn f(u: User) -> User = { u with active = false }").functions[0]
    assert isinstance(fn.body.tail, ast.RecordUpdateExpr)
    assert isinstance(fn.body.tail.base, ast.NameExpr)


def test_field_access_is_member_expr() -> None:
    fn = parse("fn f(u: User) -> U64 = { u.id }").functions[0]
    assert isinstance(fn.body.tail, ast.MemberExpr)


def test_match_expression() -> None:
    src = "fn f(r: Result<I64, E>) -> I64 = {\n  match r {\n    Ok(v) => v\n    Err(_) => 0\n  }\n}"
    fn = parse(src).functions[0]
    match = fn.body.tail
    assert isinstance(match, ast.MatchExpr)
    assert isinstance(match.arms[0].pattern, ast.CtorPattern)
    assert match.arms[0].pattern.name == "Ok"
    assert isinstance(match.arms[0].pattern.args[0], ast.BindPattern)
    assert isinstance(match.arms[1].pattern.args[0], ast.WildcardPattern)


def test_region_expression() -> None:
    fn = parse("fn f() -> I64 = { region scratch { 40 + 2 } }").functions[0]
    assert isinstance(fn.body.tail, ast.RegionExpr)
    assert fn.body.tail.name == "scratch"


def test_try_operator() -> None:
    src = "fn f() -> Result<I64, E> = {\n  let x = div(10, 2)?\n  Ok(x)\n}"
    fn = parse(src).functions[0]
    assert isinstance(fn.body.stmts[0], ast.LetStmt)
    assert isinstance(fn.body.stmts[0].value, ast.TryExpr)


def test_dump_roundtrip_smoke() -> None:
    # Records-and-blocks disambiguation: a block body still parses as a block.
    module = parse("fn f() -> I64 = {\n  let x = 1\n  x + 1\n}")
    assert "Let x = 1" in dump_module(module)
