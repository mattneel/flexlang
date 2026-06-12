"""M8 adversarial-review findings, pinned: the silent assert-through-impl
no-op, the for-in/pop interpreter ICE, derive helper-name collisions,
Option/Result field derives, Map diagnostics polish, and hint honesty."""

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


def _test_cmd(path: str, backend: str = "interp") -> tuple[int, bytes]:
    args = [sys.executable, "-m", "flx", "test", path]
    if backend == "native":
        args.insert(4, "--native")
    proc = subprocess.run(args, capture_output=True)
    return proc.returncode, proc.stdout


def _run(path: str, backend: str = "interp") -> tuple[int, bytes, bytes]:
    args = [sys.executable, "-m", "flx", "run", path]
    if backend == "native":
        args.insert(4, "--native")
    proc = subprocess.run(args, capture_output=True)
    return proc.returncode, proc.stdout, proc.stderr


def _check_proc(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "flx", "check", path], capture_output=True, text=True
    )


def _diag(src: str) -> list:
    with pytest.raises(FlexError) as exc:
        check_and_monomorphize(expand(parse(src)))
    return exc.value.diagnostics


# --- the silent assert-through-impl no-op (critical) -----------------------------------

FAILING_ASSERTS = """\
module Main
import Std.Str

derive(Eq) type Person = { name: String, age: I64 }

test "unequal must fail" {
  let a: Person = { name = "ann", age = 3 }
  let b: Person = { name = "bob", age = 3 }
  assert_eq(a, b)
}

test "equal must pass" {
  let a: Person = { name = "ann", age = 3 }
  let b: Person = { name = "ann", age = 3 }
  assert_eq(a, b)
}

test "assert_ne on equal must fail" {
  let a: Person = { name = "ann", age = 3 }
  assert_ne(a, a)
}
"""


def test_impl_routed_asserts_actually_assert(tmp_path: Path) -> None:
    path = _write(tmp_path, FAILING_ASSERTS)
    code, out = _test_cmd(path)
    assert code == 1
    assert b"fail Main / unequal must fail" in out
    assert b"ok Main / equal must pass" in out
    assert b"fail Main / assert_ne on equal must fail" in out
    assert b"1 passed, 2 failed" in out


@native
def test_impl_routed_asserts_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, FAILING_ASSERTS)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


LIAR_IMPL = """\
module Main

type Box = { s: String }

impl Eq for Box = {
  fn eq(self: Box, other: Box) -> Bool = { false }
}

test "a lying impl is honored" {
  let a: Box = { s = "same" }
  assert_eq(a, a)
}
"""


def test_custom_impl_semantics_are_honored(tmp_path: Path) -> None:
    # assert_eq must call the USER's impl, even when it disagrees with
    # structural equality — on both backends identically.
    path = _write(tmp_path, LIAR_IMPL)
    code, out = _test_cmd(path)
    assert code == 1
    assert b"fail Main / a lying impl is honored" in out


@native
def test_custom_impl_semantics_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, LIAR_IMPL)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


# --- for-in + pop ICE (critical) ---------------------------------------------------------

POP_IN_FORIN = """\
import Std.IO

fn main() -> I64 uses { Log } = {
  mut xs = [1, 2, 3, 4]
  for x in xs {
    print("visit " ++ to_str(x))
    match List.pop(xs) {
      Some(v) => print(" popped " ++ to_str(v) ++ "\\n")
      None => print(" none\\n")
    }
  }
  0
}
"""


def test_pop_during_forin_panics_cleanly(tmp_path: Path) -> None:
    path = _write(tmp_path, POP_IN_FORIN)
    code, _, err = _run(path)
    assert code == 1
    assert b"flx: runtime error: index 2 out of bounds (len 2)" in err
    assert b"Traceback" not in err


@native
def test_pop_during_forin_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, POP_IN_FORIN)
    assert _run(path, "interp") == _run(path, "native")


def test_pop_during_forin_in_tests_recovers(tmp_path: Path) -> None:
    src = (
        "import Std.IO\n"
        'test "shrinks" {\n'
        "  mut xs = [1, 2, 3, 4]\n"
        "  for x in xs { let _p = List.pop(xs)\n  }\n"
        "}\n"
        'test "still runs" { assert_eq(1, 1) }\n'
    )
    path = _write(tmp_path, src)
    code, out = _test_cmd(path)
    assert code == 1
    assert b"fail" in out and b"ok Main / still runs" in out
    assert b"1 passed, 1 failed" in out


# --- derive fixes ---------------------------------------------------------------------------

ENC_COLLISION = """\
module Main
import Std.Str
import Std.IO

derive(Eq, Show) type Foo = { s: String }
derive(Eq, Show) type FOO = { t: String }

derive(Eq) type HoldA = { xs: List<Foo> }
derive(Eq) type HoldB = { ys: List<FOO> }

fn b(v: Bool) -> String = { if v { "T" } else { "F" } }

fn main() -> I64 uses { Log } = {
  let a1: HoldA = { xs = [{ s = "x" }] }
  let a2: HoldA = { xs = [{ s = "x" }] }
  let c1: HoldB = { ys = [{ t = "x" }] }
  let c2: HoldB = { ys = [{ t = "y" }] }
  println(b(a1.eq(a2)) ++ b(c1.eq(c2)))
  0
}
"""


def test_enc_collision_fixed(tmp_path: Path) -> None:
    # Foo and FOO must get DISTINCT generated list helpers.
    path = _write(tmp_path, ENC_COLLISION)
    code, out, err = _run(path)
    assert code == 0, err.decode(errors="replace")
    assert out == b"TF\n"


OPTION_FIELDS = """\
module Main
import Std.Str

derive(Eq, Show) type Inner = { s: String }
derive(Eq, Show) type Holder = {
  o: Option<I64>,
  r: Result<I64, String>,
  chain: Option<Inner>,
  u: Unit
}

test "option and result fields derive eq" {
  let a: Holder = { o = Some(1), r = Err("no"), chain = Some({ s = "x" }), u = () }
  let b: Holder = { o = Some(1), r = Err("no"), chain = Some({ s = "x" }), u = () }
  let c: Holder = { o = Some(1), r = Err("no"), chain = Some({ s = "y" }), u = () }
  let d: Holder = { o = None, r = Ok(0), chain = None, u = () }
  assert_eq(a, b)
  assert_ne(a, c)
  assert_ne(a, d)
}

test "option and result fields derive show" {
  let a: Holder = { o = Some(1), r = Err("no"), chain = None, u = () }
  assert_eq(
    a.show(),
    "Holder { o = Some(1), r = Err(no), chain = None, u = () }"
  )
}
"""


def test_option_result_fields_derive(tmp_path: Path) -> None:
    path = _write(tmp_path, OPTION_FIELDS)
    code, out = _test_cmd(path)
    assert code == 0, out.decode(errors="replace")


@native
def test_option_result_fields_parity(tmp_path: Path) -> None:
    path = _write(tmp_path, OPTION_FIELDS)
    assert _test_cmd(path, "interp") == _test_cmd(path, "native")


# --- diagnostics polish -----------------------------------------------------------------------


def test_bad_map_annotation_single_error(tmp_path: Path) -> None:
    # The rejected annotation must not cascade into "annotate the binding".
    proc = _check_proc(
        _write(tmp_path, "fn main() -> I64 = {\n  let m: Map<I64, String> = Map.new()\n  0\n}\n")
    )
    assert proc.returncode == 1
    assert "Map keys are String" in proc.stderr
    assert "TYPE023" not in proc.stderr


def test_match_on_error_scrutinee_no_cascade(tmp_path: Path) -> None:
    src = (
        "fn main() -> I64 = {\n"
        "  let xs = [1, 2]\n"
        '  match Map.get(xs, "k") { Some(n) => n  None => 0 }\n}\n'
    )
    proc = _check_proc(_write(tmp_path, src))
    assert proc.returncode == 1
    assert "operates on a Map" in proc.stderr
    assert "<error>" not in proc.stderr
    assert "MATCH003" not in proc.stderr


def test_map_indexing_hint(tmp_path: Path) -> None:
    src = 'fn main() -> I64 = {\n  let m: Map<String, I64> = Map.new()\n  m["a"]\n}\n'
    proc = _check_proc(_write(tmp_path, src))
    assert proc.returncode == 1
    assert "use Map.get(m, key)" in proc.stderr
    assert "expected I64" not in proc.stderr  # no misleading index-type error


def test_pop_hint_headline_is_honest() -> None:
    diags = _diag("fn main() -> I64 = {\n  let xs = [1]\n  pop(xs)\n  0\n}\n")
    msgs = [d.message for d in diags if d.code == "NAME001"]
    assert any("Flex spells it differently" in m for m in msgs)
    assert not any("does not have" in m for m in msgs)
