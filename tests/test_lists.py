"""List literals and `List<T>` (interpreter-only for now)."""

from __future__ import annotations

from pathlib import Path

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.macro import expand
from flx.sema.specialize import check_and_monomorphize
from flx.syntax.parser import parse


def _codes(src: str) -> list[str]:
    with pytest.raises(FlexError) as exc:
        check_and_monomorphize(expand(parse(src)))
    return [d.code for d in exc.value.diagnostics]


def test_list_of_records_typechecks_and_runs(tmp_path: Path) -> None:
    src = (
        "type Dep = { name: String, path: String }\n"
        'fn deps() -> List<Dep> = { [ { name = "a", path = "x" }, { name = "b", path = "y" } ] }\n'
        "fn main() -> I64 = { let d = deps()\n  2 }\n"
    )
    flx = tmp_path / "l.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_run(str(flx), interpret=True) == 2


def test_empty_list_infers_from_context(tmp_path: Path) -> None:
    src = "fn nothing() -> List<I64> = { [] }\nfn main() -> I64 = { let n = nothing()\n  0 }\n"
    flx = tmp_path / "e.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_run(str(flx), interpret=True) == 0


def test_heterogeneous_list_rejected() -> None:
    assert "TYPE003" in _codes("fn f() -> List<I64> = { [1, true] }")


def test_empty_list_without_context_rejected() -> None:
    assert "TYPE023" in _codes("fn f() -> I64 = { let x = []\n 0 }")


def test_list_arity_enforced() -> None:
    assert "TYPE013" in _codes("fn f(x: List<I64, Bool>) -> I64 = { 0 }")


def test_lists_compare_structurally(tmp_path: Path) -> None:
    src = "fn main() -> I64 = { if [1, 2] == [1, 2] { 0 } else { 1 } }\n"
    flx = tmp_path / "eq.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_run(str(flx), interpret=True) == 0
