"""Milestone 2 from the blind study: ADTs made whole — recursive types,
multi-field payloads, boxed native payloads, match ergonomics, and the
statement-position if/else relaxation."""

from __future__ import annotations

import os
import shutil
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


def _both_backends(tmp_path: Path, src: str, code: int, out: str | None = None) -> None:
    """Run on the interpreter and (when available) natively; both must agree."""
    path = _write(tmp_path, src)
    capfd = _both_backends.capfd  # set by the fixture wrapper below
    assert driver.cmd_run(path, interpret=True) == code
    interp_out = capfd.readouterr().out
    if out is not None:
        assert interp_out == out
    if _tools_available():
        assert driver.cmd_run(path, native=True) == code
        assert capfd.readouterr().out == interp_out


@pytest.fixture(autouse=True)
def _wire_capfd(capfd: pytest.CaptureFixture[str]) -> None:
    _both_backends.capfd = capfd  # type: ignore[attr-defined]


# --- recursive and mutually recursive types --------------------------------------


def test_recursive_adt(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "type N = | Zero | Succ(N)\n"
        "fn depth(n: N) -> I64 = { match n { Zero => 0  Succ(m) => 1 + depth(m) } }\n"
        "fn main() -> I64 = { depth(Succ(Succ(Succ(Zero)))) }\n",
        3,
    )


def test_mutually_recursive_adts(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "type Even = | Z | SuccE(Odd)\n"
        "type Odd = | SuccO(Even)\n"
        "fn evens(e: Even) -> I64 = { match e { Z => 0  SuccE(o) => 1 + odds(o) } }\n"
        "fn odds(o: Odd) -> I64 = { match o { SuccO(e) => 1 + evens(e) } }\n"
        "fn main() -> I64 = { evens(SuccE(SuccO(SuccE(SuccO(Z))))) }\n",
        4,
    )


def test_generic_recursive_adt(tmp_path: Path) -> None:
    # Nested constructors infer through the expected type AND structurally
    # (Chain<T> against Chain<I64> binds T) without annotations.
    _both_backends(
        tmp_path,
        "type Chain<T> = | End(T) | Link(Chain<T>)\n"
        "fn len(c: Chain<I64>) -> I64 = "
        "{ match c { End(v) => v  Link(rest) => 1 + len(rest) } }\n"
        "fn main() -> I64 = { let c = Link(Link(End(40)))\n len(c) }\n",
        42,
    )


def test_deep_recursion_native(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "type N = | Zero | Succ(N)\n"
        "fn mk(n: I64) -> N = { if n == 0 { Zero } else { Succ(mk(n - 1)) } }\n"
        "fn depth(n: N) -> I64 = { match n { Zero => 0  Succ(m) => 1 + depth(m) } }\n"
        "fn main() -> I64 = { depth(mk(200)) % 251 }\n",
        200,
    )


def test_many_multi_field_boxes_no_heap_corruption(tmp_path: Path) -> None:
    # Regression: heap-box sizes are folded from sizeof GEPs at mlir-translate
    # time, which must use the HOST data layout (toolchain attaches it to the
    # module). Under LLVM's default layout the folded size of a two-field
    # {i32,i64}-payload box was 24 while the store wrote 32 — glibc malloc
    # aborted after a couple of allocations. Build a whole expression tree so
    # any size lie corrupts the heap loudly.
    _both_backends(
        tmp_path,
        "module Main\nimport Std.IO\n"
        "type Expr = | Num(I64) | Add(Expr, Expr) | Mul(Expr, Expr)\n"
        "fn eval(e: Expr) -> I64 = { match e {\n"
        "  Num(n) => n\n"
        "  Add(a, b) => eval(a) + eval(b)\n"
        "  Mul(a, b) => eval(a) * eval(b)\n} }\n"
        "fn tree(n: I64) -> Expr = "
        "{ if n == 0 { Num(1) } else { Add(tree(n - 1), Mul(Num(1), tree(n - 1))) } }\n"
        "fn main() -> I64 uses { Log } = {\n"
        "  let total = eval(tree(6))\n"
        "  println(to_str(total))\n"
        "  total % 251\n}\n",
        64,
        out="64\n",
    )


# --- multi-field payloads ---------------------------------------------------------


def test_multi_field_payload(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "type Shape = | Circle(I64) | Rect(I64, I64) | Dot\n"
        "fn area(s: Shape) -> I64 = { match s {\n"
        "  Circle(r) => r * r * 3\n  Rect(w, h) => w * h\n  Dot => 0\n} }\n"
        "fn main() -> I64 = { area(Rect(3, 4)) + area(Circle(1)) + area(Dot) }\n",
        15,
    )


def test_mixed_string_int_payload(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "module Main\nimport Std.IO\n"
        "type Entry = | KV(String, I64) | Empty\n"
        "fn main() -> I64 uses { Log } = {\n"
        '  match KV("answer", 42) {\n'
        '    KV(k, v) => { println(k ++ "=" ++ to_str(v))  v }\n'
        "    Empty => 0\n  }\n}\n",
        42,
        out="answer=42\n",
    )


def test_ctor_arity_checked() -> None:
    diags = _diag(
        "type Shape = | Rect(I64, I64)\n"
        "fn main() -> I64 = { match Rect(1) { Rect(w, h) => w  _ => 0 } }\n"
    )
    assert any(d.code == "TYPE005" for d in diags)


# --- boxed native payloads (strings, records, ADTs) -------------------------------


def test_string_payload(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "module Main\nimport Std.IO\nimport Std.Str\n"
        "type E = | Msg(String) | Code(I64)\n"
        "fn describe(e: E) -> String = { match e {\n"
        '  Msg(s) => "msg: " ++ s\n  Code(n) => "code: " ++ to_str(n)\n} }\n'
        "fn main() -> I64 uses { Log } = {\n"
        '  println(describe(Msg("boom")))\n  println(describe(Code(42)))\n'
        '  length(describe(Msg("xyz")))\n}\n',
        8,
        out="msg: boom\ncode: 42\n",
    )


def test_record_payload(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "type P = { x: I64, y: I64 }\n"
        "type Shape = | Box2(P, P) | Pt(P)\n"
        "fn area(s: Shape) -> I64 = { match s {\n"
        "  Box2(a, b) => (b.x - a.x) * (b.y - a.y)\n  Pt(p) => 0\n} }\n"
        "fn main() -> I64 = { area(Box2({ x = 1, y = 1 }, { x = 5, y = 4 })) }\n",
        12,
    )


def test_try_on_string_result(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "module Main\nimport Std.Str\n"
        "fn fetch(ok: Bool) -> Result<String, I64> = "
        '{ if ok { Ok("payload") } else { Err(7) } }\n'
        "fn use_it(ok: Bool) -> Result<I64, I64> = {\n"
        "  let s = fetch(ok)?\n  Ok(length(s))\n}\n"
        "fn main() -> I64 = { match use_it(true) { Ok(n) => n  Err(e) => e } }\n",
        7,
    )


def test_eq_on_boxed_payload_rejected() -> None:
    # A boxed RECORD payload would compare as a pointer natively; the checker
    # says no. (Single String payloads compare by CONTENT since M8.)
    diags = _diag(
        "type P = { x: I64 }\n"
        "type E = | Wrap(P) | Nil\n"
        "fn main() -> I64 = {\n"
        "  let a = Wrap({ x = 1 })\n"
        "  if a == a { 0 } else { 1 }\n}\n"
    )
    assert any(d.code == "TYPE019" for d in diags)


def test_eq_on_string_payload_compares_content(tmp_path: Path) -> None:
    # M8: single String payloads ride structural equality, by content.
    src = (
        "type E = | Msg(String) | Nil\n"
        'fn main() -> I64 = { if Msg("a") == Msg("a") { 0 } else { 1 } }\n'
    )
    flx = tmp_path / "main.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_run(str(flx), interpret=True) == 0


# --- match ergonomics --------------------------------------------------------------


def test_nested_patterns(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "type N = | Zero | Succ(N)\n"
        "fn f(n: N) -> I64 = { match n {\n"
        "  Zero => 0\n  Succ(Zero) => 1\n  Succ(Succ(m)) => 2\n  Succ(m) => 99\n} }\n"
        "fn main() -> I64 = "
        "{ f(Zero) * 100 + f(Succ(Zero)) * 10 + f(Succ(Succ(Zero))) }\n",
        12,
    )


def test_literal_patterns(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "type Opt = | Got(I64) | Nope\n"
        "fn f(o: Opt) -> I64 = { match o {\n"
        "  Got(0) => 100\n  Got(-3) => 300\n  Got(n) => n\n  Nope => -1\n} }\n"
        "fn main() -> I64 = { f(Got(0)) + f(Got(-3)) + f(Got(7)) + f(Nope) }\n",
        150,  # 406 & 0xFF
    )


def test_bool_literal_patterns(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "type P = | Pair(Bool, I64)\n"
        "fn f(p: P) -> I64 = { match p {\n"
        "  Pair(true, n) => n\n  Pair(false, 0) => -100\n  Pair(_, n) => -n\n} }\n"
        "fn main() -> I64 = { f(Pair(true, 5)) + f(Pair(false, 3)) + f(Pair(false, 0)) }\n",
        158,  # -98 & 0xFF
    )


def test_block_arm_bodies(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "type C = | A | B\n"
        "fn main() -> I64 = { match A {\n"
        "  A => {\n    let x = 3\n    x * 2\n  }\n  B => 0\n} }\n",
        6,
    )


def test_nested_pattern_does_not_cover(tmp_path: Path) -> None:
    diags = _diag(
        "type N = | Zero | Succ(N)\n"
        "fn main() -> I64 = { match Succ(Zero) { Zero => 0  Succ(Zero) => 1 } }\n"
    )
    assert any(d.code == "MATCH001" and "catch-all" in (d.help or "") for d in diags)


def test_refutable_arm_after_full_coverage_unreachable() -> None:
    diags = _diag(
        "type N = | Zero | Succ(N)\n"
        "fn main() -> I64 = "
        "{ match Zero { Zero => 0  Succ(m) => 1  Succ(Zero) => 2 } }\n"
    )
    assert any(d.code == "MATCH002" and "unreachable" in d.message for d in diags)


def test_string_literal_pattern_rejected() -> None:
    with pytest.raises(FlexError) as exc:
        parse('type E = | M(String)\nfn f(e: E) -> I64 = { match e { M("a") => 0  _ => 1 } }')
    assert any("string literal patterns" in d.message for d in exc.value.diagnostics)


def test_match_arms_still_strict_in_value_position() -> None:
    diags = _diag(
        'type C = | A | B\nfn main() -> I64 = { let x = match A { A => 1  B => "s" }\n 0 }\n'
    )
    assert any(d.code == "TYPE008" for d in diags)


def test_derive_show_multi_field(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "module Main\nimport Std.IO\n"
        "derive(Show) type Shape = | Circle(I64) | Rect(I64, I64) | Dot\n"
        "fn main() -> I64 uses { Log } = {\n"
        "  println(Rect(2, 3).show())\n  println(Dot.show())\n  0\n}\n",
        0,
        out="Rect(2, 3)\nDot\n",
    )


# --- statement-position if/else ----------------------------------------------------


def test_statement_if_branches_need_not_match(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "module Main\nimport Std.IO\n"
        "fn main() -> I64 uses { Log } = {\n"
        '  if 2 > 1 { println("yes") } else { 0 }\n'
        "  7\n}\n",
        7,
        out="yes\n",
    )


def test_unit_fn_tail_if_relaxed(tmp_path: Path) -> None:
    _both_backends(
        tmp_path,
        "module Main\nimport Std.IO\n"
        "fn report(n: I64) -> Unit uses { Log } = "
        '{ if n > 0 { println("pos") } else { n } }\n'
        "fn main() -> I64 uses { Log } = { report(1)\n report(-1)\n 0 }\n",
        0,
        out="pos\n",
    )


def test_value_position_if_still_strict() -> None:
    diags = _diag('fn main() -> I64 = { let x = if true { 1 } else { "s" }\n 0 }\n')
    assert any(d.code == "TYPE008" for d in diags)
