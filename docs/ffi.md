# FFI — Calling C

Flex talks to C through `extern fn` declarations. An extern is a **trust
declaration**: Flex cannot verify what a C function does, so the author asserts
its signature *and its effects*, and the effect system holds every caller to
that assertion — the same "signatures tell the truth" rule as everywhere else,
extended to the one place the compiler has to take your word for it.

```flex
extern fn llabs(n: I64) -> I64
extern fn strlen(s: String) -> I64
extern fn getenv(name: String) -> String
extern fn puts(s: String) -> I64 uses { Process }

fn main() -> I64 uses { Process } = {
  let r = puts("hello from libc")
  llabs(0 - 40) + strlen("ab")
}
```

Calling `puts` from a function that doesn't declare `uses { Process }` is
EFFECT001, exactly as if it were a Flex function. An extern with no `uses`
clause asserts the C function is pure — that assertion is on you.

## What crosses the ABI

The surface is deliberately small; anything else is rejected (FFI002) rather
than mis-marshalled:

| Flex | C |
|---|---|
| `I64` parameter / return | `long long` |
| `String` parameter | `const char *` (NUL-terminated; every Flex string is) |
| `String` return | `char *` — wrapped back into a Flex string; **NULL becomes `""`** |
| `Unit` return | `void` |

No records, ADTs, `Bool`, or generics across the boundary, and **no variadic
functions** — declaring `printf` with a fixed arity is undefined behavior on
most ABIs; use `puts`/`fputs`.

## Both backends, one semantics

- **Native**: the extern is declared by its unmangled symbol and compiled to a
  direct C call; `clang` links it (libc works out of the box — a missing symbol
  is a link-time error).
- **Interpreter**: the same call is dispatched through the symbols already
  loaded in the process (via `ctypes`), so `uvx --from flexlang flx run` can
  call libc with **no toolchain installed**. A missing symbol is a clean
  runtime error.

The two paths are differential-tested to produce identical output, including
the interleaving of C and Flex output.

`pub extern fn` exports an extern from a module like any other function;
private externs stay module-local (VIS001). Externs are callable from `build.flx`
targets under the same effect rules. See
[`examples/ffi.flx`](https://github.com/mattneel/flexlang/blob/main/examples/ffi.flx).

## Not yet

Exporting Flex functions *to* C, linking extra libraries (`-l...`) from the
manifest, structs by value, and callbacks are future work — this milestone is
calling C from Flex, soundly typed and effect-checked.
