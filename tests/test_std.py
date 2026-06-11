"""The standard library: Flex modules shipped inside the compiler package."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from flx import driver
from flx.modules import std_root


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


STD_PROGRAM = """module Main
import Std.Math
import Std.Str
import Std.Env
import Std.Time
import Std.Proc

fn main() -> I64 uses { Process, Time } = {
  let alive = if unix_time() > 1700000000 { 1 } else { 0 }
  let me = if pid() > 0 { 1 } else { 0 }
  abs(0 - 30) + pow(2, 3) + length("fallback") + sign(99) + cmp("a", "b") + alive + me
}

test "math" {
  assert_eq(abs(0 - 5), 5)
  assert_eq(min(3, 4), 3)
  assert_eq(max(3, 4), 4)
  assert_eq(clamp(99, 0, 9), 9)
  assert_eq(clamp(0 - 99, 0, 9), 0)
  assert_eq(sign(0 - 7), 0 - 1)
  assert_eq(sign(0), 0)
  assert_eq(pow(2, 10), 1024)
  assert_eq(pow(7, 0), 1)
  assert_eq(pow(2, 0 - 1), 0)
}

test "strings" {
  assert_eq(length("hello"), 5)
  assert_eq(length(""), 0)
  assert(is_empty(""))
  assert(!is_empty("x"))
  assert(eq("abc", "abc"))
  assert(ne("abc", "abd"))
  assert_eq(cmp("a", "b"), 0 - 1)
  assert_eq(cmp("b", "a"), 1)
  assert_eq(cmp("same", "same"), 0)
}

test "string equality via the Eq trait" {
  assert("hello".eq("hello"))
  assert(!("a".eq("b")))
}

test "show for strings" {
  assert(eq("xyz".show(), "xyz"))
}

test "env" uses { Process } {
  assert(eq(get_or("FLX_NO_SUCH_VAR_XYZ", "fallback"), "fallback"))
  assert(!has("FLX_NO_SUCH_VAR_XYZ"))
}
"""


def test_std_root_ships_with_the_package() -> None:
    assert (std_root() / "Std" / "Math.flx").is_file()


def test_std_program_interprets(tmp_path: Path) -> None:
    path = _write(tmp_path, STD_PROGRAM)
    # 30 + 8 + 8 + 1 - 1 + 1 + 1 = 48
    assert driver.cmd_run(path, interpret=True) == 48
    assert driver.cmd_test(path, interpret=True) == 0


@native
def test_std_matches_native(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, STD_PROGRAM)
    native_code = driver.cmd_run(path, native=True)
    native_out = capfd.readouterr().out
    interp_code = driver.cmd_run(path, interpret=True)
    interp_out = capfd.readouterr().out
    assert (interp_code, interp_out) == (native_code, native_out)
    assert native_code == 48
    assert driver.cmd_test(path, native=True) == 0


def test_std_private_externs_stay_private(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # Std.Str's `strcmp` extern is private; users wrap or redeclare, not reach in.
    path = _write(
        tmp_path,
        'module Main\nimport Std.Str\nfn main() -> I64 = { strcmp("a", "b") }\n',
    )
    assert driver.cmd_check(path) == 1
    assert "VIS001" in capfd.readouterr().err


def test_user_can_redeclare_std_extern(tmp_path: Path) -> None:
    # C-style identical redeclaration: a user declaring `strlen` next to
    # Std.Str's private one is fine.
    src = (
        "module Main\nimport Std.Str\n"
        "extern fn strlen(s: String) -> I64\n"
        'fn main() -> I64 = { strlen("abc") + length("z") }\n'
    )
    assert driver.cmd_run(_write(tmp_path, src), interpret=True) == 4


def test_conflicting_extern_redeclaration_rejected(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    src = (
        "module Main\nimport Std.Str\n"
        "extern fn strcmp(a: String, b: String) -> I64\n"  # Std declares -> I32
        "fn main() -> I64 = { 0 }\n"
    )
    assert driver.cmd_check(_write(tmp_path, src)) == 1
    assert "FFI004" in capfd.readouterr().err


def test_user_modules_shadow_std(tmp_path: Path) -> None:
    # A local Std/Math.flx wins over the bundled one, deliberately.
    std_dir = tmp_path / "Std"
    std_dir.mkdir()
    (std_dir / "Math.flx").write_text(
        "module Std.Math\npub fn abs(n: I64) -> I64 = { 777 }\n", encoding="utf-8"
    )
    path = _write(tmp_path, "module Main\nimport Std.Math\nfn main() -> I64 = { abs(1) }\n")
    assert driver.cmd_run(path, interpret=True) == 777 & 0xFF


def test_std_effects_propagate(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    # Std.Proc.pid uses { Process }: calling it without declaring is EFFECT001.
    path = _write(tmp_path, "module Main\nimport Std.Proc\nfn main() -> I64 = { pid() }\n")
    assert driver.cmd_check(path) == 1
    assert "EFFECT001" in capfd.readouterr().err


# --- review findings -------------------------------------------------------------


def test_shadow_cannot_rewire_std_internals(tmp_path: Path) -> None:
    # The stdlib's own dependency graph is pinned: a user Std/Str.flx shadow must
    # not change Std.Env's documented behavior (its internal `import Std.Str`).
    std_dir = tmp_path / "Std"
    std_dir.mkdir()
    (std_dir / "Str.flx").write_text(
        "module Std.Str\npub fn is_empty(s: String) -> Bool = { false }\n", encoding="utf-8"
    )
    src = (
        "module Main\nimport Std.Env\n"
        "extern fn strlen(s: String) -> I64\n"
        "fn main() -> I64 uses { Process } = "
        '{ strlen(get_or("FLX_DEFINITELY_UNSET_XYZ", "fallback")) }\n'
    )
    assert driver.cmd_run(_write(tmp_path, src), interpret=True) == 8  # "fallback"


def test_pub_redeclaration_cannot_unlock_private_extern(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "Lib.flx").write_text(
        "module Lib\npub extern fn strcmp(a: String, b: String) -> I32\n", encoding="utf-8"
    )
    src = 'module Main\nimport Std.Str\nimport Lib\nfn main() -> I64 = { strcmp("a", "b") }\n'
    assert driver.cmd_check(_write(tmp_path, src)) == 1
    assert "FFI004" in capfd.readouterr().err


def test_arity_collision_with_std_is_clean(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # A user fn colliding with a std fn at a different arity must be TYPE002,
    # not a zip() traceback.
    src = (
        "module Main\nimport Std.Math\n"
        "fn abs(a: I64, b: I64) -> I64 = { a + b }\nfn main() -> I64 = { abs(1, 2) }\n"
    )
    assert driver.cmd_check(_write(tmp_path, src)) == 1
    assert "TYPE002" in capfd.readouterr().err


def test_derived_eq_with_string_fields(tmp_path: Path) -> None:
    # derive(Eq) on a record with String fields compares field-wise through the
    # Eq trait for strings (so Std.Str must be imported).
    src = (
        "module Main\nimport Std.Str\n"
        "derive(Eq) type User = { id: I64, name: String }\n"
        "fn main() -> I64 = { 0 }\n"
        'test "eq" { let a = { id = 1, name = "ada" }\n'
        '  assert(a.eq({ id = 1, name = "ada" }))\n'
        '  assert(!a.eq({ id = 1, name = "bob" }))\n'
        '  assert(!a.eq({ id = 2, name = "ada" })) }\n'
    )
    assert driver.cmd_test(_write(tmp_path, src), interpret=True) == 0


def test_i32_abi_sign_extension(tmp_path: Path) -> None:
    # The regression that motivated I32: strcmp returns C int; reading it as 64
    # bits made negative results positive. cmp must be exactly -1 here.
    src = (
        "module Main\nimport Std.Str\n"
        'fn main() -> I64 = { cmp("a", "b") + 1 }\n'  # -1 + 1 = 0
    )
    assert driver.cmd_run(_write(tmp_path, src), interpret=True) == 0


@native
def test_i32_abi_sign_extension_native(tmp_path: Path) -> None:
    src = 'module Main\nimport Std.Str\nfn main() -> I64 = { cmp("a", "b") + 1 }\n'
    assert driver.cmd_run(_write(tmp_path, src), native=True) == 0
