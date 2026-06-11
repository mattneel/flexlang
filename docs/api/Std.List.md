# Std.List

*Generated from the `doc` declarations in `List.flx` by `flx docs build`. Examples are executed by `flx docs check`.*

Helpers over the built-in growable `List<T>`.

Lists themselves are built into the language: `[1, 2, 3]` literals, `xs[i]`
indexing (panics out of bounds), `List.push(xs, v)`, `List.len(xs)`,
`List.set(xs, i, v)`, and `for x in xs { ... }` need no import. Lists have
REFERENCE semantics: `let ys = xs` aliases, it does not copy. This module
adds the derived helpers.

*since 0.0.1 · status: implemented*

## range

```flx
fn range(a: I64, b: I64) -> List<I64>
```

The half-open range [a, b) as a list.

**Example: builds half-open ranges** — ✓ checked by `flx docs check`:

```flx
let r = range(1, 5)
assert_eq(List.len(r), 4)
assert_eq(r[0], 1)
assert_eq(r[3], 4)
assert_eq(List.len(range(3, 3)), 0)
```

## map

```flx
fn map<T, U>(xs: List<T>, f: (T) -> U) -> List<U>
```

A new list with a pure function applied to every element.

Function arguments must be PURE, monomorphic, top-level functions — function
types carry no effects yet. Any pure function fits, like `abs` here.

**Example: maps abs over elements** — ✓ checked by `flx docs check`:

```flx
let magnitudes = map([-3, 4, -5], abs)
assert_eq(magnitudes[0], 3)
assert_eq(magnitudes[2], 5)
```

See also: `filter`, `fold`, `Std.Math`

## filter

```flx
fn filter<T>(xs: List<T>, keep: (T) -> Bool) -> List<T>
```

The elements a predicate keeps, in order.

**Example: keeps the empty strings** — ✓ checked by `flx docs check`:

```flx
let empties = filter(["", "x", ""], is_empty)
assert_eq(List.len(empties), 2)
```

See also: `Std.Str`

## fold

```flx
fn fold<T, A>(xs: List<T>, init: A, f: (A, T) -> A) -> A
```

Reduce a list left-to-right from an initial accumulator.

**Example: folds min to find the smallest** — ✓ checked by `flx docs check`:

```flx
assert_eq(fold([5, 3, 8, 1], 9999, min), 1)
```

**Example: folds max to find the largest** — ✓ checked by `flx docs check`:

```flx
assert_eq(fold(range(1, 11), 0, max), 10)
```
