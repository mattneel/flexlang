# Command-Line Interface

The compiler ships a single `flx` executable. The default execution path is the
portable interpreter; pass `--native` to `run` or `test` when you want the
LLVM/MLIR backend.

| Command | Purpose | Status |
|---------|---------|--------|
| `flx parse <file>` | Parse and print the AST | ✅ |
| `flx check <file>` | Resolve and typecheck | ✅ |
| `flx emit-mlir <file>` | Emit MLIR text | ✅ |
| `flx run [file] [args...]` | Run on the interpreter by default (`--native` for LLVM) | ✅ |
| `flx test [path]` | Discover, compile, and run tests | ✅ |
| `flx docs check [path]` | Prove doc examples and expected errors | ✅ |
| `flx docs build [path]` | Render doc declarations into Markdown | ✅ |
| `flx deps <lock|vendor|verify>` | Lock, vendor, and verify package dependencies | ✅ |
| `flx expand <file>` | Show comptime/macro/derive expansion | ✅ |
| `flx highlight <file>` | Syntax-highlight `.flx` | ✅ |
| `flx build <file> -o <bin>` | Build a native executable | working |
| `flx release preflight` | Check release readiness before publish | ✅ |
| `flx emit-hir <file>` | Emit typed HIR | stub |
| `flx emit-mir <file>` | Emit MIR | stub |
| `flx explain-effects <file>` | Explain effects | stub |
| `flx explain-cost <file>` | Explain allocation/cost | stub |

## `flx highlight`

```sh
flx highlight examples/add.flx                 # auto-detects truecolor / 256
flx highlight examples/add.flx --style github-dark
flx highlight examples/add.flx --format html > add.html
```

`--format` accepts `auto`, `ansi`, `ansi256`, `truecolor`, and `html`;
`--style` accepts any Pygments style name. See
[Syntax Highlighting](highlighting.md) for details.

## `flx run`

```sh
flx run tool.flx alpha beta
flx run --native tool.flx alpha beta
flx run              # with package.flx: run the manifest entry
```

Everything after the file is passed to the program as `Env.argv()` user
arguments; argv[0] is excluded so interpreter and native binaries agree. Put
`flx` flags before the file. A leading `--` after the file is stripped, so
`flx run tool.flx -- --flag` passes `--flag`.

## `flx test`

```sh
flx test examples/add.flx
flx test examples --interpret
flx test src --native --filter parser
flx test examples/add.flx --format json
flx test examples/add.flx --format junit
```

When the path is a directory, `flx test` recursively runs every `.flx` source
file under it, skipping `package.flx` manifests and `build.flx` build files.
Each discovered source file is still checked as a normal entry, including any
package dependency roots found beside it.

`--format pretty` is the default. `json` and `junit` are available on the
interpreter backend for CI consumers that need machine-readable test reports.
The native test harness currently emits pretty output only.

## `flx deps`

```sh
flx deps lock
flx deps verify
flx deps vendor
```

`lock` writes `flex.lock` with exact content hashes for path dependencies.
`verify` checks the current dependency trees against that lock. `vendor` copies
dependencies into `vendor/` and records those paths in the lockfile, so a
reviewed package can build offline without changing `package.flx`.

## `flx release`

```sh
flx release preflight
```

The release preflight fails on a dirty git worktree, builds the wheel/sdist in
a temporary directory, and rejects source distributions that contain local
study/review reports or generated build output.

## `flx docs`

```sh
flx docs check              # prove bundled compiler/std docs
flx docs build --check      # CI drift check for this repository's generated docs
flx docs check src          # prove doc declarations in a local file or directory
flx docs build src --output site
flx docs explain TYPE019
```

`flx docs check <path>` runs nested doc tests and exact `expect_error`
examples for local libraries. `flx docs build <path>` renders local API
Markdown into the chosen output directory without requiring this repository's
mdBook layout.
