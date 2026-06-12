# Numbers, Bits, and Function Values

Everything on this page runs identically on the interpreter and the native
backend.

## F64

Float literals take a decimal point or an exponent: `12.5`, `0.25`, `1e9`,
`6.02e23`. `F64` is IEEE-754 double precision:

```flex
let x = 0.1 + 0.2          // 0.30000000000000004 — floats are floats
let inf = 1.0 / 0.0        // division does not trap: inf, -inf, or nan
let nan = 0.0 / 0.0
nan == nan                 // false (ordered comparison)
let r = 7.5 % 2.0          // C fmod semantics: 1.5
```

There is **no implicit numeric conversion**. Convert explicitly:

```flex
to_f64(7) / 2.0            // 3.5
to_i64(3.99)               // 3 — truncates toward zero
to_i64(0.0 / 0.0)          // runtime panic: there is no honest answer
to_str(1.5)                // "1.5" — shortest text that parses back exactly
```

`to_str` prints the shortest decimal string that round-trips to the same
float, computed by the same algorithm on both backends.

### libm through the FFI

`F64` crosses the C ABI as `double`, so `import Std.Math` now provides
`sqrt`, `sin`, `cos`, `floor`, `ceil`, `fabs`, `log`, and `exp` straight from
libm — and any other double-taking C function is one declaration away:

```flex
extern fn pow(base: F64, exp: F64) -> F64   // declare what you need
```

(`Std.Math.pow` remains the integer power; the float one is yours to name.)

Float literal patterns are rejected — float equality is rarely what a match
means. Compare explicitly in a guard-style `if`.

## Hex, binary, and bitwise operations

```flex
let mask = 0xFF & 0x0F     // 15
let bits = 0b1010 | 0b0101 // 15
let flip = 0xF0 ^ 0xFF     // 15
let big  = 1 << 40
let down = -16 >> 2        // -4 — >> is arithmetic (sign-preserving)
let all  = 0xFFFFFFFFFFFFFFFF  // bit patterns >= 2^63 wrap: this is -1
```

Bitwise operators bind tighter than comparisons (Rust-style), so
`x & 1 == 1` means `(x & 1) == 1`. Shift counts are masked to `0..63` —
`1 << 100` is `1 << 36`, never undefined behavior.

## Function values

A **pure** top-level function can be passed as a value. Function types are
written `(T1, T2) -> R`:

```flex
import Std.List

fn double(x: I64) -> I64 = { x * 2 }
fn is_even(x: I64) -> Bool = { x % 2 == 0 }
fn add(a: I64, b: I64) -> I64 = { a + b }

let doubled = map(range(1, 5), double)      // [2, 4, 6, 8]
let evens = filter(range(1, 10), is_even)   // [2, 4, 6, 8]
let total = fold(range(1, 11), 0, add)      // 55
```

`Std.List` provides `map`, `filter`, and `fold`; writing your own
higher-order functions is plain Flex:

```flex
fn apply2(f: (I64) -> I64, v: I64) -> I64 = { f(f(v)) }
```

The rules that keep the effect system sound:

- Only **pure** functions become values — referencing one that `uses { … }`
  is an error (a value would smuggle its effects past the checker).
  Effect-carrying function types are the roadmap.
- Externs stay call-only (their C ABI marshalling happens at direct calls).
- Generic functions can't be passed (pass a monomorphic one).
- Function values flow through parameters, arguments, and `let` bindings;
  storing them (records, lists, ADT payloads, `mut`) is not supported yet.
- No closures yet — a function value is a name, not a capture.

## Float ↔ text

Both directions live in [`Std.Str`](api/Std.Str.md): `parse_float(s)` returns
`Option<F64>` (strict grammar, correctly rounded via libc strtod on both
backends — `parse_float(to_str(x))` round-trips exactly), and
`to_str_fixed(x, decimals)` is C's `%.*f`. `pad_left`/`pad_right` align
columns.

## Not yet

- `~` bitwise not — write `x ^ -1`.
- Closures, lambdas, and functions returning functions.
