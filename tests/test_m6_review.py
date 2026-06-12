"""M6 adversarial-review findings, pinned: the ADT-variant ICE family, the
duplicate-definition error cascade, the native panic-message truncation, the
builtin-Option VIS001 cascade, the dead --format flag, and flx test --docs
--native backend semantics."""

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


def _write(tmp_path: Path, src: str) -> str:
    flx = tmp_path / "main.flx"
    flx.write_text(src, encoding="utf-8")
    return str(flx)


def _check(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "flx", "check", path], capture_output=True, text=True
    )


# --- the ADT-variant ICE family ----------------------------------------------------------


def test_user_type_named_fs_is_a_diagnostic_not_an_ice(tmp_path: Path) -> None:
    # `type Fs` + `import Std.IO` breaks Std.IO's own `Fs.read_line()` body —
    # that must be a clean error naming the shadow, never a KeyError traceback.
    src = "module Main\nimport Std.IO\n\ntype Fs = | Disk\n\nfn main() -> I64 = { 0 }\n"
    proc = _check(_write(tmp_path, src))
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr
    assert "has no variant 'read_line'" in proc.stderr
    assert "shadows the Fs intrinsic module" in proc.stderr


def test_user_type_named_log_is_a_diagnostic_not_an_ice(tmp_path: Path) -> None:
    src = (
        "module Main\nimport Std.IO\n\ntype Log = | Quiet | Loud\n\n"
        'fn main() -> I64 uses { Log } = { println("x")\n  0 }\n'
    )
    proc = _check(_write(tmp_path, src))
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr
    assert "shadows the Log intrinsic module" in proc.stderr


def test_nonexistent_variant_is_a_diagnostic(tmp_path: Path) -> None:
    src = "type Color = | Red | Green\nfn main() -> I64 = { let x = Color.Blue\n  0 }\n"
    proc = _check(_write(tmp_path, src))
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr
    assert "type 'Color' has no variant 'Blue'" in proc.stderr


def test_wrong_adts_variant_is_a_diagnostic(tmp_path: Path) -> None:
    # `Some` exists — as Option's variant, not Color's.
    src = "type Color = | Red | Green\nfn main() -> I64 = { let x = Color.Some\n  0 }\n"
    proc = _check(_write(tmp_path, src))
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr
    assert "type 'Color' has no variant 'Some'" in proc.stderr


# --- duplicate definitions: no false cascade ------------------------------------------------


def test_duplicate_read_line_reports_collision_only(tmp_path: Path) -> None:
    # The user's `fn read_line() -> String` collides with Std.IO's — TYPE002 is
    # the truth; "return value has type String, expected Option<String>" about
    # the user's internally-consistent body is not.
    src = (
        "module Main\nimport Std.IO\n\n"
        'fn read_line() -> String = { "shadowed" }\n\n'
        "fn main() -> I64 uses { Log } = {\n  println(read_line())\n  0\n}\n"
    )
    proc = _check(_write(tmp_path, src))
    assert proc.returncode == 1
    assert "TYPE002" in proc.stderr
    assert "expected Option<String>" not in proc.stderr
    assert "EFFECT001" not in proc.stderr


# --- builtin Option redefinition: no stdlib blame -------------------------------------------


def test_redefining_option_does_not_blame_stdlib(tmp_path: Path) -> None:
    src = (
        "module Main\nimport Std.IO\n\ntype Option = | Yes | No\n\n"
        "fn main() -> I64 uses { Fs, Log } = {\n"
        '  match read_line() { Some(l) => { println(l) }  None => { println("eof") } }\n'
        "  0\n}\n"
    )
    proc = _check(_write(tmp_path, src))
    assert proc.returncode == 1
    assert "TYPE002" in proc.stderr
    assert "VIS001" not in proc.stderr
    assert "std/Std/IO.flx" not in proc.stderr  # never point fault reports at stdlib


# --- native panic-message exactness ----------------------------------------------------------


@native
def test_long_byte_panic_message_parity(tmp_path: Path) -> None:
    # The full 20-digit value must survive the native snprintf buffer.
    src = (
        "module Main\nimport Std.Str\n"
        "fn main() -> I64 = { length(from_byte(9000000000000000000)) }\n"
    )
    path = _write(tmp_path, src)
    runs = [
        subprocess.run([sys.executable, "-m", "flx", "run", *extra, path], capture_output=True)
        for extra in ([], ["--native"])
    ]
    assert runs[0].returncode == runs[1].returncode == 1
    assert runs[0].stderr == runs[1].stderr
    assert (
        b"byte 9000000000000000000 is outside 1..255 (strings are NUL-terminated)" in runs[0].stderr
    )


# --- CLI honesty ------------------------------------------------------------------------------


def test_unimplemented_test_format_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, 'test "t" { assert_eq(1, 1) }\n')
    for fmt in ("json", "junit"):
        proc = subprocess.run(
            [sys.executable, "-m", "flx", "test", "--format", fmt, path],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2
        assert "not yet implemented" in proc.stderr
    ok = subprocess.run(
        [sys.executable, "-m", "flx", "test", "--format", "pretty", path],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0


# --- flx test --docs backend semantics --------------------------------------------------------


@native
def test_docs_native_runs_native_only(tmp_path: Path) -> None:
    src = (
        "module DocHost\nimport Std.Str\n"
        "pub fn pair() -> String = { from_byte(0xC3) ++ from_byte(0xA9) }\n"
        'doc pair { test "completes utf-8" { assert_eq(pair(), "é") } }\n'
        "fn main() -> I64 = { 0 }\n"
    )
    path = _write(tmp_path, src)
    proc = subprocess.run(
        [sys.executable, "-m", "flx", "test", "--docs", "--native", path],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    # --native means native, not both: exactly one doc-test run block.
    assert proc.stdout.count("passed, ") == 2  # the regular run + ONE doc run
