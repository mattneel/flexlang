"""Regression tests for the macro/comptime adversarial review."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.macro import expand
from flx.sema.check import check
from flx.syntax.dump import dump_module
from flx.syntax.parser import parse


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"

    def has(tool: str) -> bool:
        return bool(shutil.which(tool)) or os.path.exists(os.path.join(bindir, tool))

    return all(has(t) for t in ("mlir-opt", "mlir-translate", "clang"))


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


def _codes(src: str) -> list[str]:
    with pytest.raises(FlexError) as exc:
        check(expand(parse(src)))
    return [d.code for d in exc.value.diagnostics]


def test_recursive_macro_hits_depth_limit() -> None:
    assert "MAC003" in _codes(
        "macro loop(x) = quote { loop(unquote(x)) }\nfn f() -> I64 = { loop(1) }"
    )


def test_nested_comptime_sees_outer_bindings() -> None:
    dump = dump_module(
        expand(parse("fn f() -> I64 = { comptime { let x = 5\n comptime { x + 1 } } }"))
    )
    assert "Expr 6" in dump


def test_multi_payload_variant_rejected() -> None:
    assert "TYPE022" in _codes("type Pair = | Both(I64, I64)\nfn f() -> I64 = { 0 }")


def test_derive_eq_on_string_record_needs_string_eq() -> None:
    # Records with String fields now derive field-wise through the Eq trait —
    # without `impl Eq for String` in scope (import Std.Str), dispatch fails.
    assert "DISP001" in _codes(
        'derive(Eq) type W = { s: String }\nfn f(w: W) -> Bool = { w.eq({ s = "x" }) }'
    )


def test_derive_eq_on_string_adt_payload_rejected() -> None:
    assert "DER001" in _codes("derive(Eq) type W = | Tag(String) | Empty")


def test_derive_show_multi_payload_rejected() -> None:
    # The multi-field variant is rejected (TYPE022 from check or DER001 from derive).
    codes = _codes("derive(Show) type T = | Pair(I64, I64) | One(I64)")
    assert "DER001" in codes or "TYPE022" in codes


@native
def test_comptime_negative_div_matches_runtime(tmp_path: Path) -> None:
    src = (
        "module Main\n"
        "fn d(a: I64, b: I64) -> I64 = { a / b }\n"
        "fn r(a: I64, b: I64) -> I64 = { a % b }\n"
        "fn main() -> I64 = {\n"
        "  if comptime { 0 - 7 / 2 } == d(0 - 7, 2)\n"
        "  && comptime { 0 - 7 % 2 } == r(0 - 7, 2) { 1 } else { 0 } }"
    )
    flx = tmp_path / "d.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_run(str(flx)) == 1
