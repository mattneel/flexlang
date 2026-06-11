# TYPE008

Branches disagree on a type where the value is used.

An `if`/`match` used AS A VALUE needs every branch to produce the same
type. In statement position (nobody consumes the value) the branches are
free to differ.

**Example: value-position branches must agree** — expected to fail with `TYPE008` (proven by `flx docs check`):

```flx
fn main() -> I64 = { let x = if true { 1 } else { "s" }
  0 }
```
