"""Release preflight checks for publishing Flex packages."""

from __future__ import annotations

import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

_FORBIDDEN_NAMES = {"ADVERSARIAL_REVIEW.md"}
_FORBIDDEN_DIRS = {"book", "dist"}


def _dirty_paths(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return ["<not a git worktree>"]
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _forbidden_member(name: str) -> bool:
    parts = Path(name).parts
    basename = parts[-1] if parts else name
    if basename.startswith("STUDY-") and basename.endswith(".md"):
        return True
    if basename in _FORBIDDEN_NAMES:
        return True
    return any(part in _FORBIDDEN_DIRS for part in parts[1:])


def forbidden_sdist_members(sdist: Path) -> list[str]:
    with tarfile.open(sdist, "r:gz") as tar:
        return sorted(m.name for m in tar.getmembers() if _forbidden_member(m.name))


def _built_sdists(out_dir: Path) -> list[Path]:
    return sorted(out_dir.glob("*.tar.gz"))


def preflight(root: Path | None = None, *, allow_dirty: bool = False, build: bool = True) -> int:
    root = (root or Path.cwd()).resolve()
    dirty = _dirty_paths(root)
    if dirty and not allow_dirty:
        print("flx release: working tree is not clean", file=sys.stderr)
        for line in dirty[:20]:
            print(f"  {line}", file=sys.stderr)
        if len(dirty) > 20:
            print(f"  ... {len(dirty) - 20} more", file=sys.stderr)
        return 1

    if build:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            proc = subprocess.run(["uv", "build", "--out-dir", str(out_dir)], cwd=root)
            if proc.returncode != 0:
                return proc.returncode
            for sdist in _built_sdists(out_dir):
                bad = forbidden_sdist_members(sdist)
                if bad:
                    print(f"flx release: forbidden files in {sdist.name}", file=sys.stderr)
                    for name in bad:
                        print(f"  {name}", file=sys.stderr)
                    return 1
    print("release preflight ok")
    return 0


def cmd_preflight(*, allow_dirty: bool = False) -> int:
    return preflight(allow_dirty=allow_dirty)
