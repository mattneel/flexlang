"""Adversarial-review findings for M2 (25 confirmed): builtin shadowing, ctor
namespace collisions, polymorphic recursion, literal-pattern ranges, the native
stack guard, generic inference through constructors, divergence analysis, and
match reachability."""

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


# --- builtin shadowing + the constructor namespace --------------------------------


def test_user_result_rejected_not_ice() -> None:
    # `type Result` used to silently replace the builtin; `?` then ICEd on the
    # missing type args. Now the declaration itself is the error.
    diags = _diag("type Result = | Yes | No\nfn main() -> I64 = { 0 }\n")
    assert any(d.code == "TYPE002" and "builtin" in d.message for d in diags)


def test_try_on_zero_arg_result_never_unpacks() -> None:
    # Even with the TYPE002, `?` must not crash mid-check on a malformed Result.
    diags = _diag(
        "type Result = | Yes | No\n"
        "fn main() -> I64 = { 0 }\n"
        'test "t" { let r = Yes\n let x = r? }\n'
    )
    assert all(isinstance(d.code, str) for d in diags)  # diagnostics, no ICE


def test_primitive_type_names_rejected() -> None:
    diags = _diag("type I64 = | A\nfn main() -> I64 = { 0 }\n")
    assert any(d.code == "TYPE002" and "builtin" in d.message for d in diags)


def test_duplicate_variant_in_one_adt_rejected() -> None:
    diags = _diag("type D = | X(I64) | X(Bool) | Y\nfn main() -> I64 = { 0 }\n")
    assert any("declared twice" in d.message for d in diags)


def test_ctor_collision_across_adts_rejected() -> None:
    diags = _diag("type B = | Hit | Q\ntype C = | Hit(Bool)\nfn main() -> I64 = { 0 }\n")
    assert any("already defined by type 'B'" in d.message for d in diags)


def test_ctor_collision_with_builtin_rejected() -> None:
    diags = _diag("type A = | Some(I64) | Other\nfn main() -> I64 = { 0 }\n")
    assert any("already defined by type 'Option'" in d.message for d in diags)


# --- polymorphic recursion ----------------------------------------------------------


def test_polymorphic_recursion_clean_diagnostic() -> None:
    # Used to recurse the checker to death (RecursionError masked as PAR003).
    diags = _diag(
        "type Bad<T> = | Leaf | Wrap(Bad<Option<T>>)\n"
        "fn use_bad(b: Bad<I64>) -> I64 = { 0 }\n"
        "fn main() -> I64 = { 0 }\n"
    )
    assert any(d.code == "TYPE024" for d in diags)


# --- literal pattern ranges ----------------------------------------------------------


def test_literal_pattern_range_checked() -> None:
    diags = _diag(
        "type O = | G(I64) | N\n"
        "fn main() -> I64 = { match G(1) { G(9223372036854775808) => 1  _ => 0 } }\n"
    )
    assert any(d.code == "TYPE011" for d in diags)


def test_int64_min_works_in_both_positions(tmp_path: Path) -> None:
    src = (
        "type O = | G(I64) | N\n"
        "fn main() -> I64 = {\n"
        "  let m = -9223372036854775808\n"
        "  match G(m) { G(-9223372036854775808) => 1  _ => 0 }\n}\n"
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 1
    if _tools_available():
        assert driver.cmd_run(path, native=True) == 1


# --- the native stack guard ----------------------------------------------------------


@native
def test_native_stack_overflow_clean_error(tmp_path: Path, capfd) -> None:
    # Deep non-tail recursion used to die as a raw SIGSEGV (exit 139, silence).
    # Constructor wrapping keeps post-call work so LLVM can't turn the
    # recursion into a loop.
    src = (
        "type N = | Zero | Succ(N)\n"
        "fn mk(n: I64) -> N = { if n == 0 { Zero } else { Succ(mk(n - 1)) } }\n"
        "fn main() -> I64 = { match mk(50000000) { Zero => 0  Succ(m) => 1 } }\n"
    )
    assert driver.cmd_run(_write(tmp_path, src), native=True) == 1
    assert "stack overflow" in capfd.readouterr().err


# --- generic inference through type constructors -------------------------------------


def test_generic_param_infers_through_constructor(tmp_path: Path) -> None:
    src = (
        "type Chain<T> = | End(T) | Link(Chain<T>)\n"
        "fn len<T>(c: Chain<T>) -> I64 = "
        "{ match c { End(v) => 1  Link(rest) => 1 + len(rest) } }\n"
        "fn main() -> I64 = { len(Link(Link(End(40)))) + len(End(true)) }\n"
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 4
    if _tools_available():
        assert driver.cmd_run(path, native=True) == 4


def test_eq_infers_ctor_from_left_operand(tmp_path: Path) -> None:
    # `r == Ok(2)` infers Ok's type arguments from the left operand. (The
    # payloads must be slot-inline for `==` per the equality-honesty rule.)
    src = (
        "fn mk() -> Result<I64, I64> = { Ok(2) }\n"
        "fn main() -> I64 = { let r = mk()\n if r == Ok(2) { 0 } else { 1 } }\n"
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 0
    if _tools_available():
        assert driver.cmd_run(path, native=True) == 0


# --- divergence + if/match value rules ------------------------------------------------


def test_match_of_returns_as_fn_tail(tmp_path: Path) -> None:
    src = (
        "type C = | A | B\n"
        "fn pick(c: C) -> I64 = {\n"
        "  match c {\n    A => { return 1 }\n    B => { return 2 }\n  }\n}\n"
        "fn main() -> I64 = { pick(B) }\n"
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 2
    if _tools_available():
        assert driver.cmd_run(path, native=True) == 2


def test_else_less_if_rejected_in_value_position() -> None:
    diags = _diag("fn main() -> I64 = { let x = if true { 1 }\n 0 }\n")
    assert any("without `else` has no value" in d.message for d in diags)


# --- match reachability + pattern linearity -------------------------------------------


def test_arm_after_catchall_unreachable() -> None:
    diags = _diag("type C = | A | B\nfn main() -> I64 = { match A { _ => 0  A => 1 } }\n")
    assert any(d.code == "MATCH002" and "catch-all" in d.message for d in diags)


def test_nonlinear_pattern_rejected() -> None:
    diags = _diag(
        "type P = | Pair(I64, I64)\n"
        "fn main() -> I64 = { match Pair(1, 2) { Pair(x, x) => x  _ => 0 } }\n"
    )
    assert any("more than once" in d.message for d in diags)


# --- records ---------------------------------------------------------------------------


def test_record_forward_reference(tmp_path: Path) -> None:
    # Field types may name records declared LATER in the file.
    src = (
        "type Outer = { inner: Inner }\n"
        "type Inner = { v: I64 }\n"
        "fn main() -> I64 = { let o = { inner = { v = 6 } }\n o.inner.v }\n"
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 6
    if _tools_available():
        assert driver.cmd_run(path, native=True) == 6


def test_recursion_through_record_and_adt(tmp_path: Path) -> None:
    # A record may recurse THROUGH an ADT (which boxes).
    src = (
        "type Node = { v: I64, next: Link }\n"
        "type Link = | Nil | More(Node)\n"
        "fn total(n: Node) -> I64 = "
        "{ match n.next { Nil => n.v  More(m) => n.v + total(m) } }\n"
        "fn main() -> I64 = "
        "{ total({ v = 1, next = More({ v = 2, next = More({ v = 39, next = Nil }) }) }) }\n"
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 42
    if _tools_available():
        assert driver.cmd_run(path, native=True) == 42


def test_directly_recursive_record_rejected() -> None:
    diags = _diag("type R = { next: R }\nfn main() -> I64 = { 0 }\n")
    assert any(d.code == "TYPE024" and "infinite size" in d.message for d in diags)


def test_payload_arity_validated_at_declaration() -> None:
    # `Option<I64, I64>` in an UNUSED payload is still an error.
    diags = _diag("type T = | A(Option<I64, I64>)\nfn main() -> I64 = { 0 }\n")
    assert any(d.code == "TYPE013" for d in diags)


# --- derive(Show) recursion -------------------------------------------------------------


def test_derive_show_recursive_output(tmp_path: Path, capfd) -> None:
    # Nested payloads render through their own Show impl, not "<?>".
    src = (
        "module Main\nimport Std.IO\n"
        "derive(Show) type N = | Zero | Succ(N)\n"
        "fn main() -> I64 uses { Log } = { println(Succ(Succ(Zero)).show())\n 0 }\n"
    )
    path = _write(tmp_path, src)
    assert driver.cmd_run(path, interpret=True) == 0
    out = capfd.readouterr().out
    assert out == "Succ(Succ(Zero))\n"
    if _tools_available():
        assert driver.cmd_run(path, native=True) == 0
        assert capfd.readouterr().out == out
