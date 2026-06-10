# Flex

**Flex** is a native functional programming language for explicit systems
programming:

> Write like F#.
> Macro like Elixir.
> Allocate like Zig.
> Ship like C.

Flex aims to feel like a high-level functional language at the API boundary
while preserving a systems-language cost model underneath. The core principle:

> Function signatures should tell the truth about what can fail, what can
> allocate, and what effects can happen.

```flex
fn transcode(input: Path, output: Path)
  -> Result<Stats, TranscodeError>
  uses { Fs, Alloc, Log, Time } =
{
  region scratch {
    let bytes = Fs.read(input, scratch)?
    let frames = Decode.parse(bytes, scratch)?
    let stats = Encode.write(frames, output)?
    Log.info("transcoded")
    Ok(stats)
  }
}
```

That signature says the function returns either `Stats` or `TranscodeError`,
may touch the filesystem, may allocate, may log, and may read time — with
temporary memory confined to `scratch`. No hidden exceptions, no hidden global
runtime, no hidden GC.

This book covers:

- the **[Specification](MVP.md)** — the full language design and MVP plan;
- **[Traits and Generics](traits.md)** — traits, impls, `derive`, and bounded
  generic functions, all statically dispatched and monomorphized;
- the **[Command-Line Interface](cli.md)** — the `flx` tool;
- **[Syntax Highlighting](highlighting.md)** — the Flex Pygments lexer that
  colors every example in this book;
- runnable **[Examples](examples.md)**.

> Flex is an early-stage prototype. The compiler is written in Python with
> [xDSL](https://github.com/xdslproject/xdsl) and lowers through MLIR/LLVM 22 to
> native code.
