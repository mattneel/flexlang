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

The MVP compiler is **working end-to-end** — it lexes, parses, type-checks,
emits MLIR, lowers through LLVM 22, and produces native binaries:

```sh
flx parse     examples/add.flx     # AST
flx check     examples/add.flx     # type + name checking
flx emit-mlir examples/add.flx     # textual MLIR (func/arith/cf/memref)
flx run       examples/add.flx     # compile to native and run (exit code 42)
flx test      examples/add.flx     # compile + run first-class tests
```

```console
$ flx test examples/add.flx
running 1 test

ok Main / add works

1 passed, 0 failed
```

The full §3.1 MVP feature set is implemented and lowers to native code:

- integer/bool literals, `let`/`mut`, arithmetic, comparisons, short-circuit
  boolean ops, `if`/`else`, `while`, functions, calls, and the pipe operator;
- **records** (`type T = { … }`, construction, field access, `{ r with f = v }`);
- **ADTs** + generic `Result<T,E>`/`Option<T>` (monomorphized), **`match`** with
  exhaustiveness checking, and the **`?`** operator;
- **effects** — `uses { … }` is checked across the call graph;
- **regions** — `region name { … }` (shallow);
- runtime-backed **string** literals (so `Log.info`/`fail` print);
- first-class `test` blocks with `assert`/`assert_eq`/`assert_ne`/`fail`.

See `examples/` for `add`, `result`, `records`, `effects`, and `regions`.
The remaining `flx` subcommands (`build`, `emit-hir`, `emit-mir`, `expand`,
`explain-*`) are still scaffolded stubs; macros/comptime, the borrow checker,
and a standard library remain future work (`docs/MVP.md` §3.2).

## Syntax highlighting

Flex ships a [Pygments](https://pygments.org) lexer, so `.flx` renders nicely
in the terminal and in docs:

```sh
flx highlight examples/add.flx                 # auto-detects truecolor/256
flx highlight examples/add.flx --style github-dark
flx highlight examples/add.flx --format html > add.html
```

The lexer is registered as a Pygments plugin (alias `flex`), so any
Pygments-aware tool works too:

```sh
pygmentize -l flex examples/add.flx
pygmentize examples/add.flx            # picks Flex by the .flx extension
```

> Note: Pygments' built-in **Felix** lexer also claims `*.flx`; the Flex lexer
> sets a higher priority so extension-based lookup resolves to Flex. Use the
> `flex` alias (not `flx`) for unambiguous explicit selection.

## Documentation

The docs are an [mdBook](https://rust-lang.github.io/mdBook/) under `docs/`,
published to GitHub Pages at **<https://mattneel.github.io/flexlang/>** by the
`Deploy docs` workflow. Every Flex code block in the book is highlighted by the
project's own Pygments lexer via the `flx-mdbook` preprocessor.

```sh
mise run docs          # build to book/
mise run docs-serve    # live-reload preview
```

> First-time Pages setup: in the GitHub repo, **Settings → Pages → Build and
> deployment → Source: GitHub Actions**.

## Continuous integration

Two workflows in `.github/workflows/`:

- **CI** (`ci.yml`) — on push/PR: `ruff check`, `ruff format --check`, `mypy`,
  `pytest`, and an mdBook build.
- **Deploy docs** (`docs.yml`) — on push to the default branch: build the book
  and deploy to Pages.

Run them locally with [`act`](https://github.com/nektos/act):

```sh
act -j check -P ubuntu-latest=catthehacker/ubuntu:act-latest
act -j docs  -P ubuntu-latest=catthehacker/ubuntu:act-latest
```

## Layout

```
book.toml            mdBook config (src = docs/, flex preprocessor)
docs/                mdBook sources (SUMMARY.md, MVP.md spec, chapters)
mise.toml            tool pins, env, tasks
pyproject.toml       package metadata, deps, ruff/mypy/pytest config
scripts/             LLVM install + toolchain inspection
src/flx/             compiler package: cli, highlight/ lexer, mdbook preprocessor
tests/               pytest suite
examples/            sample .flx programs
.github/workflows/   CI + Pages deploy
```
