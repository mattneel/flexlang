# Examples

These are the real files under [`examples/`](https://github.com/mattneel/flexlang/tree/main/examples),
included verbatim and highlighted by the Flex lexer.

## Hello integer

```flex
{{#include ../examples/hello.flx}}
```

## Add, with a first-class test

This is the MVP success-criteria program — it should parse, typecheck, emit
MLIR, lower to LLVM, and run:

```flex
{{#include ../examples/add.flx}}
```

Run and test it:

```sh
flx run  examples/add.flx
flx test examples/add.flx
```

## Traits, impls, and generics

A trait, two impls, a bounded generic, an unconstrained generic, and a derived
`Show` — all dispatched statically and monomorphized. See
[Traits and Generics](traits.md) for the guide.

```flex
{{#include ../examples/traits.flx}}
```
