"""Block-scoped modules."""

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


def test_block_module_main_runs(tmp_path: Path) -> None:
    entry = _project(
        tmp_path,
        {
            "main.flx": """\
module Main {
  fn main() -> I64 = { 42 }
}
"""
        },
    )
    assert driver.cmd_run(entry, interpret=True) == 42


def test_one_file_can_define_multiple_modules(tmp_path: Path) -> None:
    entry = _project(
        tmp_path,
        {
            "main.flx": """\
module Lib.Math {
  pub fn triple(n: I64) -> I64 = { n + n + n }
}

module Main {
  import Lib.Math

  fn main() -> I64 = { triple(14) }
}
"""
        },
    )
    assert driver.cmd_run(entry, interpret=True) == 42


def test_same_file_block_modules_keep_private_visibility(tmp_path: Path) -> None:
    codes = _check_codes(
        tmp_path,
        {
            "main.flx": """\
module Lib.Secret {
  fn secret() -> I64 = { 99 }
  pub fn reveal() -> I64 = { secret() }
}

module Main {
  import Lib.Secret

  fn main() -> I64 = { secret() }
}
"""
        },
    )
    assert "VIS001" in codes


def test_import_can_find_block_module_in_non_conventional_file(tmp_path: Path) -> None:
    entry = _project(
        tmp_path,
        {
            "Lib/Bundle.flx": """\
module Lib.Math {
  pub fn triple(n: I64) -> I64 = { n + n + n }
}
""",
            "main.flx": """\
module Main {
  import Lib.Math

  fn main() -> I64 = { triple(14) }
}
""",
        },
    )
    assert driver.cmd_run(entry, interpret=True) == 42


def test_module_qualified_call(tmp_path: Path) -> None:
    entry = _project(
        tmp_path,
        {
            "main.flx": """\
module Lib.Math {
  pub fn triple(n: I64) -> I64 = { n + n + n }
}

module Main {
  import Lib.Math

  fn main() -> I64 = { Lib.Math.triple(14) }
}
"""
        },
    )
    assert driver.cmd_run(entry, interpret=True) == 42


@native
def test_module_qualified_call_native(tmp_path: Path) -> None:
    entry = _project(
        tmp_path,
        {
            "main.flx": """\
module Lib.Math {
  pub fn triple(n: I64) -> I64 = { n + n + n }
}

module Main {
  import Lib.Math

  fn main() -> I64 = { Lib.Math.triple(14) }
}
"""
        },
    )
    assert driver.cmd_run(entry, native=True) == 42


def test_module_qualified_call_respects_private_visibility(tmp_path: Path) -> None:
    codes = _check_codes(
        tmp_path,
        {
            "main.flx": """\
module Lib.Secret {
  fn secret() -> I64 = { 99 }
}

module Main {
  import Lib.Secret

  fn main() -> I64 = { Lib.Secret.secret() }
}
"""
        },
    )
    assert "VIS001" in codes
