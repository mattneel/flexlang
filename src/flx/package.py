"""`package.flx` — the package manifest, as a pure Flex value.

There is no TOML: a package describes itself in Flex. The manifest is a typed
record returned by a pure function,

    module Package

    fn manifest() -> Manifest = {
      {
        name = "app",
        version = "0.1.0",
        entry = "main.flx",
        dependencies = [ { name = "Mathlib", path = "../mathlib" } ]
      }
    }

and is read by *evaluating* `manifest()` in the interpreter with ZERO effect
capabilities. The effect system is the data/logic boundary: `manifest()` may not
declare `uses { ... }` (PKG003), and the checker already rejects any effectful
call from an effect-free function — so reading a manifest is provably pure data
extraction. Build logic lives in `build.flx`, which is the opposite: effectful,
declared targets (see flx/build.py).

The `Manifest` / `Dependency` record types are builtin ONLY while checking a
package file, so ordinary programs' record literals can never resolve to them.

Dependencies are path dependencies for now: each names a directory (relative to
the depending package) that becomes an additional import-resolution root, and
whose own `package.flx` (if present) contributes its dependencies transitively.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from flx import interp
from flx.diagnostics import Diagnostic, FlexError
from flx.macro import expand
from flx.sema.check import check
from flx.syntax.parser import parse
from flx.types import STRING, ListType, RecordType

MANIFEST_FILE = "package.flx"

DEPENDENCY_TYPE = RecordType("Dependency", (("name", STRING), ("path", STRING)))
MANIFEST_TYPE = RecordType(
    "Manifest",
    (
        ("name", STRING),
        ("version", STRING),
        ("entry", STRING),
        ("dependencies", ListType(DEPENDENCY_TYPE)),
    ),
)
BUILTIN_RECORDS = {"Manifest": MANIFEST_TYPE, "Dependency": DEPENDENCY_TYPE}


@dataclass(frozen=True)
class PackageDep:
    name: str
    path: str


@dataclass(frozen=True)
class PackageManifest:
    name: str
    version: str
    entry: str
    dependencies: tuple[PackageDep, ...]
    dir: Path  # directory containing package.flx


def _err(code: str, message: str, *, help: str | None = None) -> FlexError:
    return FlexError([Diagnostic(code, message, None, help=help)])


def find_package(start: Path | None = None) -> Path | None:
    """The `package.flx` governing `start` (default: cwd), or None."""
    here = (start or Path.cwd()).resolve()
    candidate = here / MANIFEST_FILE
    return candidate if candidate.is_file() else None


def load_manifest(manifest_path: Path) -> PackageManifest:
    """Parse, type-check, and purely evaluate a `package.flx`."""
    try:
        source = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _err("PKG001", f"cannot read {manifest_path}: {exc}") from None

    module = expand(parse(source, str(manifest_path)))
    result = check(module, builtin_records=BUILTIN_RECORDS)

    fn = result.module.functions and {f.name: f for f in result.module.functions}.get("manifest")
    fn_ty = result.functions.get("manifest")
    if not fn or fn_ty is None:
        raise _err(
            "PKG002",
            f"{manifest_path} does not define `fn manifest() -> Manifest`",
        )
    if fn.params:
        raise _err("PKG002", "manifest() must take no arguments")
    if fn.effects:
        raise _err(
            "PKG003",
            "manifest() must be pure — it may not declare `uses { ... }`",
            help="a manifest is data; put effectful build logic in build.flx targets",
        )
    if fn_ty.ret != MANIFEST_TYPE:
        raise _err("PKG002", f"manifest() must return Manifest, not {fn_ty.ret}")

    value = interp.Interpreter(result).call(fn, [])
    assert isinstance(value, dict)  # guaranteed by the Manifest return type
    deps = tuple(PackageDep(d["name"], d["path"]) for d in value["dependencies"])
    return PackageManifest(
        name=value["name"],
        version=value["version"],
        entry=value["entry"],
        dependencies=deps,
        dir=manifest_path.resolve().parent,
    )


def dependency_roots(manifest: PackageManifest) -> tuple[Path, ...]:
    """Import-resolution roots contributed by `manifest`'s dependencies,
    transitively (a dep directory with its own package.flx brings its deps)."""
    roots: list[Path] = []
    visited: set[Path] = {manifest.dir}

    def collect(m: PackageManifest) -> None:
        for dep in m.dependencies:
            dep_dir = (m.dir / dep.path).resolve()
            if not dep_dir.is_dir():
                raise _err(
                    "PKG004",
                    f"dependency {dep.name!r} of {m.name!r}: no such directory {dep_dir}",
                )
            if dep_dir in visited:
                continue
            visited.add(dep_dir)
            roots.append(dep_dir)
            nested = dep_dir / MANIFEST_FILE
            if nested.is_file():
                collect(load_manifest(nested))

    collect(manifest)
    return tuple(roots)
