# ADTs and Pattern Matching

Algebraic data types are Flex's workhorse: a closed set of variants, each
optionally carrying data, consumed with `match`. Everything on this page runs
identically on the interpreter and the native backend.

## Declaring types

```flex
type Color = | Red | Green | Blue            // a payloadless enum
type Shape =
  | Circle(I64)                              // one payload field
  | Rect(I64, I64)                           // multi-field payloads
  | Dot
type Entry = | KV(String, I64) | Empty       // any types can ride along
```

Types may be **recursive** — directly, mutually, or through generics:

```flex
type N = | Zero | Succ(N)

type Even = | Z | SuccE(Odd)
type Odd  = | SuccO(Even)

type Chain<T> = | End(T) | Link(Chain<T>)
```

Natively, a payload that doesn't fit a machine word by value (a `String`, a
record, another non-enum ADT, or any multi-field payload) is allocated on the
heap and freed at process exit; region-based reclamation is the roadmap.

## match

`match` takes the first arm whose pattern fits. Patterns nest, and integer and
bool literals are patterns too:

```flex
fn classify(n: N) -> I64 = {
  match n {
    Zero => 0
    Succ(Zero) => 1                          // nested constructor pattern
    Succ(Succ(m)) => 2
    Succ(m) => 99                            // first match wins
  }
}

fn lookup(o: Option<I64>) -> I64 = {
  match o {
    Some(0) => -1                            // literal pattern
    Some(n) => n
    None => 0
  }
}
```

Arm bodies can be blocks:

```flex
match shape {
  Rect(w, h) => {
    let area = w * h
    area * 2
  }
  _ => 0
}
```

## Exhaustiveness

Every `match` must provably cover its type. An arm counts toward coverage only
when it takes the **whole** variant — every constructor argument a binder or
`_`. `Succ(Zero)` does not cover `Succ`, so a match using nested or literal
patterns needs a catch-all (`_ => …`) or an all-binders arm:

```text
error[MATCH001]: non-exhaustive match; missing Succ
help: arms with literal or nested sub-patterns don't count toward coverage;
      add a catch-all arm (`_ => ...`) or an all-binders arm
```

Refutable arms after the variant is fully covered are reported unreachable
(MATCH002).

## Constructing values

Constructors are called like functions; generic arguments are inferred from
the expected type or from the arguments — `Link(Link(End(40)))` needs no
annotation when the context (a parameter type, a `let` with usage, a return
type) pins `Chain<I64>`.

## Equality on ADTs

`==` and `assert_eq` work on ADTs whose payloads are machine-word scalars
(`I64`, `Bool`, payloadless enums). Variants carrying strings, records, other
ADTs, or multi-field payloads do not compare structurally with `==` — match on
them instead, or write an `impl Eq`:

```text
error[TYPE019]: assert_eq is not supported for E
```

## Statement-position if and match

When nobody consumes its value, an `if`/`else` (or `match`) does not require
its branches to agree on a type:

```flex
fn main() -> I64 uses { Log } = {
  if verbose { println("starting") } else { 0 }   // fine as a statement
  let x = if verbose { 1 } else { "no" }          // TYPE008: value position
  7
}
```

## Not yet

- String literal patterns (`M("a") => …`) — bind and compare with `.eq()`.
- Range and record patterns.
- Full usefulness analysis (bool-literal splits like `Pair(true, _)` /
  `Pair(false, _)` still want a catch-all).
