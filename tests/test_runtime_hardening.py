"""Hostile-input probes for the native C runtime support library."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from flx.backend.runtime import BASE_RUNTIME_C

cc = pytest.mark.skipif(shutil.which("cc") is None, reason="C compiler not available")


def _run_probe(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    src = tmp_path / "probe.c"
    exe = tmp_path / "probe"
    src.write_text(
        BASE_RUNTIME_C + "\n#include <limits.h>\n" + "int main(void) {\n" + body + "\n}\n",
        encoding="utf-8",
    )
    subprocess.run(["cc", str(src), "-o", str(exe)], check=True, capture_output=True, text=True)
    return subprocess.run([str(exe)], capture_output=True, text=True)


@cc
def test_runtime_rejects_negative_path_lengths_cleanly(tmp_path: Path) -> None:
    proc = _run_probe(tmp_path, 'char *p = __flx_path_copy("x", -1); (void)p; return 0;')

    assert proc.returncode == 1
    assert "flx: runtime error: negative byte length" in proc.stderr


@cc
def test_runtime_rejects_string_length_overflow(tmp_path: Path) -> None:
    proc = _run_probe(
        tmp_path,
        'FlxStr out; __flx_str_concat("a", LLONG_MAX, "b", 1, &out); return 0;',
    )

    assert proc.returncode == 1
    assert "flx: runtime error: allocation size overflow" in proc.stderr


@cc
def test_runtime_rejects_list_capacity_overflow(tmp_path: Path) -> None:
    proc = _run_probe(
        tmp_path,
        "FlxList l = { LLONG_MAX / 2 + 1, LLONG_MAX / 2 + 1, 0 }; "
        "__flx_list_push(&l, 1); return 0;",
    )

    assert proc.returncode == 1
    assert "flx: runtime error: container capacity overflow" in proc.stderr


@cc
def test_runtime_rejects_negative_map_key_lengths_cleanly(tmp_path: Path) -> None:
    proc = _run_probe(
        tmp_path,
        'void *m = __flx_map_new(); __flx_map_set(m, "x", -1, 0); return 0;',
    )

    assert proc.returncode == 1
    assert "flx: runtime error: negative byte length" in proc.stderr
