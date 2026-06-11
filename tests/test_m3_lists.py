"""Milestone 3 from the blind study: the data layer — List<T> on both backends,
runtime for-in, let/mut type annotations, byte-level strings, and Env.argv."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flx import driver
from flx.diagnostics import FlexError
from flx.macro import expand
from flx.sema.specialize import check_and_monomorphize
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


def _diag(src: str) -> list:
    with pytest.raises(FlexError) as exc:
        check_and_monomorphize(expand(parse(src)))
    return exc.value.diagnostics


def _both(tmp_path: Path, src: str, code: int, out: str | None = None, capfd=None) -> None:
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == code
    interp_out = capfd.readouterr().out if capfd is not None else None
    if out is not None:
        assert interp_out == out
    if _tools_available():
        assert driver.cmd_run(path, native=True) == code
        if capfd is not None:
            assert capfd.readouterr().out == interp_out


# --- the core list surface ---------------------------------------------------------


def test_list_core_ops(tmp_path: Path) -> None:
    _both(
        tmp_path,
        "fn main() -> I64 = {\n"
        "  let xs = [10, 20, 30]\n"
        "  List.push(xs, 40)\n"
        "  List.set(xs, 0, 11)\n"
        "  xs[0] + xs[3] + List.len(xs) * 100\n}\n",
        # 11 + 40 + 400 = 451 & 0xFF = 195
        195,
    )


def test_for_in_and_range(tmp_path: Path) -> None:
    _both(
        tmp_path,
        "module Main\nimport Std.List\n"
        "fn main() -> I64 = {\n"
        "  mut total = 0\n"
        "  for x in [5, 6, 7] { total = total + x }\n"
        "  for i in range(1, 5) { total = total + i }\n"
        "  total\n}\n",
        28,  # 18 + 10
    )


def test_empty_list_needs_annotation() -> None:
    diags = _diag("fn main() -> I64 = { let xs = []\n 0 }\n")
    assert any("cannot infer the element type" in d.message for d in diags)


def test_let_annotation_types_empty_list(tmp_path: Path) -> None:
    _both(
        tmp_path,
        "fn main() -> I64 = {\n  mut xs: List<I64> = []\n  List.push(xs, 9)\n  xs[0]\n}\n",
        9,
    )


def test_let_annotation_mismatch_rejected() -> None:
    diags = _diag('fn main() -> I64 = { let x: I64 = "s"\n 0 }\n')
    assert any(d.code == "TYPE003" for d in diags)


def test_index_oob_message_parity(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    src = "fn main() -> I64 = { let xs = [1, 2]\n xs[5] }\n"
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 1
    interp_err = capfd.readouterr().err
    assert "index 5 out of bounds (len 2)" in interp_err
    if _tools_available():
        assert driver.cmd_run(path, native=True) == 1
        assert "index 5 out of bounds (len 2)" in capfd.readouterr().err


def test_index_string_rejected_with_hint() -> None:
    diags = _diag('fn main() -> I64 = { let s = "ab"\n let c = s[0]\n 0 }\n')
    assert any(d.code == "TYPE017" and "char_at" in (d.help or "") for d in diags)


def test_list_reference_semantics(tmp_path: Path) -> None:
    _both(
        tmp_path,
        "fn main() -> I64 = {\n"
        "  let xs = [1]\n  let ys = xs\n  List.push(ys, 2)\n  List.len(xs)\n}\n",
        2,
    )


def test_eq_on_lists_rejected() -> None:
    diags = _diag("fn main() -> I64 = { if [1] == [1] { 0 } else { 1 } }\n")
    assert any(d.code == "TYPE019" for d in diags)


# --- lists in structures -----------------------------------------------------------


def test_list_in_record_and_payload(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    _both(
        tmp_path,
        "module Main\nimport Std.IO\n"
        "type Bag = { name: String, items: List<String> }\n"
        "type Holder = | Has(List<I64>) | Nothing2\n"
        "fn main() -> I64 uses { Log } = {\n"
        '  let b = { name = "bag", items = ["x"] }\n'
        '  List.push(b.items, "y")\n'
        '  for it in b.items { println(b.name ++ ":" ++ it) }\n'
        "  match Has([5, 6]) { Has(xs) => xs[0] + xs[1]  Nothing2 => 0 }\n}\n",
        11,
        out="bag:x\nbag:y\n",
        capfd=capfd,
    )


def test_list_of_lists(tmp_path: Path) -> None:
    _both(
        tmp_path,
        "fn main() -> I64 = { let grid = [[1, 2], [3, 4]]\n grid[1][0] + grid[0][1] }\n",
        5,
    )


def test_string_elements_round_trip(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    _both(
        tmp_path,
        "module Main\nimport Std.IO\n"
        "fn main() -> I64 uses { Log } = {\n"
        '  let names = ["ada", "grace"]\n'
        "  for n in names { println(n) }\n  List.len(names)\n}\n",
        2,
        out="ada\ngrace\n",
        capfd=capfd,
    )


# --- byte-level strings --------------------------------------------------------------


def test_str_byte_ops(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    _both(
        tmp_path,
        "module Main\nimport Std.IO\nimport Std.Str\n"
        "fn main() -> I64 uses { Log } = {\n"
        '  println(char_at("hello", 1))\n'
        '  println(substr("hello", 1, 3))\n'
        '  println(substr("hello", 3, 99))\n'
        '  byte_at("A", 0)\n}\n',
        65,
        out="e\nell\nlo\n",
        capfd=capfd,
    )


def test_split_and_parse_int(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    _both(
        tmp_path,
        "module Main\nimport Std.IO\nimport Std.Str\n"
        "fn main() -> I64 uses { Log } = {\n"
        '  let parts = split("a,bb,,ccc", ",")\n'
        '  for p in parts { println("[" ++ p ++ "]") }\n'
        "  mut total = 0\n"
        '  match parse_int("-42") { Some(n) => { total = total + n }  None => () }\n'
        '  match parse_int("4x") { Some(n) => { total = total + n }  None => () }\n'
        "  total + List.len(parts)\n}\n",
        218,  # -42 + 4 = -38 & 0xFF
        out="[a]\n[bb]\n[]\n[ccc]\n",
        capfd=capfd,
    )


def test_byte_at_oob_panics(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    src = 'module Main\nimport Std.Str\nfn main() -> I64 = { byte_at("ab", 9) }\n'
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 1
    assert "index 9 out of bounds (len 2)" in capfd.readouterr().err
    if _tools_available():
        assert driver.cmd_run(path, native=True) == 1
        assert "index 9 out of bounds (len 2)" in capfd.readouterr().err


def test_non_ascii_bytes_lossless(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    # Byte semantics: "é" is two UTF-8 bytes; splitting them stays lossless and
    # re-concatenating restores the character. Identical on both backends.
    _both(
        tmp_path,
        "module Main\nimport Std.IO\nimport Std.Str\n"
        "fn main() -> I64 uses { Log } = {\n"
        '  let s = "héllo"\n'
        "  println(to_str(length(s)))\n"
        '  println(substr(s, 1, 2) ++ "")\n'
        "  0\n}\n",
        0,
        out="6\n\xe9\n",  # length counts bytes; bytes 1..2 are the é sequence
        capfd=capfd,
    )


# --- argv ------------------------------------------------------------------------------


def test_argv_via_cli(tmp_path: Path) -> None:
    src = (
        "module Main\nimport Std.IO\n"
        "fn main() -> I64 uses { Process, Log } = {\n"
        '  for a in Env.argv() { println("[" ++ a ++ "]") }\n'
        "  List.len(Env.argv())\n}\n"
    )
    path = _write(tmp_path, src)
    proc = subprocess.run(
        [sys.executable, "-m", "flx", "run", path, "alpha", "two words"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert proc.stdout == "[alpha]\n[two words]\n"


def test_argv_requires_process_effect() -> None:
    diags = _diag("fn main() -> I64 = { List.len(Env.argv()) }\n")
    assert any(d.code == "EFFECT001" and "Process" in d.message for d in diags)


def test_argv_empty_without_args(tmp_path: Path) -> None:
    _both(
        tmp_path,
        "fn main() -> I64 uses { Process } = { List.len(Env.argv()) }\n",
        0,
    )


def test_for_in_snapshots_length(tmp_path: Path) -> None:
    # for-in takes the length at loop entry on BOTH backends: pushes inside
    # the body are not visited (and can't make the loop infinite).
    _both(
        tmp_path,
        "fn main() -> I64 = {\n"
        "  let xs = [1, 2]\n  mut seen = 0\n"
        "  for x in xs {\n    seen = seen + 1\n    List.push(xs, x * 10)\n  }\n"
        "  seen * 10 + List.len(xs)\n}\n",
        24,  # 2 visits, len 4
    )


# --- review findings ---------------------------------------------------------------------


def test_generics_over_list_types(tmp_path: Path) -> None:
    # MONO002 misfire: the monomorphizer never learned ListType in M3.
    _both(
        tmp_path,
        "fn id<T>(x: T) -> T = { x }\n"
        "fn first<T>(xs: List<T>) -> T = { xs[0] }\n"
        "fn main() -> I64 = { let xs = id([1, 2, 3])\n first(xs) + first([[7]])[0] }\n",
        8,
    )


def test_panic_fails_one_test_not_the_suite(tmp_path: Path, capfd) -> None:
    # A runtime panic is attributed to ITS test; the suite continues and the
    # summary prints — identically on both backends (setjmp/longjmp natively).
    src = (
        "fn main() -> I64 = { 0 }\n"
        'test "oob" { let xs = [1]\n assert_eq(xs[5], 1) }\n'
        'test "still runs" { assert(true) }\n'
    )
    path = _write(tmp_path, src)
    assert driver.cmd_test(path, interpret=True) == 1
    out = capfd.readouterr().out
    assert "  runtime error: index 5 out of bounds (len 1)" in out
    assert "fail Main / oob" in out
    assert "ok Main / still runs" in out
    assert "1 passed, 1 failed" in out
    if _tools_available():
        assert driver.cmd_test(path, native=True) == 1
        assert capfd.readouterr().out == out


def test_main_return_type_rule_both_backends(tmp_path: Path, capfd) -> None:
    # Was native-only (RUN002 in the shim): the interpreter ran what native refused.
    path = _write(tmp_path, "fn main() -> List<I64> = { [1] }\n")
    assert driver.cmd_run(path, interpret=True) == 1
    assert "must return I64 or Unit" in capfd.readouterr().err
    if _tools_available():
        assert driver.cmd_run(path, native=True) == 1
        assert "must return I64 or Unit" in capfd.readouterr().err


def test_mut_annotation_is_reassignment_context(tmp_path: Path) -> None:
    _both(
        tmp_path,
        "fn main() -> I64 = {\n"
        "  mut r: Result<I64, String> = Ok(1)\n"
        '  r = Err("boom")\n'
        "  mut xs: List<I64> = [1]\n"
        "  xs = []\n"
        "  match r { Ok(n) => n  Err(e) => 7 + List.len(xs) }\n}\n",
        7,
    )


def test_user_type_shadows_intrinsic_module(tmp_path: Path) -> None:
    # A user ADT named Str/Env/Log wins over the intrinsic module namespace.
    _both(
        tmp_path,
        "type Str = | Wrap(I64)\nfn main() -> I64 = { match Str.Wrap(5) { Wrap(n) => n } }\n",
        5,
    )


def test_read_line_truncates_at_nul(tmp_path: Path) -> None:
    # Strings are NUL-terminated: the stored length must agree with the extent
    # strlen-based ops see, so read_line cuts at the first NUL.
    src = (
        "module Main\nimport Std.IO\nimport Std.Str\n"
        "fn main() -> I64 uses { Fs } = {\n"
        "  match read_line() { Some(l) => length(l)  None => 99 }\n}\n"
    )
    path = _write(tmp_path, src)
    proc = subprocess.run(
        [sys.executable, "-m", "flx", "run", path],
        input="ab\x00cdef\n",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


# --- for-in shape errors ----------------------------------------------------------------


def test_for_requires_list() -> None:
    diags = _diag("fn main() -> I64 = { for x in 5 { }\n 0 }\n")
    assert any(d.code == "TYPE021" for d in diags)


def test_list_op_arity_and_type_errors() -> None:
    diags = _diag("fn main() -> I64 = { List.push([1])\n 0 }\n")
    assert any(d.code == "TYPE005" for d in diags)
    diags = _diag('fn main() -> I64 = { List.push([1], "s")\n 0 }\n')
    assert any(d.code == "TYPE003" for d in diags)
    diags = _diag("fn main() -> I64 = { List.reverse([1])\n 0 }\n")
    assert any(d.code == "TYPE010" for d in diags)
