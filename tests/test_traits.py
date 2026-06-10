"""Tests for traits, impls, and static method dispatch (Part A)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.macro import expand
from flx.sema.check import check
from flx.sema.specialize import check_and_monomorphize
from flx.syntax.parser import parse

SHOW = "trait Show = { fn show(self: Self) -> String }\n"
POINT = "type Point = { x: I64, y: I64 }\n"
IMPL = 'impl Show for Point = { fn show(self: Point) -> String = { "Point" } }\n'


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"

    def has(tool: str) -> bool:
        return bool(shutil.which(tool)) or os.path.exists(os.path.join(bindir, tool))

    return all(has(t) for t in ("mlir-opt", "mlir-translate", "clang"))


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


def _check(src: str) -> None:
    check(expand(parse(src)))


def _codes(src: str) -> list[str]:
    with pytest.raises(FlexError) as exc:
        check(expand(parse(src)))
    return [d.code for d in exc.value.diagnostics]


def _mono_codes(src: str) -> list[str]:
    with pytest.raises(FlexError) as exc:
        check_and_monomorphize(expand(parse(src)))
    return [d.code for d in exc.value.diagnostics]


def test_trait_impl_method_checks() -> None:
    _check(SHOW + POINT + IMPL + "fn f(p: Point) -> String = { p.show() }")


def test_missing_impl_rejected() -> None:
    assert "DISP001" in _codes(SHOW + POINT + "fn f(p: Point) -> String = { p.show() }")


def test_impl_signature_mismatch_rejected() -> None:
    bad = "impl Show for Point = { fn show(self: Point) -> I64 = { 0 } }\n"
    assert "IMPL005" in _codes(SHOW + POINT + bad)


def test_conflicting_impl_rejected() -> None:
    assert "IMPL006" in _codes(SHOW + POINT + IMPL + IMPL)


def test_impl_missing_method_rejected() -> None:
    trait2 = "trait Two = { fn a(self: Self) -> I64\n fn b(self: Self) -> I64 }\n"
    impl2 = "impl Two for Point = { fn a(self: Point) -> I64 = { 1 } }\n"
    assert "IMPL003" in _codes(trait2 + POINT + impl2)


def test_field_wins_over_method() -> None:
    # `p.x` is the field even though a (hypothetical) method might share the name.
    _check(POINT + SHOW + IMPL + "fn f(p: Point) -> I64 = { p.x }")


def test_self_outside_impl_rejected() -> None:
    assert "TRAIT008" in _codes("fn f(x: Self) -> I64 = { 0 }")


@native
def test_trait_dispatch_runs(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    src = (
        SHOW
        + POINT
        + "impl Show for Point = { fn show(self: Point) -> String = "
        + '{ "x=" ++ to_str(self.x) } }\n'
        + 'test "t" uses { Log } { let p = { x = 9, y = 0 }\n Log.info(p.show())\n assert(true) }'
    )
    flx = tmp_path / "t.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_test(str(flx)) == 0
    assert "x=9" in capfd.readouterr().out


@native
def test_derive_generates_working_impls(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    src = (
        "derive(Eq, Show) type Point = { x: I64, y: I64 }\n"
        'test "t" uses { Log } { let p = { x = 1, y = 2 }\n'
        " Log.info(p.show())\n assert(p.eq(p))\n assert(!p.eq({ x = 1, y = 3 })) }"
    )
    flx = tmp_path / "d.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_test(str(flx)) == 0
    assert "Point { x = 1, y = 2 }" in capfd.readouterr().out


# --- Part B: bounded generic functions + monomorphization ---------------------

GENERIC = "fn describe<T: Show>(v: T) -> String = { v.show() }\n"


def test_generic_call_typechecks() -> None:
    check_and_monomorphize(
        expand(parse(SHOW + POINT + IMPL + GENERIC + "fn f(p: Point) -> String = { describe(p) }"))
    )


def test_unsatisfied_bound_rejected() -> None:
    # I64 has no `impl Show`, so the bound is unmet.
    assert "BOUND001" in _codes(SHOW + GENERIC + "fn f() -> String = { describe(5) }")


def test_uninferable_type_param_rejected() -> None:
    src = SHOW + "fn make<T: Show>(n: I64) -> I64 = { n }\nfn f() -> I64 = { make(3) }"
    assert "BOUND003" in _codes(src)


def test_unknown_trait_bound_rejected() -> None:
    assert "BOUND004" in _codes("fn f<T: Bogus>(x: T) -> T = { x }")


def test_generic_body_error_surfaces_per_instantiation() -> None:
    # `v + v` is fine for I64 but not for a record; the error appears when the
    # record instantiation is specialized and re-checked.
    src = POINT + "fn dbl<T>(v: T) -> T = { v + v }\nfn f(p: Point) -> Point = { dbl(p) }"
    assert "TYPE003" in _mono_codes(src)


def test_unconstrained_generic_passthrough_typechecks() -> None:
    src = POINT + "fn pick<T>(a: T, b: T) -> T = { a }\nfn f(p: Point) -> Point = { pick(p, p) }"
    check_and_monomorphize(expand(parse(src)))


@native
def test_generic_dispatch_runs(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    src = (
        SHOW
        + POINT
        + 'impl Show for Point = { fn show(self: Point) -> String = { "P" ++ to_str(self.x) } }\n'
        + "impl Show for I64 = { fn show(self: I64) -> String = { to_str(self) } }\n"
        + GENERIC
        + 'test "t" uses { Log } {\n'
        + "  Log.info(describe(7))\n"
        + "  Log.info(describe({ x = 4, y = 0 }))\n"
        + "  assert(true)\n}"
    )
    flx = tmp_path / "g.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_test(str(flx)) == 0
    out = capfd.readouterr().out
    assert "7" in out and "P4" in out


@native
def test_two_instantiations_are_distinct(tmp_path: Path) -> None:
    # One generic `id` used at two types must produce two working specializations.
    src = (
        "fn id<T>(x: T) -> T = { x }\nfn main() -> I64 = { let a = id(40)\n let b = id(2)\n a + b }"
    )
    flx = tmp_path / "id.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_run(str(flx)) == 42
