"""Multi-file modules: import resolution, merge, and `pub`/private visibility."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.macro import expand
from flx.modules import load_program
from flx.sema.specialize import check_and_monomorphize


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"
    return all(
        bool(shutil.which(t)) or os.path.exists(os.path.join(bindir, t))
        for t in ("mlir-opt", "mlir-translate", "clang")
    )


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


def _project(tmp_path: Path, files: dict[str, str]) -> str:
    """Write `files` (relative path -> source) under tmp_path; return the entry."""
    for rel, src in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src, encoding="utf-8")
    return str(tmp_path / "main.flx")


def _check_codes(tmp_path: Path, files: dict[str, str]) -> list[str]:
    entry = _project(tmp_path, files)
    with pytest.raises(FlexError) as exc:
        info = load_program(entry)
        check_and_monomorphize(
            expand(info.module),
            info.decl_module,
            info.public,
            info.file_module,
            info.module_spans,
            info.module_imports,
        )
    return [d.code for d in exc.value.diagnostics]


LIB = {
    "Lib/Math.flx": (
        "module Lib.Math\n"
        "pub fn add(x: I64, y: I64) -> I64 = { x + y }\n"
        "pub fn triple(n: I64) -> I64 = { add(n, add(n, n)) }\n"
        "fn secret() -> I64 = { 99 }\n"
    ),
}


def test_multifile_interpret(tmp_path: Path) -> None:
    entry = _project(
        tmp_path,
        {**LIB, "main.flx": "module Main\nimport Lib.Math\nfn main() -> I64 = { triple(14) }\n"},
    )
    assert driver.cmd_run(entry, interpret=True) == 42


@native
def test_multifile_native(tmp_path: Path) -> None:
    entry = _project(
        tmp_path,
        {**LIB, "main.flx": "module Main\nimport Lib.Math\nfn main() -> I64 = { triple(14) }\n"},
    )
    assert driver.cmd_run(entry) == 42


def test_transitive_import(tmp_path: Path) -> None:
    entry = _project(
        tmp_path,
        {
            **LIB,
            "Lib/Greet.flx": (
                "module Lib.Greet\nimport Lib.Math\npub fn greet(n: I64) -> I64 = { triple(n) }\n"
            ),
            "main.flx": "module Main\nimport Lib.Greet\nfn main() -> I64 = { greet(2) }\n",
        },
    )
    assert driver.cmd_run(entry, interpret=True) == 6


def test_private_function_is_hidden(tmp_path: Path) -> None:
    codes = _check_codes(
        tmp_path,
        {**LIB, "main.flx": "module Main\nimport Lib.Math\nfn main() -> I64 = { secret() }\n"},
    )
    assert "VIS001" in codes


def test_private_type_in_signature_is_hidden(tmp_path: Path) -> None:
    codes = _check_codes(
        tmp_path,
        {
            "Lib/T.flx": "module Lib.T\ntype Secret = { code: I64 }\n",
            "main.flx": (
                "module Main\nimport Lib.T\n"
                "fn takes(s: Secret) -> I64 = { s.code }\nfn main() -> I64 = { 0 }\n"
            ),
        },
    )
    assert "VIS001" in codes


def test_private_constructor_is_hidden(tmp_path: Path) -> None:
    codes = _check_codes(
        tmp_path,
        {
            "Lib/C.flx": "module Lib.C\ntype Color = | Red | Green\n",
            "main.flx": (
                "module Main\nimport Lib.C\n"
                "fn main() -> I64 = { match Red { Red => 1  Green => 2 } }\n"
            ),
        },
    )
    assert "VIS001" in codes


def test_pub_type_and_ctor_work(tmp_path: Path) -> None:
    entry = _project(
        tmp_path,
        {
            "Lib/C.flx": "module Lib.C\npub type Color = | Red | Green\n",
            "main.flx": (
                "module Main\nimport Lib.C\n"
                "fn main() -> I64 = { match Green { Red => 1  Green => 2 } }\n"
            ),
        },
    )
    assert driver.cmd_run(entry, interpret=True) == 2


def test_missing_import_is_reported(tmp_path: Path) -> None:
    entry = _project(
        tmp_path, {"main.flx": "module Main\nimport Lib.Nope\nfn main() -> I64 = { 0 }\n"}
    )
    assert driver.cmd_check(entry) == 1  # MOD001, rendered to stderr


# --- holes found and closed by the adversarial module review -------------------


def test_record_literal_cannot_construct_foreign_private(tmp_path: Path) -> None:
    # A bare record literal must not resolve to another module's private record
    # type (that would construct it); with the type invisible, nothing matches.
    codes = _check_codes(
        tmp_path,
        {
            "Lib/S.flx": (
                "module Lib.S\ntype Secret = { code: I64 }\n"
                "pub fn reveal(s: Secret) -> I64 = { s.code }\n"
            ),
            "main.flx": (
                "module Main\nimport Lib.S\n"
                "fn main() -> I64 = { let s = { code = 5 }\n  reveal(s) }\n"
            ),
        },
    )
    assert "TYPE014" in codes


def test_private_ctor_hidden_in_match_patterns(tmp_path: Path) -> None:
    # Naming a private constructor in a PATTERN is as much a reference as in an
    # expression — even when a pub function hands you the scrutinee.
    codes = _check_codes(
        tmp_path,
        {
            "Lib/C.flx": (
                "module Lib.C\ntype Color = | Red | Green\npub fn make() -> Color = { Red }\n"
            ),
            "main.flx": (
                "module Main\nimport Lib.C\n"
                "fn main() -> I64 = { match make() { Red => 1  Green => 2 } }\n"
            ),
        },
    )
    assert "VIS001" in codes


def test_generic_body_visibility_enforced(tmp_path: Path) -> None:
    # Monomorphized specializations inherit their template's module (via spans),
    # so a generic body cannot call another module's private function.
    codes = _check_codes(
        tmp_path,
        {
            "Lib/Sec.flx": "module Lib.Sec\nfn secret() -> I64 = { 77 }\n",
            "main.flx": (
                "module Main\nimport Lib.Sec\n"
                "fn ident<T>(x: T) -> I64 = { secret() }\nfn main() -> I64 = { ident(0) }\n"
            ),
        },
    )
    assert "VIS001" in codes


def test_impl_body_visibility_enforced(tmp_path: Path) -> None:
    codes = _check_codes(
        tmp_path,
        {
            "Lib/Sec.flx": "module Lib.Sec\nfn secret() -> I64 = { 77 }\n",
            "main.flx": (
                "module Main\nimport Lib.Sec\n"
                "trait Get = { fn get(self: Self) -> I64 }\ntype Box = { v: I64 }\n"
                "impl Get for Box = { fn get(self: Box) -> I64 = { secret() } }\n"
                "fn main() -> I64 = { let b = { v = 1 }\n  b.get() }\n"
            ),
        },
    )
    assert "VIS001" in codes


def test_import_header_must_match_path(tmp_path: Path) -> None:
    # A headerless imported file defaults to "Main", which mismatches its import
    # path — its definitions must not silently join the entry module.
    codes = _check_codes(
        tmp_path,
        {
            "Lib/NoHdr.flx": "fn sneaky() -> I64 = { 1 }\n",
            "main.flx": "module Main\nimport Lib.NoHdr\nfn main() -> I64 = { sneaky() }\n",
        },
    )
    assert "MOD002" in codes


def test_module_injection_rejected(tmp_path: Path) -> None:
    # A file cannot inject definitions into another module by declaring its name.
    codes = _check_codes(
        tmp_path,
        {
            "Lib/Evil.flx": "module Lib.Other\npub fn expose() -> I64 = { 1 }\n",
            "main.flx": "module Main\nimport Lib.Evil\nfn main() -> I64 = { expose() }\n",
        },
    )
    assert "MOD002" in codes


def test_duplicate_public_type_rejected(tmp_path: Path) -> None:
    codes = _check_codes(
        tmp_path,
        {
            "Lib/T1.flx": "module Lib.T1\npub type Thing = { a: I64 }\n",
            "Lib/T2.flx": "module Lib.T2\npub type Thing = { a: I64, b: I64 }\n",
            "main.flx": ("module Main\nimport Lib.T1\nimport Lib.T2\nfn main() -> I64 = { 0 }\n"),
        },
    )
    assert "TYPE002" in codes


def test_same_shape_private_records_do_not_collide(tmp_path: Path) -> None:
    # A private record in one module must not make a same-shaped record literal
    # ambiguous in another module — each side resolves to its own visible type.
    entry = _project(
        tmp_path,
        {
            "Lib/S.flx": (
                "module Lib.S\ntype Hidden = { x: I64, y: I64 }\n"
                "pub fn probe(n: I64) -> I64 = { let h = { x = n, y = n }\n  h.x }\n"
            ),
            "main.flx": (
                "module Main\nimport Lib.S\ntype Mine = { x: I64, y: I64 }\n"
                "fn main() -> I64 = { let m = { x = 40, y = 2 }\n  m.x + m.y + probe(0) }\n"
            ),
        },
    )
    assert driver.cmd_run(entry, interpret=True) == 42


def test_imported_tests_labeled_with_their_module(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    entry = _project(
        tmp_path,
        {
            "Lib/W.flx": (
                "module Lib.W\npub fn two() -> I64 = { 2 }\n"
                'test "lib test" { assert_eq(two(), 2) }\n'
            ),
            "main.flx": (
                "module Main\nimport Lib.W\nfn main() -> I64 = { two() }\n"
                'test "entry test" { assert_eq(two(), 2) }\n'
            ),
        },
    )
    assert driver.cmd_test(entry, interpret=True) == 0
    out = capfd.readouterr().out
    assert "ok Lib.W / lib test" in out
    assert "ok Main / entry test" in out


def test_cross_file_macro_and_gensym(tmp_path: Path) -> None:
    # One expander pass over the merged program: a macro defined in a lib and used
    # in the entry expands hygienically (no gensym collision across files).
    entry = _project(
        tmp_path,
        {
            "Lib/M.flx": "module Lib.M\nmacro dbl(x) = quote { unquote(x) + unquote(x) }\n",
            "main.flx": "module Main\nimport Lib.M\nfn main() -> I64 = { dbl(21) }\n",
        },
    )
    assert driver.cmd_run(entry, interpret=True) == 42
