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
    info = load_program(entry)
    with pytest.raises(FlexError) as exc:
        check_and_monomorphize(expand(info.module), info.decl_module, info.public)
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
