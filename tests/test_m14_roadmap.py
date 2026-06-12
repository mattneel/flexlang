"""Roadmap polish after the v5 study."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"
    return all(
        bool(shutil.which(t)) or os.path.exists(os.path.join(bindir, t))
        for t in ("mlir-opt", "mlir-translate", "clang")
    )


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


def _write(tmp_path: Path, src: str) -> Path:
    path = tmp_path / "main.flx"
    path.write_text(src, encoding="utf-8")
    return path


def _test_cmd(path: Path, backend: str = "interp") -> tuple[int, bytes]:
    cmd = [sys.executable, "-m", "flx", "test"]
    if backend == "native":
        cmd.append("--native")
    cmd.append(str(path))
    proc = subprocess.run(cmd, capture_output=True)
    return proc.returncode, proc.stdout + proc.stderr


def _run_cmd(
    path: Path, *flags: str, backend: str = "interp"
) -> subprocess.CompletedProcess[bytes]:
    cmd = [sys.executable, "-m", "flx", "run", *flags]
    if backend == "native":
        cmd.append("--native")
    cmd.append(str(path))
    return subprocess.run(cmd, capture_output=True)


COMPOSITE_EQ = """\
module Main

import Std.Map
import Std.Str

derive(Eq, Show) type StudentReport = { name: String, grade: I64 }
derive(Eq) type Jsonish = { fields: Map<String, I64> }

test "list equality composes over strings and records" {
  assert_eq(["ann", "bob"], ["ann", "bob"])
  assert_ne(["ann", "bob"], ["ann"])
  assert_eq(["ann", "bob"].show(), "[ann, bob]")

  let reports: List<StudentReport> = [{ name = "ann", grade = 95 }]
  assert_eq(reports, [{ name = "ann", grade = 95 }])
  assert_eq(reports.show(), "[StudentReport { name = ann, grade = 95 }]")
}

test "derive eq composes over map fields" {
  let left: Map<String, I64> = Map.new()
  Map.set(left, "a", 1)
  Map.set(left, "b", 2)
  let same: Map<String, I64> = Map.new()
  Map.set(same, "a", 1)
  Map.set(same, "b", 2)
  let different: Map<String, I64> = Map.new()
  Map.set(different, "a", 1)
  Map.set(different, "b", 3)

  assert_eq({ fields = left }, { fields = same })
  assert_ne({ fields = left }, { fields = different })
}
"""


def test_composite_equality_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, COMPOSITE_EQ)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_composite_equality_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, COMPOSITE_EQ)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


def _data_helpers(path: Path) -> str:
    return f"""\
module Main

import Std.Csv
import Std.Fs
import Std.Str

test "text data helpers" uses {{ Fs }} {{
  assert_eq(lower_ascii("A_Z 09!"), "a_z 09!")
  assert(is_ascii_alpha(65))
  assert(is_ascii_digit(57))
  assert(is_ascii_alnum(122))
  assert(!is_ascii_alnum(45))
  assert_eq(parse_csv_line("a,\\"b,c\\",\\"d\\"\\"e\\""), Ok(["a", "b,c", "d\\"e"]))
  assert_eq(parse_csv_line("\\"unterminated"), Err("unterminated quoted field"))

  match write_text("{path}", "a") {{ Ok(_) => ()  Err(e) => fail(e) }}
  match append_text("{path}", "b") {{ Ok(_) => ()  Err(e) => fail(e) }}
  assert_eq(read_text("{path}"), Ok("ab"))
}}
"""


def test_text_data_helpers_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, _data_helpers(tmp_path / "data.txt"))
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_text_data_helpers_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, _data_helpers(tmp_path / "data-native.txt"))
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


def test_run_quiet_status_and_missing_file_parity(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """\
module Main

import Std.Fs
import Std.IO

fn main() -> I64 uses { Fs, Log } = {
  match read_text("missing.csv") {
    Ok(_) => 0
    Err(e) => { eprintln("read error: " ++ e) 7 }
  }
}
""",
    )
    noisy = _run_cmd(path)
    assert noisy.returncode == 7
    assert b"flx: exited with code 7" in noisy.stderr

    interp = _run_cmd(path, "--quiet-status")
    assert interp.returncode == 7
    assert b"flx: exited with code" not in interp.stderr

    if _tools_available():
        nat = _run_cmd(path, "--quiet-status", backend="native")
        assert nat.returncode == 7
        assert interp.stderr == nat.stderr == b"read error: No such file or directory\n"
