"""Milestone 6: IO correctness — read_line returns Option<String> (a blank
line and EOF are distinguishable), from_byte/from_bytes build strings from
bytes, and \\xNN escapes put raw bytes in string literals. Everything
differential: both backends must agree byte-for-byte."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flx.diagnostics import FlexError
from flx.syntax.lexer import tokenize
from flx.syntax.parser import parse


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"
    return all(
        bool(shutil.which(t)) or os.path.exists(os.path.join(bindir, t))
        for t in ("mlir-opt", "mlir-translate", "clang")
    )


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


def _write(tmp_path: Path, src: str) -> str:
    flx = tmp_path / "main.flx"
    flx.write_text(src, encoding="utf-8")
    return str(flx)


def _run(path: str, stdin: bytes = b"", backend: str = "interp") -> tuple[int, bytes, bytes]:
    args = [sys.executable, "-m", "flx", "run", path]
    if backend == "native":
        args.insert(4, "--native")
    proc = subprocess.run(args, input=stdin, capture_output=True)
    return proc.returncode, proc.stdout, proc.stderr


def _test_cmd(path: str, backend: str = "interp") -> tuple[int, bytes]:
    args = [sys.executable, "-m", "flx", "test", path]
    if backend == "native":
        args.insert(4, "--native")
    proc = subprocess.run(args, capture_output=True)
    return proc.returncode, proc.stdout


READ_LOOP = """\
module Main
import Std.IO

fn main() -> I64 uses { Fs, Log } = {
  mut count = 0
  mut going = true
  while going {
    match read_line() {
      Some(line) => {
        println("[" ++ line ++ "]")
        count = count + 1
      }
      None => { going = false }
    }
  }
  count
}
"""


# --- read_line -> Option<String> -----------------------------------------------------


def test_read_line_blank_vs_eof(tmp_path: Path) -> None:
    # The v3 design gap: a blank line is Some(""), EOF is None — three lines in,
    # three lines out, the blank one preserved.
    path = _write(tmp_path, READ_LOOP)
    code, out, _ = _run(path, b"a\n\nb\n")
    assert (code, out) == (3, b"[a]\n[]\n[b]\n")


@native
def test_read_line_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, READ_LOOP)
    for stdin in (b"a\n\nb\n", b"", b"unterminated", b"caf\xc3\xa9\n\xff raw\n"):
        interp = _run(path, stdin, "interp")
        nat = _run(path, stdin, "native")
        assert interp == nat, f"backend divergence for stdin={stdin!r}"


def test_read_line_final_line_without_newline(tmp_path: Path) -> None:
    path = _write(tmp_path, READ_LOOP)
    code, out, _ = _run(path, b"ab")
    assert (code, out) == (1, b"[ab]\n")


def test_read_line_eof_immediately(tmp_path: Path) -> None:
    path = _write(tmp_path, READ_LOOP)
    code, out, _ = _run(path, b"")
    assert (code, out) == (0, b"")


def test_read_line_passes_raw_bytes_through(tmp_path: Path) -> None:
    # Bytes in, bytes out: no encoding is assumed (0xFF is not valid UTF-8).
    path = _write(tmp_path, READ_LOOP)
    code, out, _ = _run(path, b"caf\xc3\xa9 \xff\n")
    assert (code, out) == (1, b"[caf\xc3\xa9 \xff]\n")


def test_read_line_is_an_option_in_the_checker(tmp_path: Path) -> None:
    # Using the old String contract must fail to type-check now.
    src = (
        "module Main\nimport Std.IO\nimport Std.Str\n"
        "fn main() -> I64 uses { Fs } = { length(read_line()) }\n"
    )
    path = _write(tmp_path, src)
    proc = subprocess.run(
        [sys.executable, "-m", "flx", "check", path], capture_output=True, text=True
    )
    assert proc.returncode == 1
    assert "Option" in proc.stderr


# --- from_byte / from_bytes ------------------------------------------------------------

BYTES_TESTS = """\
module Main
import Std.Str
import Std.List

test "from_byte basics" {
  assert_eq(from_byte(65), "A")
  assert_eq(from_byte(0xE9), "\\xe9")
  assert_eq(byte_at(from_byte(255), 0), 255)
}

test "concat completes utf-8" {
  assert_eq(from_byte(195) ++ from_byte(169), "é")
}

test "from_bytes basics" {
  assert_eq(from_bytes([104, 105]), "hi")
  assert_eq(from_bytes([195, 169]), "é")
  assert_eq(from_bytes([]), "")
}

test "round-trip through byte_at" {
  mut bs: List<I64> = []
  mut i = 0
  while i < length("flex \\xff bytes") {
    List.push(bs, byte_at("flex \\xff bytes", i))
    i = i + 1
  }
  assert_eq(from_bytes(bs), "flex \\xff bytes")
}

test "byte 0 panics" {
  assert_eq(from_byte(0), "never")
}

test "byte 256 panics" {
  assert_eq(from_bytes([65, 256]), "never")
}

test "negative byte panics" {
  assert_eq(from_byte(-1), "never")
}
"""


def test_from_bytes_suite_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, BYTES_TESTS)
    code, out = _test_cmd(path)
    assert code == 1  # the three panic tests fail BY DESIGN, recovered per-test
    assert out.count(b"\nok ") == 4
    assert out.count(b"byte 0 is outside 1..255 (strings are NUL-terminated)") == 1
    assert out.count(b"byte 256 is outside 1..255") == 1
    assert out.count(b"byte -1 is outside 1..255") == 1
    assert b"4 passed, 3 failed" in out


@native
def test_from_bytes_suite_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, BYTES_TESTS)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


def test_from_byte_panic_message_in_run(tmp_path: Path) -> None:
    src = "module Main\nimport Std.Str\nfn main() -> I64 = { length(from_byte(0)) }\n"
    path = _write(tmp_path, src)
    code, _, err = _run(path)
    assert code == 1
    assert b"flx: runtime error: byte 0 is outside 1..255 (strings are NUL-terminated)" in err


def test_from_bytes_empty_infers(tmp_path: Path) -> None:
    # The intrinsic's param type is inference context: from_bytes([]) works.
    src = "module Main\nimport Std.Str\nfn main() -> I64 = { length(from_bytes([])) }\n"
    path = _write(tmp_path, src)
    assert _run(path)[0] == 0


# --- \xNN escapes ------------------------------------------------------------------------


def test_hex_escape_values() -> None:
    tokens = tokenize('let a = "\\x41" let b = "\\xc3\\xa9" let c = "\\xff"')
    strings = [t.text for t in tokens if t.kind.name == "STRING"]
    assert strings[0] == "A"
    assert strings[1] == "é"  # adjacent escapes completing UTF-8 canonicalize
    assert strings[2] == "\udcff"  # a stray byte stays a surrogate


@native
def test_hex_escape_output_parity(tmp_path: Path) -> None:
    src = (
        "module Main\nimport Std.IO\n"
        'fn main() -> I64 uses { Log } = { println("\\xe9 and \\xc3\\xa9")\n  0 }\n'
    )
    path = _write(tmp_path, src)
    interp = _run(path, b"", "interp")
    nat = _run(path, b"", "native")
    assert interp == nat
    assert interp[1] == b"\xe9 and \xc3\xa9\n"


def test_hex_escape_equality_with_literal(tmp_path: Path) -> None:
    src = (
        "module Main\nimport Std.Str\n"
        'test "spelled bytes equal typed text" { assert_eq("\\xc3\\xa9", "é") }\n'
    )
    path = _write(tmp_path, src)
    assert _test_cmd(path)[0] == 0


def test_hex_escape_rejects_nul() -> None:
    with pytest.raises(FlexError) as exc:
        parse('fn main() -> I64 = { let s = "\\x00"\n 0 }')
    assert any("NUL-terminated" in d.message for d in exc.value.diagnostics)


def test_hex_escape_needs_two_digits() -> None:
    for bad in ('"\\x4"', '"\\xZZ"', '"\\x"'):
        with pytest.raises(FlexError) as exc:
            parse(f"fn main() -> I64 = {{ let s = {bad}\n 0 }}")
        assert any("two hex digits" in d.message for d in exc.value.diagnostics)


def test_unknown_escape_message_names_xnn() -> None:
    with pytest.raises(FlexError) as exc:
        parse('fn main() -> I64 = { let s = "\\q"\n 0 }')
    assert any("\\xNN" in d.message for d in exc.value.diagnostics)


# --- raw bytes in test names and reports ---------------------------------------------------

HOSTILE_NAMES = """\
module Hostile
import Std.Str

test "label with \\xe9 raw byte" { assert_eq(1, 1) }

test "failing \\xff label" { assert_eq(from_byte(0xFF), "\\xfe") }
"""


def test_surrogate_test_labels_interp(tmp_path: Path) -> None:
    # Labels and failure reports carry the raw bytes, not U+FFFD mojibake.
    path = _write(tmp_path, HOSTILE_NAMES)
    code, out = _test_cmd(path)
    assert code == 1
    assert b"ok Hostile / label with \xe9 raw byte" in out
    assert b"fail Hostile / failing \xff label" in out
    assert b'actual "\xff", expected "\xfe"' in out
    assert "�".encode() not in out  # no U+FFFD replacement mojibake


@native
def test_surrogate_test_labels_parity(tmp_path: Path) -> None:
    # The native harness embeds labels as octal-escaped C strings; the interp
    # writes raw bytes. Same bytes out.
    path = _write(tmp_path, HOSTILE_NAMES)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


def test_surrogate_doc_test_name_synthesizes(tmp_path: Path) -> None:
    # A doc test named with \xNN must survive synthesis (the synthesized file
    # is written as UTF-8; surrogates must re-escape, not crash).
    src = (
        "module Hostile\nimport Std.Str\n"
        "pub fn one() -> I64 = { 1 }\n"
        'doc one { test "name with \\xff byte" { assert_eq(one(), 1) } }\n'
        "fn main() -> I64 = { 0 }\n"
    )
    path = _write(tmp_path, src)
    proc = subprocess.run(
        [sys.executable, "-m", "flx", "test", "--docs", path], capture_output=True
    )
    assert proc.returncode == 0, proc.stderr
    assert b"Traceback" not in proc.stderr


# --- the discoverability hints -----------------------------------------------------------


def test_unimported_std_name_hints_module(tmp_path: Path) -> None:
    src = "fn main() -> I64 = { let s = from_byte(65)\n  0 }\n"
    path = _write(tmp_path, src)
    proc = subprocess.run(
        [sys.executable, "-m", "flx", "check", path], capture_output=True, text=True
    )
    assert proc.returncode == 1
    assert "`import Std.Str` provides from_byte" in proc.stderr


def test_chr_hint_points_at_from_byte() -> None:
    with pytest.raises(FlexError) as exc:
        from flx.macro import expand
        from flx.sema.specialize import check_and_monomorphize

        check_and_monomorphize(expand(parse("fn main() -> I64 = { let s = chr(65)\n 0 }")))
    assert any("from_byte" in (d.help or "") for d in exc.value.diagnostics)
