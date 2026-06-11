# TYPE019

Equality is not defined for this type.

`==` and `assert_eq` work on scalars, records of comparables, and ADTs
whose payloads fit a machine word. Strings compare via `import Std.Str`
(the Eq trait); lists and String-carrying ADTs need a `match` or an
explicit comparison — pointer identity would be a lie.

**Example: lists do not compare with ==** — expected to fail with `TYPE019` (proven by `flx docs check`):

```flx
fn main() -> I64 = { if [1] == [1] { 0 } else { 1 } }
```
