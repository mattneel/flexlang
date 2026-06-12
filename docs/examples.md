# Examples

These are the real files under [`examples/`](https://github.com/mattneel/flexlang/tree/main/examples),
included verbatim and highlighted by the Flex lexer.

## Hello integer

```flex
module Main

fn main() -> I64 =
{
  42
}
```

## Add, with a first-class test

This is the MVP success-criteria program — it should parse, typecheck, emit
MLIR, lower to LLVM, and run:

```flex
module Main

fn add(a: I64, b: I64) -> I64 =
{
  a + b
}

fn main() -> I64 =
{
  add(20, 22)
}

test "add works" {
  assert_eq(add(20, 22), 42)
}
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
module Main

trait Show =
{
  fn show(self: Self) -> String
}

type Point = { x: I64, y: I64 }

impl Show for Point =
{
  fn show(self: Point) -> String = { "(" ++ to_str(self.x) ++ ", " ++ to_str(self.y) ++ ")" }
}

impl Show for I64 =
{
  fn show(self: I64) -> String = { to_str(self) }
}

fn announce<T: Show>(label: String, value: T) -> String uses { Log } =
{
  let line = label ++ ": " ++ value.show()
  Log.info(line)
  line
}

fn first<A, B>(a: A, b: B) -> A = { a }

derive(Show) type Color =
  | Red
  | Green
  | Blue

fn main() -> I64 uses { Log } =
{
  let p = { x = 3, y = 4 }
  announce("point", p)
  announce("count", 7)
  let kept = first(p, Blue)
  kept.x + kept.y
}
```
