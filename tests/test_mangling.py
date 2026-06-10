"""Symbol-mangling injectivity (Phase 0 of the module system).

The backend identifies a function by its mangled symbol, so the mangler MUST be
injective: distinct (function, type-args) and (trait, type, method) tuples must
never produce the same string — otherwise two functions collapse into one and the
program miscompiles. These tests pin the encoding against the specific collisions
an adversarial design review found in the old `$`-joined scheme.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver
from flx.sema.check import _mangle, _mono_key, _type_enc, spec_symbol
from flx.types import BOOL, I64, AdtType, PrimType, RecordType


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"
    return all(
        bool(shutil.which(t)) or os.path.exists(os.path.join(bindir, t))
        for t in ("mlir-opt", "mlir-translate", "clang")
    )


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


def _opt(arg: object) -> AdtType:
    return AdtType("Option", (), (arg,))  # type: ignore[arg-type]


def _pair(a: object, b: object) -> AdtType:
    return AdtType("Pair", (), (a, b))  # type: ignore[arg-type]


def test_type_enc_distinguishes_nesting_from_arity() -> None:
    # `Pair<I64, Bool>` (one nested 2-arg type) vs the two leaves spread out must
    # not flatten to the same token string.
    nested = _type_enc(_pair(I64, BOOL))
    assert nested == "2$Pair$0$I64$0$Bool"
    assert _type_enc(I64) == "0$I64"
    assert _type_enc(_opt(I64)) == "1$Option$0$I64"
    # distinct type args -> distinct keys (the monomorphization dedup key)
    assert _mono_key(_opt(I64)) != _mono_key(_opt(BOOL))
    assert _mono_key(_pair(I64, BOOL)) != _mono_key(_pair(BOOL, I64))


def test_generic_spec_arity_is_recoverable() -> None:
    # h<Pair<I64,Bool>> (1 type arg) vs h<Pair,I64,Bool> (3 type args): the old
    # `$`-join made both `h$Pair$I64$Bool`; tagged + arity-framed they differ.
    one = spec_symbol("h", (_mono_key(_pair(I64, BOOL)),))
    three = spec_symbol("h", (_mono_key(PrimType("Pair")), _mono_key(I64), _mono_key(BOOL)))
    assert one != three
    assert one.startswith("g$h$1$")
    assert three.startswith("g$h$3$")


def test_producers_are_tag_disjoint() -> None:
    # A generic spec, a trait-impl method, and a plain function live in disjoint
    # namespaces. The classic cross-producer collision: a module fn `Geo.map`
    # specialized at I64 vs `impl Geo for map`'s method I64.
    spec = spec_symbol("Geo$map", (_mono_key(I64),))  # (module-prefixed name, later)
    method = _mangle("Geo", "map", "I64")
    assert spec != method
    assert spec.startswith("g$") and method.startswith("t$")
    # plain user function names never contain `$`, so never collide with either
    assert "$" not in "describe"
    assert spec_symbol("Show", (_mono_key(_opt(I64)),)) != _mangle("Show", "Option", "show")


def test_encoding_is_injective_over_a_type_zoo() -> None:
    point = RecordType("Point", ())
    zoo = [
        I64,
        BOOL,
        point,
        _opt(I64),
        _opt(BOOL),
        _opt(point),
        _pair(I64, BOOL),
        _pair(BOOL, I64),
        _opt(_opt(I64)),
        _opt(_pair(I64, BOOL)),
        _pair(_opt(I64), BOOL),
    ]
    encs = [_type_enc(t) for t in zoo]
    assert len(set(encs)) == len(encs), "distinct types must encode distinctly"


@native
def test_nested_generic_and_trait_method_coexist(tmp_path: Path) -> None:
    # End-to-end: a generic instantiated at a record, the same generic at an ADT,
    # and a trait method — all distinct symbols, all linked into one binary.
    src = (
        "trait Show = { fn show(self: Self) -> String }\n"
        "type Point = { x: I64, y: I64 }\n"
        'impl Show for Point = { fn show(self: Point) -> String = { "P" } }\n'
        "fn id<T>(x: T) -> T = { x }\n"
        "fn main() -> I64 = { let a = id({ x = 4, y = 0 })\n"
        "  let b = id(Some(3))\n"
        "  a.x + (match b { Some(v) => v  None => 0 }) }"
    )
    flx = tmp_path / "m.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_run(str(flx)) == 7
