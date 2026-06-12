"""Import aliases, selective imports, and per-module import scope."""

from __future__ import annotations

from pathlib import Path

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.macro import expand
from flx.modules import load_program
from flx.sema.specialize import check_and_monomorphize


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


LIB = """\
module Lib.Math {
  pub fn add(x: I64, y: I64) -> I64 = { x + y }
  pub fn triple(n: I64) -> I64 = { add(n, add(n, n)) }
  fn secret() -> I64 = { 99 }
}
"""


def test_import_alias_allows_qualified_call(tmp_path: Path) -> None:
    entry = _project(
        tmp_path,
        {
            "Lib/Math.flx": LIB,
            "main.flx": """\
module Main {
  import Lib.Math as Math

  fn main() -> I64 = { Math.triple(14) }
}
""",
        },
    )
    assert driver.cmd_run(entry, interpret=True) == 42


def test_import_alias_does_not_import_unqualified_names(tmp_path: Path) -> None:
    codes = _check_codes(
        tmp_path,
        {
            "Lib/Math.flx": LIB,
            "main.flx": """\
module Main {
  import Lib.Math as Math

  fn main() -> I64 = { triple(14) }
}
""",
        },
    )
    assert "NAME001" in codes


def test_selective_import_allows_selected_name(tmp_path: Path) -> None:
    entry = _project(
        tmp_path,
        {
            "Lib/Math.flx": LIB,
            "main.flx": """\
module Main {
  import Lib.Math.{triple}

  fn main() -> I64 = { triple(14) }
}
""",
        },
    )
    assert driver.cmd_run(entry, interpret=True) == 42


def test_selective_import_hides_unselected_unqualified_name(tmp_path: Path) -> None:
    codes = _check_codes(
        tmp_path,
        {
            "Lib/Math.flx": LIB,
            "main.flx": """\
module Main {
  import Lib.Math.{triple}

  fn main() -> I64 = { add(40, 2) }
}
""",
        },
    )
    assert "NAME001" in codes


def test_import_scope_is_per_module_not_global(tmp_path: Path) -> None:
    codes = _check_codes(
        tmp_path,
        {
            "main.flx": f"""\
{LIB}

module Lib.User {{
  import Lib.Math

  pub fn call() -> I64 = {{ triple(14) }}
}}

module Main {{
  import Lib.User

  fn main() -> I64 = {{ triple(14) }}
}}
""",
        },
    )
    assert "NAME001" in codes


def test_alias_qualified_call_respects_private_visibility(tmp_path: Path) -> None:
    codes = _check_codes(
        tmp_path,
        {
            "Lib/Math.flx": LIB,
            "main.flx": """\
module Main {
  import Lib.Math as Math

  fn main() -> I64 = { Math.secret() }
}
""",
        },
    )
    assert "VIS001" in codes
