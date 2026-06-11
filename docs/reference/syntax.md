# Syntax You Can Rely On

The corners of Flex syntax that the study showed people guess about, each
one proven by a checked example. (Examples on this page run under
`flx docs check` on both backends.)

## `return` works everywhere

Early `return` is legal anywhere in a function body — inside loops, `if`
branches, and `match` arms. It is the idiomatic replacement for `break`.

**Example: early return escapes a loop** — ✓ checked by `flx docs check`:

```flx
assert_eq(first_multiple_of_7_above(20), 21)
```

## `else if` chains work

**Example: else-if chains** — ✓ checked by `flx docs check`:

```flx
assert_eq(grade(95), 1)
assert_eq(grade(85), 2)
assert_eq(grade(20), 3)
```

## Operator precedence

From loosest to tightest: `|>` · `||` · `&&` · `==` `!=` · `<` `<=` `>` `>=`
· `|` · `^` · `&` · `<<` `>>` · `+` `-` `++` · `*` `/` `%` · unary `-` `!` ·
calls, `.member`, `[index]`, `?`. Bitwise binds tighter than comparison
(Rust-style), so `x & 1 == 1` means `(x & 1) == 1`.

**Example: bitwise binds tighter than comparison** — ✓ checked by `flx docs check`:

```flx
assert(0xFF & 0x0F == 0x0F)
assert_eq(1 << 2 + 3, 32)
```

## String escapes

The escape set is `\n` `\t` `\r` `\"` `\\` — anything else is a compile
error, never silent mangling. Prose blocks use `\"\"\"` and are raw.

## Literals

Integers: `42`, `0xFF`, `0b1010`, `1_000_000`. Hex/binary are BIT PATTERNS:
`0xFFFFFFFFFFFFFFFF` is -1. Floats need a decimal point or exponent:
`12.5`, `1e9`. Unit is `()`. Lists are `[1, 2, 3]`.

**Example: literal forms** — ✓ checked by `flx docs check`:

```flx
assert_eq(0xFF, 255)
assert_eq(0b1010, 10)
assert_eq(1_000_000, 1000000)
assert_eq(0xFFFFFFFFFFFFFFFF, -1)
assert_eq(to_i64(12.5 * 2.0), 25)
```

## Blocks vs records

A lone `{ x = e }` is a BLOCK containing an assignment. Record literals are
recognized by a comma (`{ x = 1, y = 2 }`), a `with` update, or `{}`. To
force a one-field record literal, parenthesize it: `({ x = e })`.

## Not in the language (and what to use)

- `break` / `continue` — use a flag in the `while` condition or early `return`.
- Tuples — use a record or a multi-field ADT variant.
- Lambdas/closures — define a top-level `fn`; pure functions are values.
- `xs[i] = v` — use `List.set(xs, i, v)`.
- String literal patterns — bind, then compare with `.eq()`.

Each of these errors with a message that says so.

See also: `Std.List`, `Std.Str`
