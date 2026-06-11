"""`build.flx`: effect-checked targets, the build graph, and the runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from flx.build import run_build
from flx.diagnostics import FlexError
from flx.macro import expand
from flx.sema.specialize import check_and_monomorphize
from flx.syntax.parser import parse


def _codes(src: str) -> list[str]:
    with pytest.raises(FlexError) as exc:
        check_and_monomorphize(expand(parse(src)))
    return [d.code for d in exc.value.diagnostics]


def _build_dir(tmp_path: Path, src: str, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "build.flx").write_text(src, encoding="utf-8")
    monkeypatch.chdir(tmp_path)


# --- checking ------------------------------------------------------------------


def test_target_must_declare_process_for_sh() -> None:
    codes = _codes('module Build\ntarget oops { sh("true")? }\n')
    assert "EFFECT001" in codes


def test_target_calls_propagate_effects() -> None:
    # `all` calls `one` (Process) without declaring Process itself.
    src = 'module Build\ntarget one uses { Process } { sh("true")? }\ntarget all { one()? }\n'
    assert "EFFECT001" in _codes(src)


def test_default_must_name_a_target() -> None:
    src = 'module Build\ntarget default = nope\ntarget one uses { Process } { sh("true")? }\n'
    assert "BUILD002" in _codes(src)


def test_unknown_flx_op_rejected() -> None:
    src = 'module Build\ntarget t uses { Fs } { flx.frobnicate("*.flx")? }\n'
    assert "BUILD003" in _codes(src)


def test_sh_is_not_available_outside_build_files(tmp_path: Path) -> None:
    # No targets -> no build mode -> `sh` is just an unknown name.
    src = 'fn main() -> I64 uses { Process } = { let r = sh("true")\n 0 }\n'
    codes = _codes(src)
    assert "NAME001" in codes


# --- running -------------------------------------------------------------------


def test_build_runs_default_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    _build_dir(
        tmp_path,
        'module Build\ntarget default = hello\ntarget hello uses { Process } { sh("true")? }\n',
        monkeypatch,
    )
    assert run_build() == 0
    assert "build ok: hello" in capfd.readouterr().out


def test_build_failure_propagates_through_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    _build_dir(
        tmp_path,
        (
            "module Build\n"
            "target default = all\n"
            'target bad uses { Process } { sh("false")? }\n'
            "target all uses { Process } { bad()? }\n"
        ),
        monkeypatch,
    )
    assert run_build() == 1
    assert "build failed: all" in capfd.readouterr().err


def test_targets_are_memoized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # `shared` is reached twice through the graph but must run once.
    marker = tmp_path / "ran"
    _build_dir(
        tmp_path,
        (
            "module Build\n"
            "target default = all\n"
            f'target shared uses {{ Process }} {{ sh("echo x >> {marker}")? }}\n'
            "target a uses { Process } { shared()? }\n"
            "target b uses { Process } { shared()? }\n"
            "target all uses { Process } { a()?\n  b()? }\n"
        ),
        monkeypatch,
    )
    assert run_build() == 0
    assert len(marker.read_text(encoding="utf-8").splitlines()) == 1


def test_explain_lists_targets_and_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    _build_dir(
        tmp_path,
        ('module Build\ntarget default = test\ntarget test uses { Fs, Process } { sh("true")? }\n'),
        monkeypatch,
    )
    assert run_build(explain=True) == 0
    out = capfd.readouterr().out
    assert "target test  (default)" in out
    assert "uses: Fs, Process" in out


def test_unknown_target_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    _build_dir(
        tmp_path,
        'module Build\ntarget one uses { Process } { sh("true")? }\n',
        monkeypatch,
    )
    assert run_build("nope") == 2
    assert "no target 'nope'" in capfd.readouterr().err


def test_flx_intrinsic_checks_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "prog.flx").write_text("fn main() -> I64 = { 0 }\n", encoding="utf-8")
    _build_dir(
        tmp_path,
        'module Build\ntarget default = c\ntarget c uses { Fs } { flx.check("*.flx")? }\n',
        monkeypatch,
    )
    assert run_build() == 0
    assert "prog.flx type-checks" in capfd.readouterr().out


def test_flx_intrinsic_fails_on_bad_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "bad_prog.flx").write_text("fn main() -> I64 = { true }\n", encoding="utf-8")
    _build_dir(
        tmp_path,
        'module Build\ntarget default = c\ntarget c uses { Fs } { flx.check("bad_*.flx")? }\n',
        monkeypatch,
    )
    assert run_build() == 1
    assert "build failed" in capfd.readouterr().err


def test_keyword_named_target_is_callable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # `test()` in a target body calls the target named `test`.
    _build_dir(
        tmp_path,
        (
            "module Build\n"
            "target default = ci\n"
            'target test uses { Process } { sh("true")? }\n'
            "target ci uses { Process } { test()? }\n"
        ),
        monkeypatch,
    )
    assert run_build() == 0
    assert "build ok: ci" in capfd.readouterr().out


def test_cyclic_targets_reported_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    _build_dir(
        tmp_path,
        "module Build\ntarget default = a\ntarget a { b()? }\ntarget b { a()? }\n",
        monkeypatch,
    )
    assert run_build() == 1
    assert "target cycle detected: a -> b -> a" in capfd.readouterr().err


def test_sh_not_available_in_build_helper_fns() -> None:
    # Build intrinsics are scoped to TARGET BODIES; a helper fn cannot wrap sh.
    src = (
        "module Build\n"
        'fn helper() -> I64 uses { Process } = { let r = sh("true")\n  0 }\n'
        'target t uses { Process } { sh("true")? }\n'
    )
    assert "NAME001" in _codes(src)


def test_targets_rejected_outside_build_flx(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # Importing a module that declares targets must not flip a program into
    # build mode.
    from flx import driver

    lib = tmp_path / "Lib"
    lib.mkdir()
    (lib / "B.flx").write_text(
        'module Lib.B\ntarget x uses { Process } { sh("true")? }\n', encoding="utf-8"
    )
    main = tmp_path / "main.flx"
    main.write_text("module Main\nimport Lib.B\nfn main() -> I64 = { 0 }\n", encoding="utf-8")
    assert driver.cmd_check(str(main)) == 1
    assert "BUILD004" in capfd.readouterr().err


def test_reserved_target_names_rejected() -> None:
    assert "BUILD006" in _codes('module Build\ntarget sh uses { Process } { sh("true")? }\n')


def test_duplicate_default_rejected() -> None:
    src = (
        "module Build\ntarget default = a\ntarget default = b\n"
        'target a uses { Process } { sh("true")? }\n'
        'target b uses { Process } { sh("true")? }\n'
    )
    assert "BUILD007" in _codes(src)


def test_macros_expand_inside_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    _build_dir(
        tmp_path,
        (
            "module Build\n"
            "macro noisy(c) = quote { sh(unquote(c))? }\n"
            "target default = t\n"
            'target t uses { Process } { noisy("true") }\n'
        ),
        monkeypatch,
    )
    assert run_build() == 0


def test_file_named_like_target_does_not_hijack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    from flx.cli import main

    _build_dir(
        tmp_path,
        'module Build\ntarget check uses { Process } { sh("true")? }\n',
        monkeypatch,
    )
    (tmp_path / "check").write_text("not a flex file", encoding="utf-8")
    assert main(["build", "check"]) == 0
    assert "build ok: check" in capfd.readouterr().out


def test_package_demo_example(capfd: pytest.CaptureFixture[str]) -> None:
    # The in-repo demo package: app depends on mathlib by path.
    from flx import driver

    assert driver.cmd_run("examples/package-demo/app/main.flx", interpret=True) == 42
    assert driver.cmd_test("examples/package-demo/app/main.flx", interpret=True) == 0


def test_keyword_named_target_and_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # `test` is a Flex keyword but a legal target name and `flx.` member.
    (tmp_path / "t.flx").write_text(
        'fn main() -> I64 = { 0 }\ntest "ok" { assert(true) }\n', encoding="utf-8"
    )
    _build_dir(
        tmp_path,
        'module Build\ntarget default = test\ntarget test uses { Fs } { flx.test("t.flx")? }\n',
        monkeypatch,
    )
    assert run_build() == 0
    assert "build ok: test" in capfd.readouterr().out
