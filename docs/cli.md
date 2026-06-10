# Command-Line Interface

The compiler ships a single `flx` executable. The command surface mirrors the
pipeline described in the [specification](MVP.md#18-cli-commands); most
subcommands are scaffolded today and implemented incrementally.

| Command | Purpose |
|---------|---------|
| `flx parse <file>` | Parse and print the AST |
| `flx check <file>` | Resolve, typecheck, effect-check, region-check |
| `flx test [path]` | Discover, compile, and run tests |
| `flx run <file>` | Compile and run |
| `flx build <file> -o <bin>` | Build a native executable |
| `flx emit-hir <file>` | Emit typed HIR |
| `flx emit-mir <file>` | Emit MIR |
| `flx emit-mlir <file>` | Emit MLIR text |
| `flx expand <file>` | Show desugar/macro expansion |
| `flx explain-effects <file>` | Explain effects |
| `flx explain-cost <file>` | Explain allocation/cost |
| `flx highlight <file>` | Syntax-highlight `.flx` (implemented) |

## `flx highlight`

```sh
flx highlight examples/add.flx                 # auto-detects truecolor / 256
flx highlight examples/add.flx --style github-dark
flx highlight examples/add.flx --format html > add.html
```

`--format` accepts `auto`, `ansi`, `ansi256`, `truecolor`, and `html`;
`--style` accepts any Pygments style name. See
[Syntax Highlighting](highlighting.md) for details.
