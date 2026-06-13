"""Flex source formatter."""

from __future__ import annotations

from pathlib import Path

from flx.cli import main
from flx.syntax.formatter import format_source

MESSY = """\
module   Main {import Lib.Math as Math fn main( x:I64)->I64={let y=Math.add(x,1) y}}
"""

FORMATTED = """\
module Main {
  import Lib.Math as Math

  fn main(x: I64) -> I64 = {
    let y = Math.add(x, 1)
    y
  }
}
"""


def test_format_source_is_stable() -> None:
    assert format_source(MESSY) == FORMATTED
    assert format_source(FORMATTED) == FORMATTED


def test_fmt_writes_file(tmp_path: Path) -> None:
    path = tmp_path / "main.flx"
    path.write_text(MESSY, encoding="utf-8")

    assert main(["fmt", str(path)]) == 0
    assert path.read_text(encoding="utf-8") == FORMATTED


def test_fmt_check_reports_unformatted(tmp_path: Path, capsys) -> None:
    path = tmp_path / "main.flx"
    path.write_text(MESSY, encoding="utf-8")

    assert main(["fmt", "--check", str(path)]) == 1
    assert "would reformat" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == MESSY


def test_fmt_stdout_does_not_write(tmp_path: Path, capsys) -> None:
    path = tmp_path / "main.flx"
    path.write_text(MESSY, encoding="utf-8")

    assert main(["fmt", "--stdout", str(path)]) == 0
    assert capsys.readouterr().out == FORMATTED
    assert path.read_text(encoding="utf-8") == MESSY
