"""`package.flx` manifests: pure evaluation, validation, and path dependencies."""

from __future__ import annotations

from pathlib import Path

import pytest

from flx import driver
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


def test_ordinary_programs_cannot_see_manifest_types(tmp_path: Path) -> None:
    # Manifest/Dependency are builtin ONLY for package files: a user record
    # literal with the same shape must not resolve to them.
    src = 'fn main() -> I64 = { let d = { name = "a", path = "b" }\n 0 }\n'
    flx = tmp_path / "prog.flx"
    flx.write_text(src, encoding="utf-8")
    assert driver.cmd_check(str(flx)) == 1  # TYPE014: no record type matches
