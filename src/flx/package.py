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

import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flx import interp
from flx.diagnostics import Diagnostic, FlexError
from flx.macro import expand
from flx.sema.specialize import check_and_monomorphize
from flx.syntax.parser import parse
from flx.types import STRING, ListType, RecordType

MANIFEST_FILE = "package.flx"
LOCK_FILE = "flex.lock"
VENDOR_DIR = "vendor"
_HASH_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    VENDOR_DIR,
}
_HASH_SKIP_FILES = {LOCK_FILE}

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
    except (OSError, UnicodeDecodeError) as exc:
        raise _err("PKG001", f"cannot read {manifest_path}: {exc}") from None

    module = expand(parse(source, str(manifest_path)))
    if module.targets:
        raise _err(
            "PKG006",
            f"{manifest_path} declares build targets",
            help="a manifest is pure data; put targets in build.flx",
        )
    if module.externs:
        # An extern's purity is author-asserted, which is exactly the trust a
        # manifest must not require: manifests stay FFI-free so "reading a
        # manifest runs no foreign code" holds unconditionally.
        raise _err(
            "PKG007",
            f"{manifest_path} declares extern fns",
            help="a manifest is pure data; call C from the program or build.flx",
        )
    result = check_and_monomorphize(module, builtin_records=BUILTIN_RECORDS)

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

    # Purity (no effects) is proven by the checker; termination is enforced by a
    # step budget — a manifest must be data, not a workload. Faults surface as
    # clean diagnostics, never tracebacks.
    interp._ensure_recursion_headroom()
    try:
        value = interp.Interpreter(result, max_steps=1_000_000).call(fn, [])
    except (interp.FlexRuntimeError, RecursionError) as exc:
        reason = "stack overflow (recursion too deep)" if isinstance(exc, RecursionError) else exc
        raise _err("PKG005", f"error while evaluating {manifest_path}: {reason}") from None
    if not isinstance(value, dict):
        raise _err("PKG002", f"manifest() evaluated to {type(value).__name__}, expected Manifest")
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
    lock = _load_lock(manifest.dir, required=False)

    def collect(m: PackageManifest) -> None:
        for dep in m.dependencies:
            dep_dir = _resolve_dependency_dir(manifest.dir, m, dep, lock)
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


def _hash_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in _HASH_SKIP_DIRS for part in rel.parts):
            continue
        if path.name in _HASH_SKIP_FILES:
            continue
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def tree_hash(root: Path) -> str:
    """Deterministic content hash for a package directory."""
    h = hashlib.sha256()
    try:
        for path in _hash_files(root):
            rel = path.relative_to(root).as_posix().encode()
            data = path.read_bytes()
            h.update(rel)
            h.update(b"\0")
            h.update(str(len(data)).encode())
            h.update(b"\0")
            h.update(data)
            h.update(b"\0")
    except OSError as exc:
        raise _err("PKG008", f"cannot hash package {root}: {exc}") from None
    return h.hexdigest()


def _manifest_for_dependency(dep_dir: Path, dep: PackageDep) -> PackageManifest:
    manifest_file = dep_dir / MANIFEST_FILE
    if manifest_file.is_file():
        return load_manifest(manifest_file)
    return PackageManifest(dep.name, "", "", (), dep_dir)


def _entry_for(
    root: PackageManifest, parent: PackageManifest, dep: PackageDep, *, vendor: bool
) -> dict[str, Any]:
    dep_dir = (parent.dir / dep.path).resolve()
    if not dep_dir.is_dir():
        raise _err(
            "PKG004", f"dependency {dep.name!r} of {parent.name!r}: no such directory {dep_dir}"
        )
    dep_manifest = _manifest_for_dependency(dep_dir, dep)
    entry: dict[str, Any] = {
        "dependency": dep.name,
        "package": dep_manifest.name,
        "version": dep_manifest.version,
        "source": {"type": "path", "path": dep.path},
        "sha256": tree_hash(dep_dir),
    }
    if vendor:
        entry["vendor"] = f"{VENDOR_DIR}/{dep.name}"
    return entry


def _collect_lock_entries(root: PackageManifest, *, vendor: bool) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    visited: set[Path] = {root.dir}

    def collect(parent: PackageManifest) -> None:
        for dep in parent.dependencies:
            entry = _entry_for(root, parent, dep, vendor=vendor)
            old = seen.get(dep.name)
            if old is not None and old["sha256"] != entry["sha256"]:
                raise _err("PKG008", f"dependency {dep.name!r} resolves to multiple hashes")
            if old is None:
                seen[dep.name] = entry
                entries.append(entry)
            dep_dir = (parent.dir / dep.path).resolve()
            if dep_dir in visited:
                continue
            visited.add(dep_dir)
            nested = dep_dir / MANIFEST_FILE
            if nested.is_file():
                collect(load_manifest(nested))

    collect(root)
    return entries


def _lock_payload(root: PackageManifest, *, vendor: bool) -> dict[str, Any]:
    return {"version": 1, "packages": _collect_lock_entries(root, vendor=vendor)}


def _write_lock(root: PackageManifest, payload: dict[str, Any]) -> Path:
    path = root.dir / LOCK_FILE
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _load_lock(root: Path, *, required: bool) -> dict[str, dict[str, Any]] | None:
    path = root / LOCK_FILE
    if not path.is_file():
        if required:
            raise _err("PKG008", f"{LOCK_FILE} not found; run `flx deps lock`")
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        packages = raw["packages"]
        if raw.get("version") != 1 or not isinstance(packages, list):
            raise ValueError("unsupported lockfile schema")
        entries = {str(p["dependency"]): p for p in packages}
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise _err("PKG008", f"invalid {path}: {exc}") from None
    return entries


def _verify_entry(dep_name: str, dep_dir: Path, entry: dict[str, Any]) -> None:
    actual = tree_hash(dep_dir)
    expected = str(entry.get("sha256", ""))
    if actual != expected:
        raise _err(
            "PKG008",
            f"dependency {dep_name!r} hash mismatch: expected {expected}, got {actual}",
        )


def _resolve_dependency_dir(
    root_dir: Path,
    parent: PackageManifest,
    dep: PackageDep,
    lock: dict[str, dict[str, Any]] | None,
) -> Path:
    source_dir = (parent.dir / dep.path).resolve()
    if lock is None:
        return source_dir
    entry = lock.get(dep.name)
    if entry is None:
        raise _err("PKG008", f"dependency {dep.name!r} is missing from {LOCK_FILE}")
    vendor_rel = entry.get("vendor")
    if isinstance(vendor_rel, str):
        vendor_dir = (root_dir / vendor_rel).resolve()
        if vendor_dir.is_dir():
            _verify_entry(dep.name, vendor_dir, entry)
            return vendor_dir
    if source_dir.is_dir():
        _verify_entry(dep.name, source_dir, entry)
    return source_dir


def _root_manifest(path: str | None = None) -> PackageManifest:
    if path is None:
        manifest_file = find_package()
        if manifest_file is None:
            raise _err("PKG001", f"no {MANIFEST_FILE} in the current directory")
        return load_manifest(manifest_file)
    p = Path(path)
    manifest_file = p / MANIFEST_FILE if p.is_dir() else p
    return load_manifest(manifest_file)


def _copy_package(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)

    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in _HASH_SKIP_DIRS or n in _HASH_SKIP_FILES}

    shutil.copytree(src, dst, ignore=ignore)


def lock_deps(path: str | None = None) -> Path:
    manifest = _root_manifest(path)
    return _write_lock(manifest, _lock_payload(manifest, vendor=False))


def vendor_deps(path: str | None = None) -> Path:
    manifest = _root_manifest(path)
    payload = _lock_payload(manifest, vendor=True)
    for entry in payload["packages"]:
        dep_name = str(entry["dependency"])
        source = entry["source"]
        if not isinstance(source, dict) or source.get("type") != "path":
            raise _err("PKG008", f"dependency {dep_name!r} has unsupported source")
        dep = next((d for d in manifest.dependencies if d.name == dep_name), None)
        if dep is None:
            continue
        src = (manifest.dir / dep.path).resolve()
        dst = (manifest.dir / str(entry["vendor"])).resolve()
        _copy_package(src, dst)
    return _write_lock(manifest, payload)


def verify_deps(path: str | None = None) -> None:
    manifest = _root_manifest(path)
    lock = _load_lock(manifest.dir, required=True)
    if lock is None:
        raise _err("PKG008", f"{LOCK_FILE} not found; run `flx deps lock`")
    dependency_roots(manifest)


def cmd_deps_lock(path: str | None = None) -> int:
    try:
        lock = lock_deps(path)
    except FlexError as err:
        for diag in err.diagnostics:
            print(f"error[{diag.code}]: {diag.message}", file=sys.stderr)
        return 1
    print(f"wrote {lock}")
    return 0


def cmd_deps_vendor(path: str | None = None) -> int:
    try:
        lock = vendor_deps(path)
    except FlexError as err:
        for diag in err.diagnostics:
            print(f"error[{diag.code}]: {diag.message}", file=sys.stderr)
        return 1
    print(f"vendored dependencies and wrote {lock}")
    return 0


def cmd_deps_verify(path: str | None = None) -> int:
    try:
        verify_deps(path)
    except FlexError as err:
        for diag in err.diagnostics:
            print(f"error[{diag.code}]: {diag.message}", file=sys.stderr)
        return 1
    print("dependencies verified")
    return 0
