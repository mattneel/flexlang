# NAME003

A function reference that cannot be a value.

PURE top-level functions are values (pass them to map/filter/fold).
Effectful functions, externs, generics, and builtins stay call-only: a
value of an effectful function would smuggle its effects past the checker.

**Example: effectful functions are call-only** — expected to fail with `NAME003` (proven by `flx docs check`):

```flx
fn shouty() -> I64 uses { Log } = { Log.info("x")
  41 }
fn main() -> I64 uses { Log } = { let g = shouty
  g() }
```
