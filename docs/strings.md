# Printing and Strings

The five things every program needs on day one, in one place.

## Printing

```flex
import Std.IO

fn main() -> I64 uses { Log } = {
  print("no newline, ")
  println("then one")
  println("answer=" ++ to_str(42))
  0
}
```

- `println(s)` / `print(s)` come from `Std.IO` (or use `Log.info(s)` directly —
  it is the same thing with a newline).
- **`++` concatenates strings.** `+` is integer addition only — the compiler
  will point you here.
- **`to_str(n)`** renders an `I64` as a `String`. There are no format strings
  yet; build output with `++`.
- A bare `flx run` reports the program's exit code on stderr; anything you
  `print` goes to stdout on both backends, identically.

## Reading

```flex
import Std.IO

fn main() -> I64 uses { Fs, Log } = {
  let line = read_line()   // one line, newline stripped; "" at end of input
  println("you said: " ++ line)
  0
}
```

## Comparing and testing

Import `Std.Str` and strings gain real equality through the trait system:

```flex
import Std.Str

test "strings" {
  assert("a".eq("a"))            // trait dispatch to strcmp
  assert_eq(cmp("a", "b"), 0 - 1)
  assert_eq("flex", "flex")      // assert_eq works on strings with Std.Str
}
```

A failing string assertion prints both values:

```text
  assert_eq failed: actual "flexx", expected "flex"
```

`length(s)` is the **byte** length (Flex strings are UTF-8 bytes);
`is_empty`, `ne`, and `cmp` (-1/0/1) live in `Std.Str` too.

## Timing

```flex
import Std.Time

fn main() -> I64 uses { Time, Log } = {
  let t0 = monotonic_ms()
  // ... work ...
  Log.info("took " ++ to_str(monotonic_ms() - t0) ++ "ms")
  0
}
```

## Beyond this page

Much of what this page once listed as missing has shipped: floats and
hex/bitwise live in [Numbers, Bits, and Function Values](numerics.md), and
byte-level access — `byte_at`, `substr`, `char_at`, `split`, `parse_int` —
is in [`Std.Str`](api/Std.Str.md) (generated from the compiler, examples
executed in CI).

Still missing, and the compiler says so at the exact place you try them:
format strings, `parse_float` (declare `extern fn atof(s: String) -> F64`),
byte-to-String construction (`chr`), and string literal patterns.
