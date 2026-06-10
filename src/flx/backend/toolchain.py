"""Drive the external MLIR/LLVM toolchain to produce and run native binaries.

Pipeline (validated against LLVM/MLIR 22):

    .mlir --mlir-opt(--convert-to-llvm)--> .llvm.mlir
          --mlir-translate(--mlir-to-llvmir)--> .ll
          --clang(.ll + runtime.c)--> executable
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from flx.diagnostics import Diagnostic, FlexError

_LLVM_BIN = "/usr/lib/llvm-22/bin"


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    candidate = os.path.join(_LLVM_BIN, name)
    if os.path.exists(candidate):
        return candidate
    raise FlexError(
        [Diagnostic("TOOL000", f"required tool {name!r} not found on PATH or in {_LLVM_BIN}")]
    )


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tool = os.path.basename(cmd[0])
        detail = (proc.stderr or proc.stdout).strip()
        raise FlexError([Diagnostic("BACKEND001", f"{tool} failed:\n{detail}")])


def build_executable(mlir_text: str, c_source: str, out_path: Path, workdir: Path) -> Path:
    mlir_file = workdir / "module.mlir"
    mlir_file.write_text(mlir_text, encoding="utf-8")

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
    _run([_tool("clang"), "-O1", str(ll), str(runtime), "-o", str(out_path)])
    return out_path


def run_executable(path: Path, args: tuple[str, ...] = ()) -> int:
    return subprocess.run([str(path), *args]).returncode
