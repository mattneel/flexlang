# Standard Library

The standard library is **written in Flex** and ships inside the compiler
package — `import Std.Math` works everywhere `flx` runs, including a bare
`uvx --from flexlang flx run` with no toolchain. There is no privileged
mechanism: Std modules are ordinary `.flx` files using `pub`, traits, and
`extern fn`, compiled into your program like any other import. The bundled
tree is the **lowest-precedence import root**, so your own modules and
dependencies can shadow `Std.*` deliberately, never ambiguously.

```flex
module Main

import Std.Math
import Std.Str
import Std.Env

fn main() -> I64 uses { Process } = {
  let greeting = get_or("FLX_GREETING", "hello")
  clamp(pow(2, 10), 0, 30) + length(greeting) + abs(0 - 7)
}
```

Effects are first-class here too: `Std.Env`/`Std.Proc` functions declare
`uses { Process }` and `Std.Time` declares `uses { Time }` — calling them from
an effect-free function is EFFECT001, exactly like any other call.

## Modules

| Module | What | Effects |
|---|---|---|
| `Std.Math` | `abs`, `min`, `max`, `clamp`, `sign`, `pow` (64-bit wrapping integer math) | pure |
| `Std.Str` | `length` (bytes), `is_empty`, `eq`, `ne`, `cmp`, plus `impl Eq for String` and `impl Show for String` | pure |
| `Std.Env` | `get_or(name, default)`, `has(name)` | `Process` |
| `Std.Time` | `unix_time()` | `Time` |
| `Std.Proc` | `pid()` | `Process` |

The headline: **importing `Std.Str` gives every `String` real equality** —
`"a".eq("b")` dispatches through the trait system to `strcmp`, on both
backends. (`length` is the UTF-8 *byte* length; `getenv`-backed `Env` cannot
distinguish unset from empty, as in C.)

## How it's built

`Std.Math` is pure Flex. The rest wraps libc through `extern fn` — the same
FFI any user can write:

```flex
module Std.Str

extern fn strlen(s: String) -> I64
extern fn strcmp(a: String, b: String) -> I32

pub fn eq(a: String, b: String) -> Bool = { strcmp(a, b) == 0 }
```

Those externs are **private** to their Std module (reaching for `strcmp`
through `Std.Str` is VIS001). If your code needs the same symbol, declare it
yourself: identical extern redeclarations merge, C-style — only a
*conflicting* signature is an error (FFI004).

Future modules (`Std.Fs`, collections) wait on the allocation story; the
mechanism — Flex code over declared-effect externs — is now in place.
