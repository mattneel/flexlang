"""Drive the external MLIR/LLVM toolchain to produce and run native binaries.

Pipeline (validated against LLVM/MLIR 22):

    .mlir --mlir-opt(--convert-to-llvm)--> .llvm.mlir
          --mlir-translate(--mlir-to-llvmir)--> .ll
          --clang(.ll + runtime.c)--> executable
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path

from flx.diagnostics import Diagnostic, FlexError

LLVM_BIN = "/usr/lib/llvm-22/bin"

# Tools the native backend (run/test/emit-mlir/build) shells out to. `flx doctor`
# reports on exactly these; pure-Python commands need none of them.
REQUIRED_TOOLS = ("mlir-opt", "mlir-translate", "clang")


def find_tool(name: str) -> str | None:
    """Resolve a backend tool on PATH or in the pinned LLVM dir, or None."""
    found = shutil.which(name)
    if found:
        return found
    candidate = os.path.join(LLVM_BIN, name)
    return candidate if os.path.exists(candidate) else None


def available() -> bool:
    """Whether the full native backend toolchain is resolvable."""
    return all(find_tool(t) is not None for t in REQUIRED_TOOLS)


def _tool(name: str) -> str:
    resolved = find_tool(name)
    if resolved is None:
        raise FlexError(
            [
                Diagnostic(
                    "TOOL000",
                    f"required tool {name!r} not found on PATH or in {LLVM_BIN}",
                    help="run `flx doctor` to check your native toolchain setup",
                )
            ]
        )
    return resolved


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tool = os.path.basename(cmd[0])
        detail = (proc.stderr or proc.stdout).strip()
        raise FlexError([Diagnostic("BACKEND001", f"{tool} failed:\n{detail}")])


@functools.lru_cache(maxsize=1)
def _host_layout() -> tuple[str, str]:
    """The host's LLVM data layout and triple, as clang sees them. The MLIR
    module must carry the REAL layout: mlir-translate constant-folds sizeof
    GEPs (heap-box sizes) using the module's layout, and without one it falls
    back to LLVM defaults (i64 aligned to 4) while clang compiles the loads
    and stores with the host layout (i64 aligned to 8) — the folded malloc
    size comes out smaller than what the store writes."""
    proc = subprocess.run(
        [_tool("clang"), "-S", "-emit-llvm", "-x", "c", os.devnull, "-o", "-"],
        capture_output=True,
        text=True,
    )
    layout = triple = ""
    for line in proc.stdout.splitlines():
        if line.startswith("target datalayout") and '"' in line:
            layout = line.split('"')[1]
        elif line.startswith("target triple") and '"' in line:
            triple = line.split('"')[1]
    return layout, triple


def _wrap_module(mlir_text: str) -> str:
    layout, triple = _host_layout()
    attrs = []
    if layout:
        attrs.append(f'llvm.data_layout = "{layout}"')
    if triple:
        attrs.append(f'llvm.target_triple = "{triple}"')
    if not attrs:
        return mlir_text
    return f"module attributes {{{', '.join(attrs)}}} {{\n{mlir_text}}}\n"


def build_executable(mlir_text: str, c_source: str, out_path: Path, workdir: Path) -> Path:
    mlir_file = workdir / "module.mlir"
    mlir_file.write_text(_wrap_module(mlir_text), encoding="utf-8")

    lowered = workdir / "module.llvm.mlir"
    _run(
        [
            _tool("mlir-opt"),
            str(mlir_file),
            "--convert-to-llvm",
            "--reconcile-unrealized-casts",
            "-o",
            str(lowered),
        ]
    )

    ll = workdir / "module.ll"
    _run([_tool("mlir-translate"), str(lowered), "--mlir-to-llvmir", "-o", str(ll)])

    runtime = workdir / "runtime.c"
    runtime.write_text(c_source, encoding="utf-8")
    # -lm: Std.Math wraps libm, and F64 % lowers to an fmod libcall.
    _run([_tool("clang"), "-O1", str(ll), str(runtime), "-o", str(out_path), "-lm"])
    return out_path


def run_executable(path: Path, args: tuple[str, ...] = ()) -> int:
    # The child shares our stdout/stderr fds: anything still in Python's text
    # buffers must land first, or redirected output interleaves out of
    # execution order (docs check runs interp then native in one process).
    sys.stdout.flush()
    sys.stderr.flush()
    code = subprocess.run([str(path), *args]).returncode
    # A signal death (e.g. an extern abort()) comes back negative from Python;
    # report it as a shell would (128 + signal), which is also what the
    # interpreter path yields when the same C call kills its process.
    return 128 - code if code < 0 else code
