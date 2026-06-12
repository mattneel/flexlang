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
| `flx expand <file>` | Show comptime/macro/derive expansion | ✅ |
| `flx highlight <file>` | Syntax-highlight `.flx` | ✅ |
| `flx build <file> -o <bin>` | Build a native executable | working |
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
