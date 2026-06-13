"""`package.flx` manifests: pure evaluation, validation, and path dependencies."""

from __future__ import annotations

from pathlib import Path

import pytest

from flx import driver
from flx.cli import main as cli_main
from flx.diagnostics import FlexError
from flx.package import dependency_roots, load_manifest

APP_MANIFEST = """module Package

fn manifest() -> Manifest = {
  {
    name = "app",
    version = "0.1.0",
    entry = "main.flx",
    dependencies = [ { name = "Mathlib", path = "../mathlib" } ]
  }
}
"""

LIB_MANIFEST = """module Package

fn manifest() -> Manifest = {
  { name = "mathlib", version = "0.2.0", entry = "Mathlib.flx", dependencies = [] }
}
"""

LIB_MODULE = """module Mathlib
pub fn square(n: I64) -> I64 = { n * n }
fn hidden() -> I64 = { 0 }
"""


def _write(base: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src, encoding="utf-8")


def _two_packages(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        {
            "app/package.flx": APP_MANIFEST,
            "app/main.flx": (
                "module Main\nimport Mathlib\nfn main() -> I64 = { square(7) }\n"
                'test "dep" { assert_eq(square(3), 9) }\n'
            ),
            "mathlib/package.flx": LIB_MANIFEST,
            "mathlib/Mathlib.flx": LIB_MODULE,
        },
    )
    return tmp_path / "app"


def test_manifest_loads_and_is_typed(tmp_path: Path) -> None:
    _write(tmp_path, {"package.flx": LIB_MANIFEST})
    m = load_manifest(tmp_path / "package.flx")
    assert (m.name, m.version, m.entry) == ("mathlib", "0.2.0", "Mathlib.flx")
    assert m.dependencies == ()


def test_manifest_dependencies_parse(tmp_path: Path) -> None:
    _write(tmp_path, {"package.flx": APP_MANIFEST})
    m = load_manifest(tmp_path / "package.flx")
    assert m.dependencies[0].name == "Mathlib"
    assert m.dependencies[0].path == "../mathlib"


def test_manifest_must_be_pure(tmp_path: Path) -> None:
    src = (
        "module Package\n"
        "fn manifest() -> Manifest uses { Log } = {\n"
        '  { name = "x", version = "0", entry = "main.flx", dependencies = [] }\n'
        "}\n"
    )
    _write(tmp_path, {"package.flx": src})
    with pytest.raises(FlexError) as exc:
        load_manifest(tmp_path / "package.flx")
    assert exc.value.diagnostics[0].code == "PKG003"


def test_manifest_effectful_call_rejected_by_checker(tmp_path: Path) -> None:
    # Even without a `uses` clause, an effectful call inside manifest() is
    # rejected — purity is enforced by the effect system, not by trust.
    src = (
        "module Package\n"
        "fn manifest() -> Manifest = {\n"
        '  Log.info("sneaky")\n'
        '  { name = "x", version = "0", entry = "main.flx", dependencies = [] }\n'
        "}\n"
    )
    _write(tmp_path, {"package.flx": src})
    with pytest.raises(FlexError) as exc:
        load_manifest(tmp_path / "package.flx")
    assert any(d.code == "EFFECT001" for d in exc.value.diagnostics)


def test_manifest_wrong_shape_rejected(tmp_path: Path) -> None:
    src = "module Package\nfn manifest() -> I64 = { 0 }\n"
    _write(tmp_path, {"package.flx": src})
    with pytest.raises(FlexError) as exc:
        load_manifest(tmp_path / "package.flx")
    assert exc.value.diagnostics[0].code == "PKG002"


def test_missing_dependency_dir(tmp_path: Path) -> None:
    _write(tmp_path, {"app/package.flx": APP_MANIFEST})  # no ../mathlib
    m = load_manifest(tmp_path / "app/package.flx")
    with pytest.raises(FlexError) as exc:
        dependency_roots(m)
    assert exc.value.diagnostics[0].code == "PKG004"


def test_run_resolves_path_dependency(tmp_path: Path) -> None:
    app = _two_packages(tmp_path)
    assert driver.cmd_run(str(app / "main.flx"), interpret=True) == 49


def test_test_resolves_path_dependency(tmp_path: Path) -> None:
    app = _two_packages(tmp_path)
    assert driver.cmd_test(str(app / "main.flx"), interpret=True) == 0


def test_dep_privacy_enforced_across_packages(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    app = _two_packages(tmp_path)
    bad = app / "bad.flx"
    bad.write_text(
        "module Main\nimport Mathlib\nfn main() -> I64 = { hidden() }\n", encoding="utf-8"
    )
    assert driver.cmd_check(str(bad)) == 1
    assert "VIS001" in capfd.readouterr().err


def test_no_arg_discovery_uses_manifest_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _two_packages(tmp_path)
    monkeypatch.chdir(app)
    assert driver.cmd_run(None, interpret=True) == 49


def test_check_validates_manifest_file(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, {"package.flx": LIB_MANIFEST})
    assert driver.cmd_check(str(tmp_path / "package.flx")) == 0
    assert "valid manifest (mathlib 0.2.0)" in capfd.readouterr().out


# --- robustness (adversarial-review findings) ----------------------------------


def test_nonterminating_manifest_hits_step_limit(tmp_path: Path) -> None:
    src = (
        "module Package\n"
        "fn spin() -> I64 = { mut i = 0\n  while i >= 0 { i = i + 1 }\n  i }\n"
        "fn manifest() -> Manifest = { let w = spin()\n"
        '  { name = "x", version = "0", entry = "main.flx", dependencies = [] } }\n'
    )
    _write(tmp_path, {"package.flx": src})
    with pytest.raises(FlexError) as exc:
        load_manifest(tmp_path / "package.flx")
    assert exc.value.diagnostics[0].code == "PKG005"


def test_manifest_runtime_fault_is_clean(tmp_path: Path) -> None:
    src = (
        "module Package\n"
        "fn manifest() -> Manifest = { let boom = 1 / 0\n"
        '  { name = "x", version = "0", entry = "main.flx", dependencies = [] } }\n'
    )
    _write(tmp_path, {"package.flx": src})
    with pytest.raises(FlexError) as exc:
        load_manifest(tmp_path / "package.flx")
    assert exc.value.diagnostics[0].code == "PKG005"


def test_manifest_recursion_is_clean(tmp_path: Path) -> None:
    src = (
        "module Package\n"
        "fn deep(n: I64) -> I64 = { deep(n + 1) }\n"
        "fn manifest() -> Manifest = { let w = deep(0)\n"
        '  { name = "x", version = "0", entry = "main.flx", dependencies = [] } }\n'
    )
    _write(tmp_path, {"package.flx": src})
    with pytest.raises(FlexError) as exc:
        load_manifest(tmp_path / "package.flx")
    assert exc.value.diagnostics[0].code == "PKG005"


def test_manifest_may_use_generics(tmp_path: Path) -> None:
    src = (
        "module Package\n"
        "fn id<T>(x: T) -> T = { x }\n"
        "fn manifest() -> Manifest = {\n"
        '  { name = id("gen"), version = "0.1.0", entry = "main.flx", dependencies = [] }\n'
        "}\n"
    )
    _write(tmp_path, {"package.flx": src})
    assert load_manifest(tmp_path / "package.flx").name == "gen"


def test_manifest_may_not_declare_targets(tmp_path: Path) -> None:
    src = (
        "module Package\n"
        'target evil uses { Process } { sh("true")? }\n'
        "fn manifest() -> Manifest = "
        '{ { name = "x", version = "0", entry = "main.flx", dependencies = [] } }\n'
    )
    _write(tmp_path, {"package.flx": src})
    with pytest.raises(FlexError) as exc:
        load_manifest(tmp_path / "package.flx")
    assert exc.value.diagnostics[0].code == "PKG006"


def test_manifest_may_not_declare_externs(tmp_path: Path) -> None:
    # Extern purity is author-asserted trust, which manifests must not require.
    src = (
        "module Package\n"
        "extern fn getpid() -> I64\n"
        "fn manifest() -> Manifest = "
        '{ { name = "x", version = "0", entry = "m.flx", dependencies = [] } }\n'
    )
    _write(tmp_path, {"package.flx": src})
    with pytest.raises(FlexError) as exc:
        load_manifest(tmp_path / "package.flx")
    assert exc.value.diagnostics[0].code == "PKG007"


def test_non_utf8_manifest_is_clean(tmp_path: Path) -> None:
    (tmp_path / "package.flx").write_bytes(b"\xff\xfe garbage")
    with pytest.raises(FlexError) as exc:
        load_manifest(tmp_path / "package.flx")
    assert exc.value.diagnostics[0].code == "PKG001"


def test_ordinary_programs_cannot_see_manifest_types(tmp_path: Path) -> None:
    # Manifest/Dependency are builtin ONLY for package files: a user record
    # literal with the same shape must not resolve to them.
    src = 'fn main() -> I64 = { let d = { name = "a", path = "b" }\n 0 }\n'
    flx = tmp_path / "prog.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_check(str(flx)) == 1  # TYPE014: no record type matches


def test_new_creates_runnable_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = tmp_path / "hello-app"
    assert cli_main(["new", str(app)]) == 0
    assert (app / "package.flx").is_file()
    assert (app / "main.flx").is_file()
    manifest = load_manifest(app / "package.flx")
    assert (manifest.name, manifest.version, manifest.entry) == ("hello-app", "0.1.0", "main.flx")

    monkeypatch.chdir(app)
    assert cli_main(["check"]) == 0
    assert cli_main(["test", "--interpret"]) == 0
    out = capsys.readouterr().out
    assert "created" in out
    assert "ok Main / main returns zero" in out


def test_new_refuses_nonempty_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "keep.txt").write_text("do not overwrite", encoding="utf-8")
    assert cli_main(["new", str(app)]) == 1
    assert "not empty" in capsys.readouterr().err


def test_add_dependency_updates_manifest_and_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "app"
    mathlib = tmp_path / "mathlib"
    _write(
        mathlib,
        {
            "package.flx": LIB_MANIFEST,
            "Mathlib.flx": LIB_MODULE,
        },
    )
    assert cli_main(["new", str(app)]) == 0
    monkeypatch.chdir(app)
    assert cli_main(["add", "Mathlib", "../mathlib"]) == 0
    manifest = load_manifest(app / "package.flx")
    assert len(manifest.dependencies) == 1
    assert manifest.dependencies[0].name == "Mathlib"
    assert manifest.dependencies[0].path == "../mathlib"

    (app / "main.flx").write_text(
        "module Main\nimport Mathlib\nfn main() -> I64 = { square(7) }\n",
        encoding="utf-8",
    )
    assert cli_main(["run", "--quiet-status"]) == 49
