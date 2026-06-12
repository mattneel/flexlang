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
- `eprintln(s)` writes a line to stderr for CLI diagnostics.
- **`++` concatenates strings.** `+` is integer addition only — the compiler
  will point you here.
- **`to_str(n)`** renders an `I64` as a `String`. There are no format strings
  yet; build output with `++`.
- A bare `flx run` reports the program's exit code on stderr; anything you
  `print` goes to stdout on both backends, identically.

## Reading

`read_line()` returns `Option<String>`: `Some(line)` with the newline
stripped — `Some("")` for a blank line — and `None` at end of input.

```flex
import Std.IO

fn main() -> I64 uses { Fs, Log } = {
  match read_line() {
    Some(line) => { println("you said: " ++ line) }
    None => { println("(no input)") }
  }
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

## Bytes

Strings are byte strings. `byte_at(s, i)` reads byte `i`; `from_byte(b)` and
`from_bytes(bs)` build strings back from bytes (1..255 — byte 0 is the NUL
terminator and panics). String literals accept `\xNN` byte escapes:

```flex
import Std.Str

test "bytes round-trip" {
  assert_eq(byte_at("\xff", 0), 255)
  let bs = to_bytes("A\xff")
  assert_eq(bs[0], 65)
  assert_eq(bs[1], 255)
  assert_eq(from_bytes([195, 169]), "é")
}
```

Strings cannot carry byte 0. For binary data, keep bytes in `List<I64>`;
convert to/from `String` only when the byte list is known to be non-NUL
text-like data.

## Beyond this page

Much of what this page once listed as missing has shipped: floats and
hex/bitwise live in [Numbers, Bits, and Function Values](numerics.md), and
byte-level access — `byte_at`, `substr`, `char_at`, `split`, `parse_int`,
`from_byte`, `from_bytes`, `to_bytes`, and `trim` — is in
[`Std.Str`](api/Std.Str.md) (generated from the compiler, examples executed in
CI).

Number formatting and parsing live there too: `parse_float` (strict,
correctly rounded, `Option<F64>`), `to_str_fixed(x, decimals)`, and
`repeat`/`pad_left`/`pad_right` for columns. For bit-oriented output,
`to_hex(n)` and `to_unsigned(n)` format the two's-complement bits of an `I64`
as unsigned 64-bit text.

Still missing, and the compiler says so at the exact place you try them:
format strings and string literal patterns.
