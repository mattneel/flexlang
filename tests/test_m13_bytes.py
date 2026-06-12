"""Fixed-width integers and byte buffers."""

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


def _check(src_path: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "flx", "check", str(src_path)], capture_output=True
    )


BYTES = """\
module Main

import Std.Bytes
import Std.Str

test "bytes literal preserves binary data" {
  let bs = <<0x41, 0, 255, "PNG">>
  assert_eq(Bytes.len(bs), 6)
  assert_eq(Bytes.at(bs, 0), 65)
  assert_eq(Bytes.at(bs, 1), 0)
  assert_eq(Bytes.at(bs, 2), 255)
  assert_eq(Bytes.to_hex(bs), "4100ff504e47")
}

fn main() -> I64 = { 0 }
"""


def test_bytes_literal_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, BYTES)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_bytes_literal_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, BYTES)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


def test_fixed_width_integer_annotations(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """\
module Main

import Std.Str

test "fixed width annotations" {
  let a: U8 = 255
  let b: I8 = -128
  let c: U16 = 65535
  let d: I16 = -32768
  let e: U32 = 4294967295
  let f: I32 = -2147483648
  let g: U64 = 18446744073709551615
  assert_eq(to_str(a), "255")
  assert_eq(to_str(b), "-128")
  assert_eq(to_str(c), "65535")
  assert_eq(to_str(d), "-32768")
  assert_eq(to_str(e), "4294967295")
  assert_eq(to_str(f), "-2147483648")
  assert_eq(to_str(g), "18446744073709551615")
}
""",
    )
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


CONVERSIONS = """\
module Main

import Std.Str

test "fixed width conversions" {
  let i8: I8 = to_i8(-128)
  let u8: U8 = to_u8(255)
  let i16: I16 = to_i16(i8)
  let u16: U16 = to_u16(u8)
  let i32: I32 = to_i32(i16)
  let u32: U32 = to_u32(4294967295)
  let i64: I64 = to_i64(i32)
  let u64: U64 = to_u64(18446744073709551615)
  assert_eq(to_str(i8), "-128")
  assert_eq(to_str(u8), "255")
  assert_eq(to_str(i16), "-128")
  assert_eq(to_str(u16), "255")
  assert_eq(to_str(i64), "-128")
  assert_eq(to_str(u32), "4294967295")
  assert_eq(to_str(u64), "18446744073709551615")
  assert_eq(to_str(to_i64(12.9)), "12")
  assert(to_f64(u64) > to_f64(9223372036854775807))
}

fn main() -> I64 = { 0 }
"""


def test_fixed_width_conversions_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, CONVERSIONS)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_fixed_width_conversions_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, CONVERSIONS)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


def test_fixed_width_integer_literal_range_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """\
module Main

fn main() -> I64 = {
  let bad: U8 = 256
  0
}
""",
    )
    proc = _check(path)
    text = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode != 0
    assert "TYPE011" in text
    assert "out of range for U8" in text


def test_fixed_width_conversion_literal_range_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """\
module Main

fn main() -> I64 = {
  let bad = to_u8(256)
  0
}
""",
    )
    proc = _check(path)
    text = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode != 0
    assert "TYPE011" in text
    assert "out of range for U8" in text
