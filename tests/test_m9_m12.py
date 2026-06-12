"""Coverage for docs CLI, CLI stdlib, collections, and binary formatting."""

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
    flx = tmp_path / "main.flx"
    flx.write_text(src, encoding="utf-8")
    return flx


def _run(path: Path, *args: str, backend: str = "interp") -> subprocess.CompletedProcess[bytes]:
    cmd = [sys.executable, "-m", "flx", "run"]
    if backend == "native":
        cmd.append("--native")
    cmd.extend([str(path), *args])
    return subprocess.run(cmd, capture_output=True)


def _test_cmd(path: Path, backend: str = "interp") -> tuple[int, bytes]:
    cmd = [sys.executable, "-m", "flx", "test"]
    if backend == "native":
        cmd.append("--native")
    cmd.append(str(path))
    proc = subprocess.run(cmd, capture_output=True)
    return proc.returncode, proc.stdout


def test_docs_cli_supports_local_check_and_build(tmp_path: Path) -> None:
    lib = tmp_path / "Lib.flx"
    lib.write_text(
        """\
module Lib

pub fn inc(x: I64) -> I64 = { x + 1 }

doc module {
  summary "A tiny library."
  status implemented
}

doc inc {
  summary "Increment by one."
  test "increments" {
    assert_eq(inc(41), 42)
  }
}
""",
        encoding="utf-8",
    )
    docs = tmp_path / "site"

    check = subprocess.run(
        [sys.executable, "-m", "flx", "docs", "check", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr

    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "flx",
            "docs",
            "build",
            str(tmp_path),
            "--output",
            str(docs),
        ],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    page = docs / "api" / "Lib.md"
    assert page.is_file()
    assert "fn inc(x: I64) -> I64" in page.read_text(encoding="utf-8")


def test_human_docs_do_not_ship_raw_mdbook_includes() -> None:
    examples = Path("docs/examples.md").read_text(encoding="utf-8")
    assert "{{#include" not in examples


CLI_STDLIB = """\
module Main

import Std.Arg
import Std.Fs
import Std.IO
import Std.Str

fn main() -> I64 uses { Process, Fs, Log } = {
  let path = match at(0) {
    Some(p) => p
    None => ""
  }
  if eq(path, "") {
    eprintln("missing path")
    return 10
  }
  match write_text(path, "  hi\\n") {
    Ok(_) => ()
    Err(e) => { eprintln(e) return 11 }
  }
  mut text = ""
  match read_text(path) {
    Ok(s) => { text = s }
    Err(e) => { eprintln(e) return 12 }
  }
  if !eq(trim(text), "hi") { return 13 }
  if !has_flag("--ok") { return 14 }
  match value_after("--name") {
    Some(v) => { if eq(v, "flex") { 0 } else { 15 } }
    None => 16
  }
}
"""


def test_cli_stdlib_file_args_trim_and_stderr_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, CLI_STDLIB)
    data = tmp_path / "data.txt"
    proc = _run(path, str(data), "--ok", "--name", "flex")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert data.read_text(encoding="utf-8") == "  hi\n"
    assert proc.stderr == b""

    err_src = _write(
        tmp_path,
        'module Main\nimport Std.IO\nfn main() -> I64 uses { Log } = { eprintln("err")\n0 }\n',
    )
    err_proc = _run(err_src)
    assert err_proc.returncode == 0
    assert err_proc.stdout == b""
    assert err_proc.stderr == b"err\n"


@native
def test_cli_stdlib_file_args_trim_native(tmp_path: Path) -> None:
    path = _write(tmp_path, CLI_STDLIB)
    data = tmp_path / "data-native.txt"
    proc = _run(path, str(data), "--ok", "--name", "flex", backend="native")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert data.read_text(encoding="utf-8") == "  hi\n"


COLLECTIONS = """\
module Main

import Std.List
import Std.Map
import Std.Str

test "entries, index assignment, and structural equality" {
  let m: Map<String, I64> = Map.new()
  Map.set(m, "a", 1)
  Map.set(m, "b", 2)
  let es = entries(m)
  match es[0] {
    MapEntry(k, v) => {
      assert_eq(k, "a")
      assert_eq(v, 1)
    }
  }
  match es[1] {
    MapEntry(k, v) => {
      assert_eq(k, "b")
      assert_eq(v, 2)
    }
  }

  mut xs = [1, 2, 3]
  xs[1] = 20
  assert_eq(xs, [1, 20, 3])
  assert(xs.eq([1, 20, 3]))
  assert_eq(xs.show(), "[1, 20, 3]")

  let n: Map<String, I64> = Map.new()
  Map.set(n, "b", 2)
  Map.set(n, "a", 1)
  assert_eq(m, n)
  assert(m.eq(n))
  assert_eq(m.show(), "{a: 1, b: 2}")

  mut words = ["pear", "apple", "fig"]
  sort_with(words, fn(a: String, b: String) -> Bool => str_lt(b, a))
  assert_eq(words[0], "pear")
  assert_eq(words[2], "apple")
}
"""


def test_collection_ergonomics_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, COLLECTIONS)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_collection_ergonomics_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, COLLECTIONS)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


FORMATTING = """\
module Main

import Std.Str

test "unsigned formatting and byte buffers" {
  assert_eq(to_hex(0), "0")
  assert_eq(to_hex(255), "ff")
  assert_eq(to_hex(-1), "ffffffffffffffff")
  assert_eq(to_unsigned(-1), "18446744073709551615")
  let bs = to_bytes("A\\xff")
  assert_eq(List.len(bs), 2)
  assert_eq(bs[0], 65)
  assert_eq(bs[1], 255)
}
"""


def test_unsigned_formatting_and_byte_buffers_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, FORMATTING)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_unsigned_formatting_and_byte_buffers_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, FORMATTING)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")
