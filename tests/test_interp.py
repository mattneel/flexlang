"""The pure-Python interpreter (`flx run/test --interpret`).

The interpreter is the portable, toolchain-free execution path; the native
backend is the optimizing one. They must agree, so the load-bearing test is
differential: for every example, native and interpreted output (exit code AND
stdout) must be identical. The non-`@native` tests give the interpreter coverage
even where the LLVM toolchain is absent (e.g. a bare `uvx` CI run).
"""

from __future__ import annotations

import os
import shutil

import pytest

from flx import driver

# Each example's `flx run` exit code, interpreted (no toolchain needed).
RUN_EXIT = {
    "add": 42,
    "effects": 42,
    "hello": 42,
    "macros": 67,
    "records": 142,
    "regions": 42,
    "result": 5,
    "traits": 7,
}
EXAMPLES = sorted(RUN_EXIT)


def _tools_available() -> bool:
    bindir = "/usr/lib/llvm-22/bin"
    return all(
        bool(shutil.which(t)) or os.path.exists(os.path.join(bindir, t))
        for t in ("mlir-opt", "mlir-translate", "clang")
    )


native = pytest.mark.skipif(not _tools_available(), reason="LLVM/MLIR toolchain not available")


@pytest.mark.parametrize("name", EXAMPLES)
def test_interpret_run_exit_codes(name: str) -> None:
    assert driver.cmd_run(f"examples/{name}.flx", interpret=True) == RUN_EXIT[name]


@pytest.mark.parametrize("name", EXAMPLES)
def test_interpret_tests_pass(name: str) -> None:
    assert driver.cmd_test(f"examples/{name}.flx", interpret=True) == 0


@native
@pytest.mark.parametrize("name", EXAMPLES)
def test_run_matches_native(name: str, capfd: pytest.CaptureFixture[str]) -> None:
    path = f"examples/{name}.flx"
    native_code = driver.cmd_run(path)
    native_out = capfd.readouterr().out
    interp_code = driver.cmd_run(path, interpret=True)
    interp_out = capfd.readouterr().out
    assert (interp_code, interp_out) == (native_code, native_out)


@native
@pytest.mark.parametrize("name", EXAMPLES)
def test_tests_match_native(name: str, capfd: pytest.CaptureFixture[str]) -> None:
    path = f"examples/{name}.flx"
    native_code = driver.cmd_test(path)
    native_out = capfd.readouterr().out
    interp_code = driver.cmd_test(path, interpret=True)
    interp_out = capfd.readouterr().out
    assert (interp_code, interp_out) == (native_code, native_out)


def test_failing_assert_reports_and_exits_nonzero(
    tmp_path: object, capfd: pytest.CaptureFixture[str]
) -> None:
    from pathlib import Path

    src = 'fn main() -> I64 = { 0 }\ntest "boom" { assert_eq(1 + 1, 3) }\n'
    flx = Path(str(tmp_path)) / "boom.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_test(str(flx), interpret=True) == 1
    out = capfd.readouterr().out
    assert "assert_eq failed: actual 2, expected 3" in out
    assert "fail" in out
    assert "0 passed, 1 failed" in out


def test_runtime_division_by_zero_is_clean(
    tmp_path: object, capfd: pytest.CaptureFixture[str]
) -> None:
    from pathlib import Path

    # Division by zero at runtime is a clean error, not a Python traceback.
    src = "fn main() -> I64 = { let z = 0\n 1 / z }\n"
    flx = Path(str(tmp_path)) / "dz.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_run(str(flx), interpret=True) == 1
    assert "division by zero" in capfd.readouterr().err


# Signed division/remainder edge cases must be DEFINED and agree across paths
# (the native backend guards them via flx_idiv/flx_imod rather than raw sdiv/srem).
_DIV_CASES = {
    "intmin_div_neg1": (
        "let m = -9223372036854775807 - 1\n m / (0 - 1)",
        -9223372036854775808 & 0xFF,
    ),
    "intmin_mod_neg1": ("let m = -9223372036854775807 - 1\n m % (0 - 1)", 0),
    "neg_div_trunc": ("(0 - 7) / 2", (-3) & 0xFF),
    "neg_mod_trunc": ("(0 - 7) % 2", (-1) & 0xFF),
}


@pytest.mark.parametrize("name", sorted(_DIV_CASES))
def test_division_edge_cases_interpret(tmp_path: object, name: str) -> None:
    from pathlib import Path

    body, expected = _DIV_CASES[name]
    flx = Path(str(tmp_path)) / f"{name}.flx"
    flx.write_text(f"fn main() -> I64 = {{ {body} }}\n", encoding="utf-8")
    assert driver.cmd_run(str(flx), interpret=True) == expected


@native
@pytest.mark.parametrize("name", sorted(_DIV_CASES))
def test_division_edge_cases_match_native(tmp_path: object, name: str) -> None:
    from pathlib import Path

    body, _ = _DIV_CASES[name]
    flx = Path(str(tmp_path)) / f"{name}.flx"
    flx.write_text(f"fn main() -> I64 = {{ {body} }}\n", encoding="utf-8")
    assert driver.cmd_run(str(flx)) == driver.cmd_run(str(flx), interpret=True)


@native
def test_native_division_by_zero_traps(tmp_path: object, capfd: pytest.CaptureFixture[str]) -> None:
    from pathlib import Path

    # Native must trap (not silently miscompile via UB) with the same message
    # and exit code as the interpreter.
    src = 'fn main() -> I64 uses { Log } = { Log.info("before")\n 10 / (0 - 0) }\n'
    flx = Path(str(tmp_path)) / "trap.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_run(str(flx)) == 1
    captured = capfd.readouterr()
    assert "before" in captured.out
    assert "division by zero" in captured.err
