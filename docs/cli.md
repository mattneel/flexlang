# Command-Line Interface

The compiler ships a single `flx` executable. The command surface mirrors the
pipeline described in the [specification](MVP.md#18-cli-commands); most
subcommands are scaffolded today and implemented incrementally.

| Command | Purpose | Status |
|---------|---------|--------|
| `flx parse <file>` | Parse and print the AST | ✅ |
| `flx check <file>` | Resolve and typecheck | ✅ |
| `flx emit-mlir <file>` | Emit MLIR text | ✅ |
| `flx run <file>` | Compile to native and run | ✅ |
| `flx test [path]` | Discover, compile, and run tests | ✅ |
| `flx highlight <file>` | Syntax-highlight `.flx` | ✅ |
| `flx build <file> -o <bin>` | Build a native executable | stub |
| `flx emit-hir <file>` | Emit typed HIR | stub |
| `flx emit-mir <file>` | Emit MIR | stub |
| `flx expand <file>` | Show desugar/macro expansion | stub |
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
