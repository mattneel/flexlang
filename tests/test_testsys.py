"""Tests for test discovery, harness generation, and `flx test`."""

from __future__ import annotations

import os
import shutil

import pytest

from flx import driver
from flx.backend.harness import generate_harness

ADD_EXPECTED = "running 1 test\n\nok Main / add works\n\n1 passed, 0 failed\n"


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"

    def has(tool: str) -> bool:
        return bool(shutil.which(tool)) or os.path.exists(os.path.join(bindir, tool))

    return all(has(t) for t in ("mlir-opt", "mlir-translate", "clang"))


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


# --- harness generation (always runs) -----------------------------------------


def test_harness_header_singular() -> None:
    c = generate_harness("Main", [(0, "add works")])
    assert 'printf("running 1 test\\n\\n")' in c
    assert "extern int flx_test_0(void);" in c
    # Name is passed as a printf argument (not in the format string) for safety.
    assert 'printf("ok %s\\n", "Main / add works")' in c


def test_harness_passes_name_as_arg_and_escapes() -> None:
    # `%` stays literal (passed as an arg, not in the format) and a real newline
    # is escaped so the C string literal stays valid.
    c = generate_harness("M", [(0, "pct%here\nnl")])
    assert 'printf("ok %s\\n", "M / pct%here\\nnl");' in c
    assert 'printf("fail %s\\n", "M / pct%here\\nnl");' in c


def test_harness_header_plural_and_indices() -> None:
    c = generate_harness("Math", [(0, "a"), (2, "c")])
    assert 'printf("running 2 tests\\n\\n")' in c
    assert "extern int flx_test_0(void);" in c
    assert "extern int flx_test_2(void);" in c
    assert "extern int flx_test_1(void);" not in c  # index 1 filtered out


# --- native test runs (skipped without toolchain) -----------------------------


@native
def test_run_add_test_exact_output(capfd: pytest.CaptureFixture[str]) -> None:
    rc = driver.cmd_test("examples/add.flx")
    out = capfd.readouterr().out
    assert rc == 0
    assert out == ADD_EXPECTED


@native
def test_failing_test_reports_and_exits_nonzero(
    tmp_path, capfd: pytest.CaptureFixture[str]
) -> None:
    src = 'fn add(a: I64, b: I64) -> I64 = { a + b }\ntest "wrong" { assert_eq(add(2, 2), 5) }'
    flx = tmp_path / "t.flx"
    flx.write_text(src, encoding="utf-8")
    rc = driver.cmd_test(str(flx))
    out = capfd.readouterr().out
    assert rc == 1
    assert "fail Main / wrong" in out
    assert "actual 4, expected 5" in out
    assert "0 passed, 1 failed" in out


@native
def test_assert_bool_and_filter(tmp_path, capfd: pytest.CaptureFixture[str]) -> None:
    src = (
        'test "boolean" { assert(2 + 2 == 4) }\n'
        'test "eq bool" { assert_eq(1 < 2, true) }\n'
        'test "other" { assert(false) }'
    )
    flx = tmp_path / "t.flx"
    flx.write_text(src, encoding="utf-8")
    rc = driver.cmd_test(str(flx), test_filter="bool")
    out = capfd.readouterr().out
    assert rc == 0
    assert "running 2 tests" in out
    assert "2 passed, 0 failed" in out
