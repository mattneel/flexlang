"""Milestone 8: the associative layer — Map<String, V> on both backends,
List.pop, sort/sort_by/sort_with, and derive(Eq/Show) for composites
(String-carrying ADTs, nested records, List fields). Differential: both
backends byte-identical."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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


def _run(path: str, backend: str = "interp") -> tuple[int, bytes, bytes]:
    args = [sys.executable, "-m", "flx", "run", path]
    if backend == "native":
        args.insert(4, "--native")
    proc = subprocess.run(args, capture_output=True)
    return proc.returncode, proc.stdout, proc.stderr


def _test_cmd(path: str, backend: str = "interp") -> tuple[int, bytes]:
    args = [sys.executable, "-m", "flx", "test", path]
    if backend == "native":
        args.insert(4, "--native")
    proc = subprocess.run(args, capture_output=True)
    return proc.returncode, proc.stdout


def _diag(src: str) -> list:
    with pytest.raises(FlexError) as exc:
        check_and_monomorphize(expand(parse(src)))
    return exc.value.diagnostics


# --- Map<String, V> ---------------------------------------------------------------

MAP_TESTS = """\
module Main
import Std.Str
import Std.List

test "set, get, has, len, remove" {
  let m: Map<String, I64> = Map.new()
  Map.set(m, "one", 1)
  Map.set(m, "two", 2)
  assert_eq(Map.get(m, "one"), Some(1))
  assert_eq(Map.get(m, "missing"), None)
  assert(Map.has(m, "two"))
  assert(!Map.has(m, "三"))
  assert_eq(Map.len(m), 2)
  Map.remove(m, "one")
  assert_eq(Map.len(m), 1)
  assert_eq(Map.get(m, "one"), None)
  Map.remove(m, "never-there")
  assert_eq(Map.len(m), 1)
}

test "replace keeps position, re-insert appends" {
  let m: Map<String, I64> = Map.new()
  Map.set(m, "a", 1)
  Map.set(m, "b", 2)
  Map.set(m, "c", 3)
  Map.set(m, "a", 11)
  Map.remove(m, "b")
  Map.set(m, "b", 22)
  let ks = Map.keys(m)
  assert_eq(List.len(ks), 3)
  assert_eq(ks[0], "a")
  assert_eq(ks[1], "c")
  assert_eq(ks[2], "b")
  let vs = Map.values(m)
  assert_eq(vs[0], 11)
  assert_eq(vs[1], 3)
  assert_eq(vs[2], 22)
}

test "string values and raw-byte keys" {
  let m: Map<String, String> = Map.new()
  Map.set(m, "café", "au lait")
  Map.set(m, "\\xff", "raw byte key")
  assert_eq(Map.get(m, "café"), Some("au lait"))
  assert_eq(Map.get(m, "\\xff"), Some("raw byte key"))
  assert_eq(Map.get(m, "cafe"), None)
}

test "reference semantics" {
  let m: Map<String, I64> = Map.new()
  let alias = m
  Map.set(alias, "k", 1)
  assert_eq(Map.get(m, "k"), Some(1))
}

test "maps of lists and float values" {
  let groups: Map<String, List<I64>> = Map.new()
  Map.set(groups, "evens", [2, 4])
  match Map.get(groups, "evens") {
    Some(xs) => {
      List.push(xs, 6)
      assert_eq(List.len(xs), 3)
    }
    None => { fail("missing group") }
  }
  let floats: Map<String, F64> = Map.new()
  Map.set(floats, "pi", 3.14)
  assert_eq(Map.get(floats, "pi"), Some(3.14))
}

test "empty map" {
  let m: Map<String, I64> = Map.new()
  assert_eq(Map.len(m), 0)
  assert_eq(List.len(Map.keys(m)), 0)
  assert_eq(Map.get(m, ""), None)
  Map.set(m, "", 0)
  assert_eq(Map.get(m, ""), Some(0))
}
"""


def test_map_suite_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, MAP_TESTS)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_map_suite_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, MAP_TESTS)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


def test_map_new_needs_context(tmp_path: Path) -> None:
    diags = _diag("fn main() -> I64 = { let m = Map.new()\n  0 }\n")
    assert any(d.code == "TYPE023" and "Map" in (d.help or "") for d in diags)


def test_map_key_must_be_string() -> None:
    diags = _diag("fn main() -> I64 = { let m: Map<I64, I64> = Map.new()\n  0 }\n")
    assert any("Map keys are String" in d.message for d in diags)


def test_map_passes_through_generic_fn(tmp_path: Path) -> None:
    # MapType must survive monomorphization (the MONO002 hole the scout found).
    src = (
        "module Main\nimport Std.Str\n"
        "fn first_of<T>(m: Map<String, T>, k: String, d: T) -> T = {\n"
        "  match Map.get(m, k) { Some(v) => v  None => d }\n}\n"
        "fn main() -> I64 = {\n"
        "  let m: Map<String, I64> = Map.new()\n"
        '  Map.set(m, "x", 7)\n'
        '  first_of(m, "x", 0)\n}\n'
    )
    path = _write(tmp_path, src)
    assert _run(path)[0] == 7


def test_map_equality_is_rejected() -> None:
    diags = _diag(
        "fn main() -> I64 = {\n"
        "  let a: Map<String, I64> = Map.new()\n"
        "  let b: Map<String, I64> = Map.new()\n"
        "  if a == b { 1 } else { 0 }\n}\n"
    )
    assert any(d.code == "TYPE019" for d in diags)


def test_type_named_map_is_rejected() -> None:
    diags = _diag("type Map = | Empty\nfn main() -> I64 = { 0 }\n")
    assert any(d.code == "TYPE002" and "builtin" in d.message for d in diags)


# --- List.pop ---------------------------------------------------------------------

POP_TESTS = """\
module Main
import Std.Str

test "pop returns Some(last) then None" {
  mut xs = [1, 2]
  assert_eq(List.pop(xs), Some(2))
  assert_eq(List.pop(xs), Some(1))
  assert_eq(List.pop(xs), None)
  assert_eq(List.len(xs), 0)
}

test "pop boxed elements" {
  mut ss = ["a", "bé"]
  assert_eq(List.pop(ss), Some("bé"))
  assert_eq(List.pop(ss), Some("a"))
  assert_eq(List.pop(ss), None)
}

test "pop floats" {
  mut fs = [1.5]
  assert_eq(List.pop(fs), Some(1.5))
  assert_eq(List.pop(fs), None)
}

test "push after pop reuses the slot" {
  mut xs = [1, 2, 3]
  let popped = List.pop(xs)
  assert_eq(popped, Some(3))
  List.push(xs, 30)
  assert_eq(xs[2], 30)
  assert_eq(List.len(xs), 3)
}
"""


def test_pop_suite_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, POP_TESTS)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_pop_suite_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, POP_TESTS)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


# --- sort family ------------------------------------------------------------------

SORT_TESTS = """\
module Main
import Std.List
import Std.Str

fn neg(n: I64) -> I64 = { 0 - n }

test "sort ascending" {
  mut xs = [3, 1, 2, -5, 3]
  sort(xs)
  assert_eq(xs[0], -5)
  assert_eq(xs[4], 3)
}

test "sort_by key, stable" {
  mut words = ["ccc", "a", "bb", "x"]
  sort_by(words, length)
  assert_eq(words[0], "a")
  assert_eq(words[1], "x")
  assert_eq(words[3], "ccc")
}

test "sort_by descending via key" {
  mut xs = [1, 3, 2]
  sort_by(xs, neg)
  assert_eq(xs[0], 3)
  assert_eq(xs[2], 1)
}

test "sort_with comparator" {
  mut words = ["pear", "apple", "fig"]
  sort_with(words, str_lt)
  assert_eq(words[0], "apple")
  assert_eq(words[2], "pear")
}

test "edge cases" {
  mut empty: List<I64> = []
  sort(empty)
  assert_eq(List.len(empty), 0)
  mut one = [42]
  sort(one)
  assert_eq(one[0], 42)
}
"""


def test_sort_suite_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, SORT_TESTS)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_sort_suite_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, SORT_TESTS)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


# --- derive for composites ---------------------------------------------------------

DERIVE_TESTS = """\
module Main
import Std.Str

derive(Eq, Show) type Inner = { s: String }
derive(Eq, Show) type Outer = { i: Inner, n: I64 }
derive(Eq, Show) type Tok = | Word(String) | Num(I64) | End
derive(Eq, Show) type Bag = { xs: List<I64>, names: List<String> }
derive(Eq, Show) type Deep = { rows: List<List<I64>> }

test "nested records compose through eq impls" {
  let a: Outer = { i = { s = "x" }, n = 1 }
  let b: Outer = { i = { s = "x" }, n = 1 }
  let c: Outer = { i = { s = "y" }, n = 1 }
  assert(a.eq(b))
  assert(!a.eq(c))
  assert_eq(a, b)
  assert_ne(a, c)
}

test "string-carrying ADTs derive Eq" {
  assert(Word("hi").eq(Word("hi")))
  assert(!Word("hi").eq(Word("ho")))
  assert(!Word("hi").eq(Num(7)))
  assert(!Num(7).eq(End))
  assert(End.eq(End))
  assert_eq(Word("hi"), Word("hi"))
  assert_ne(Word("hi"), Num(7))
}

test "list fields compare element-wise" {
  let g: Bag = { xs = [1, 2], names = ["a", "bé"] }
  let h: Bag = { xs = [1, 2], names = ["a", "bé"] }
  let k: Bag = { xs = [1, 2], names = ["a", "b"] }
  let shorter: Bag = { xs = [1], names = ["a", "bé"] }
  assert_eq(g, h)
  assert_ne(g, k)
  assert_ne(g, shorter)
}

test "nested lists derive too" {
  let a: Deep = { rows = [[1, 2], [3]] }
  let b: Deep = { rows = [[1, 2], [3]] }
  let c: Deep = { rows = [[1, 2], [4]] }
  assert_eq(a, b)
  assert_ne(a, c)
}

test "show renders composites" {
  let o: Outer = { i = { s = "x" }, n = 1 }
  assert_eq(o.show(), "Outer { i = Inner { s = x }, n = 1 }")
  assert_eq(Num(7).show(), "Num(7)")
  assert_eq(End.show(), "End")
  let d: Deep = { rows = [[1], []] }
  assert_eq(d.show(), "Deep { rows = [[1], []] }")
}
"""


def test_derive_suite_interp(tmp_path: Path) -> None:
    path = _write(tmp_path, DERIVE_TESTS)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_derive_suite_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, DERIVE_TESTS)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


def test_derive_eq_without_std_str_still_diagnoses() -> None:
    diags = _diag(
        "derive(Eq) type P = { name: String }\n"
        'fn main() -> I64 = { let p: P = { name = "x" }\n  0 }\n'
    )
    assert any(d.code == "DISP001" for d in diags)


def test_derive_map_field_is_explained() -> None:
    diags = _diag("derive(Eq) type C = { m: Map<String, I64> }\nfn main() -> I64 = { 0 }\n")
    assert any("reference semantics" in d.message for d in diags)


def test_assert_eq_hint_names_derive() -> None:
    diags = _diag(
        "type P = { s: String }\n"
        'fn main() -> I64 = { let a: P = { s = "x" }\n'
        "  assert_eq(a, a)\n  0 }\n"
    )
    # assert_eq only works in tests, but the TYPE019 help should still name derive
    assert any("derive(Eq)" in (d.help or "") for d in diags)


# --- the discoverability hints -------------------------------------------------------


def test_dict_hint_names_map() -> None:
    diags = _diag("fn main() -> I64 = { let d = dict()\n  0 }\n")
    assert any("Map" in (d.help or "") for d in diags)


def test_sort_hint_is_an_import_now() -> None:
    diags = _diag("fn main() -> I64 = { sort([3, 1])\n  0 }\n")
    assert any("`import Std.List` provides sort" in (d.help or "") for d in diags)
