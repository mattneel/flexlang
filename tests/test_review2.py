"""Regression tests for the second adversarial review (new-features bugs)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.sema.check import check
from flx.syntax.parser import parse


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"

    def has(tool: str) -> bool:
        return bool(shutil.which(tool)) or os.path.exists(os.path.join(bindir, tool))

    return all(has(t) for t in ("mlir-opt", "mlir-translate", "clang"))


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


def _codes(src: str) -> list[str]:
    with pytest.raises(FlexError) as exc:
        check(parse(src))
    return [d.code for d in exc.value.diagnostics]


def test_string_equality_rejected() -> None:
    assert "TYPE019" in _codes('test "t" { assert_eq("a", "a") }')


def test_nested_ctor_pattern_rejected() -> None:
    src = (
        "type E = | Zero | Neg\n"
        "fn f(r: Result<I64, E>) -> I64 = { match r { Ok(v) => v\n Err(Zero) => 1 } }"
    )
    assert "MATCH004" in _codes(src)


def test_duplicate_match_arm_rejected() -> None:
    src = "type C = | A | B\nfn f(c: C) -> I64 = { match c { A => 1\n A => 2\n B => 3 } }"
    assert "MATCH002" in _codes(src)


def test_duplicate_record_field_rejected() -> None:
    assert "TYPE020" in _codes("type P = { x: I64 }\nfn f() -> P = { x = 1, x = 2 }")


def test_region_yielding_region_rejected() -> None:
    assert "REGION001" in _codes("fn f() -> Region = { region s { s } }")


def test_record_equality_checks() -> None:
    check(parse("type P = { x: I64, y: I64 }\nfn eq(a: P, b: P) -> Bool = { a == b }"))


def test_qualified_constructor_with_payload_checks() -> None:
    check(parse("type E = | Code(I64) | Nil\nfn mk(x: I64) -> E = { E.Code(x) }"))


@native
def test_record_equality_runs(tmp_path: Path) -> None:
    src = (
        "type P = { x: I64, y: I64 }\n"
        "fn eq(a: P, b: P) -> Bool = { a == b }\n"
        "fn main() -> I64 = { let a = { x = 1, y = 2 }\n"
        "  let b = { x = 1, y = 9 }\n if eq(a, a) && !eq(a, b) { 1 } else { 0 } }"
    )
    flx = tmp_path / "p.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_run(str(flx)) == 1


@native
def test_adt_equality_runs(tmp_path: Path) -> None:
    src = (
        "fn ok(x: I64) -> Result<I64, I64> = { Ok(x) }\n"
        'test "eq" { assert_eq(ok(5), ok(5))\n assert_ne(ok(5), ok(6)) }'
    )
    flx = tmp_path / "a.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_test(str(flx)) == 0


@native
def test_qualified_ctor_with_payload_runs(tmp_path: Path) -> None:
    src = (
        "type E = | Code(I64) | Nil\n"
        "fn mk(x: I64) -> E = { E.Code(x) }\n"
        "fn main() -> I64 = { match mk(42) { Code(c) => c\n Nil => 0 } }"
    )
    flx = tmp_path / "q.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_run(str(flx)) == 42
