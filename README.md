# Flex

**Flex** is a native functional programming language for explicit systems
programming — F#/Elm-style syntax, Elixir-style pipes, explicit `Result`
failure, explicit effects, region-based allocation, and first-class tests,
compiled through MLIR/LLVM to native code.

The full language spec and MVP plan live in [`docs/MVP.md`](docs/MVP.md).

This repository hosts the **prototype compiler**, written in Python with
[xDSL](https://github.com/xdslproject/xdsl) for MLIR construction and the
LLVM/MLIR 22 toolchain for lowering to native binaries.

## Toolchain

| Component   | Version | Provided by |
|-------------|---------|-------------|
| Python      | 3.14.2  | mise        |
| uv          | 0.9.24  | mise        |
| LLVM / MLIR | 22.1.7  | apt.llvm.org (`scripts/install-llvm.sh`) |
| lark, xdsl  | latest  | uv / pyproject |

The compiler shells out to `mlir-opt`, `mlir-translate`, `llc`, and `clang`
from LLVM 22. mise prepends `/usr/lib/llvm-22/bin` to `PATH` for this repo so
they resolve to v22 even though the system default is LLVM 18.

## Setup

Prerequisite: [mise](https://mise.jdx.dev) and `sudo` access (for the system
LLVM packages). Everything else is bootstrapped:

```sh
mise install          # pinned Python 3.14.2 + uv
mise run bootstrap     # install LLVM/MLIR 22 (sudo) + uv sync the venv
```

`mise run bootstrap` is split into two reusable steps if you prefer:

```sh
bash scripts/install-llvm.sh   # idempotent apt.llvm.org install of LLVM/MLIR 22
uv sync                         # create .venv and install locked deps
```

Verify the environment:

```sh
mise run toolchain     # show resolved clang/llc/mlir-opt and versions
mise run check         # ruff + mypy + pytest
flx --version
```

## Common tasks

| Command            | Description                          |
|--------------------|--------------------------------------|
| `mise run sync`    | refresh `.venv` from `uv.lock`       |
| `mise run test`    | run the pytest suite                 |
| `mise run lint`    | `ruff check`                         |
| `mise run fmt`     | `ruff format`                        |
| `mise run typecheck` | `mypy`                             |
| `mise run check`   | lint + typecheck + test              |

The `flx` CLI surface (see `docs/MVP.md` §18) is scaffolded; subcommands are
implemented incrementally:

```sh
flx parse examples/add.flx
flx check examples/add.flx
flx test  examples/add.flx
flx run   examples/hello.flx
flx emit-mlir examples/add.flx
```

## Layout

```
docs/MVP.md          language spec + MVP plan
mise.toml            tool pins, env, tasks
pyproject.toml       package metadata, deps, ruff/mypy/pytest config
scripts/             LLVM install + toolchain inspection
src/flx/             compiler package (cli today; pipeline grows here)
tests/               pytest suite
examples/            sample .flx programs
```
