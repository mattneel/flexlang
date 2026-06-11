# Flex

**Flex** is a native functional programming language for explicit systems
programming — F#/Elm-style syntax, Elixir-style pipes, explicit `Result`
failure, explicit effects, region-based allocation, hygienic comptime macros,
traits/generics, and first-class tests.

The prototype is **working end-to-end**: it can parse, typecheck, expand macros,
**run programs through a pure-Python interpreter**, emit MLIR, and compile native
binaries through LLVM/MLIR 22. It is not just a syntax highlighter — it runs.

The canonical language spec and MVP plan live in [`docs/MVP.md`](docs/MVP.md).

## Install

The compiler ships on PyPI as **`flexlang`** (the command it installs is `flx`).
Because the core is **pure Python**, you can check, expand, **run, and test**
Flex with no LLVM at all — the fastest way to try it is with
[uv](https://docs.astral.sh/uv/). No clone, no setup, no toolchain:

```sh
echo 'fn main() -> I64 = { 40 + 2 }' > hello.flx
uvx --from flexlang flx run   hello.flx     # interpreter — exits 42
uvx --from flexlang flx check hello.flx
```

Cloned this repo? The bundled examples show the rest:

```sh
uvx --from flexlang flx test      examples/macros.flx
uvx --from flexlang flx expand    examples/macros.flx
uvx --from flexlang flx highlight examples/traits.flx
```

`parse`, `check`, `expand`, `highlight`, `run`, `test`, and `doctor` all work
from the bare install (one dependency, `pygments`) — `run` and `test` execute on
a tree-walking interpreter. To keep `flx` around:

```sh
uv tool install --from flexlang flx     # then just `flx <command>`
```

> The PyPI distribution is `flexlang` (the name `flx` is too close to an existing
> project), so `--from flexlang` tells uv the package behind the `flx` command.

### Native backend (optional)

The native LLVM backend is an **optimizing path, not a requirement**. It needs a
system **MLIR/LLVM 22** toolchain (`mlir-opt`, `mlir-translate`, `clang`) and is
used only for `flx build`, `flx run --native`, `flx test --native`, and
`flx emit-mlir`. It is deliberately **not** a Python dependency, so the base
install stays light. Check what you have with `flx doctor`; on Debian/Ubuntu,
`scripts/install-llvm.sh` (or [apt.llvm.org](https://apt.llvm.org)) installs it.

The interpreter and the native backend are differential-tested to produce
identical output, so `--native` only changes performance, not behavior.

## Toolchain

| Component   | Version | Provided by |
|-------------|---------|-------------|
| Python      | 3.14.2  | mise        |
| uv          | 0.9.24  | mise        |
| LLVM / MLIR | 22.1.7  | apt.llvm.org (`scripts/install-llvm.sh`) |
| pygments    | 2.19+   | uv / pyproject (only runtime dependency) |

The compiler shells out to `mlir-opt`, `mlir-translate`, and `clang` from LLVM
22. mise prepends `/usr/lib/llvm-22/bin` to `PATH` for this repo so they resolve
to v22 even though the system default is LLVM 18.

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

Run a program two ways — the interpreter (default, no LLVM) or native code:

```sh
flx parse     examples/add.flx     # AST
flx check     examples/add.flx     # type + name checking
flx emit-mlir examples/add.flx     # textual MLIR (func/arith/cf/memref)
flx run       examples/add.flx     # interpret and run (exit code 42)
flx run --native examples/add.flx  # compile through LLVM 22 and run
flx test      examples/add.flx     # run first-class tests
```

```console
$ flx test examples/add.flx
running 1 test

ok Main / add works

1 passed, 0 failed
```

### Implemented (runs on both the interpreter and native LLVM)

- integer/bool literals, `let`/`mut`, arithmetic (64-bit wrapping; guarded
  div/mod), comparisons, short-circuit boolean ops, `if`/`else`, `while`,
  functions, calls, and the pipe operator;
- **records** (`type T = { … }`, construction, field access, `{ r with f = v }`);
- **ADTs** + generic `Result<T,E>`/`Option<T>` (monomorphized) — **recursive
  and mutually recursive types**, **multi-field payloads** (`Rect(I64, I64)`),
  and String/record/ADT payloads on both backends (boxed natively); **`match`**
  with exhaustiveness checking, nested constructor patterns, integer/bool
  literal patterns, block arm bodies, and the **`?`** operator
  ([ADTs and Pattern Matching](https://mattneel.github.io/flexlang/adts.html));
- **traits & generics** — `trait`/`impl` with static method dispatch, `derive`d
  impls, and bounded generic functions (`fn f<T: Show>(…)`) by monomorphization
  (see [Traits and Generics](https://mattneel.github.io/flexlang/traits.html));
- **effects** — `uses { … }` checked across the call graph;
- runtime-backed **string** literals (`++` concat, `to_str`);
- first-class `test` blocks (`assert`/`assert_eq`/`assert_ne`/`fail`);
- **compile-time metaprogramming** (`docs/MVP.md` §10): `comptime { }` folding,
  hygienic `quote`/`unquote` **macros**, `reflect.fields` + comptime `for` +
  `unquote_splice`, and `derive(Eq, Show)` — viewable with `flx expand`;
- **multi-file modules** — `import A.B` (path-resolved) with `pub`/private
  visibility, merged into one program;
- **packages** — `package.flx`, a *typed, provably pure* Flex manifest (no TOML)
  with path dependencies; `flx run`/`test`/`check` need no arguments inside a
  package ([Packages and Builds](https://mattneel.github.io/flexlang/packages.html));
- **`build.flx`** — builds are Flex programs: effect-checked `target`s where
  shelling out requires `Process`, driving the compiler requires `Fs`, calling a
  target demands its effects, and `?` propagates failure through the memoized
  build graph. This repo [builds itself](build.flx) with it: `flx build`;
- **FFI** — `extern fn` declares a C symbol's signature *and its effects* (a
  trust declaration the effect system holds callers to). I64/String cross the
  ABI; works on both backends — the interpreter calls libc via `ctypes`, so
  even `uvx … flx run` can call C with no toolchain
  ([FFI — Calling C](https://mattneel.github.io/flexlang/ffi.html));
- **numerics + function values** — `F64` (IEEE-754, shortest-round-trip
  `to_str`, doubles across the FFI so `Std.Math` wraps libm), hex/binary
  literals, bitwise ops (`& | ^ << >>`, Rust-style precedence, masked shifts),
  and pure functions as values (`(I64) -> I64` parameter types) powering
  `Std.List.map`/`filter`/`fold`
  ([Numbers, Bits, and Function Values](https://mattneel.github.io/flexlang/numerics.html));
- **lists** — `List<T>` on both backends: `[1, 2, 3]` literals, `xs[i]`
  indexing (bounds-checked), `List.push`/`len`/`set`, `for x in xs { … }`,
  elements of any type, `let xs: List<I64> = []` annotations, and `Env.argv()`
  for program arguments
  ([Lists and Iteration](https://mattneel.github.io/flexlang/lists.html));
- **standard library** — written in Flex, shipped in the package, importable
  everywhere (`import Std.Math`): `Std.Math`/`Str`/`Env`/`Time`/`Proc`/`List`,
  with string equality via `impl Eq for String` and byte-level string tools
  (`char_at`/`substr`/`split`/`parse_int`)
  ([Standard Library](https://mattneel.github.io/flexlang/std.html)).

### Prototype / partial

- **regions** — `region name { … }` parses and checks, but lifetime/escape
  analysis is shallow (scalars copy out, so nothing dangles yet); boxed ADT
  payloads and lists are heap-allocated and reclaimed at process exit
  (region-based reclamation is the roadmap).

### Planned

- versioned/registry dependencies, `Std.Fs`/collections (need the allocation
  story), the borrow checker, and
  the `emit-hir`/`emit-mir`/`explain-*` subcommands (still stubs) — see
  `docs/MVP.md` §3.2.

See `examples/` for `add`, `result`, `records`, `effects`, `regions`, `macros`,
`traits`, `ffi`, `std`, and the two-package `package-demo/`.

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
