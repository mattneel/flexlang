# Std.Time

*Generated from the `doc` declarations in `Time.flx` by `flx docs build`. Examples are executed by `flx docs check`.*

Clocks.

*since 0.0.1 · status: implemented*

## unix_time

```flx
fn unix_time() -> I64 uses { Time }
```

Seconds since the Unix epoch (libc time).

**Example: the epoch is behind us** — ✓ checked by `flx docs check`:

```flx
assert(unix_time() > 1500000000)
```

## monotonic_ms

```flx
fn monotonic_ms() -> I64 uses { Time }
```

A monotonic clock in milliseconds — for measuring durations.

Monotone non-decreasing within a run; the absolute value is meaningless.

**Example: time does not run backwards** — ✓ checked by `flx docs check`:

```flx
let t0 = monotonic_ms()
let t1 = monotonic_ms()
assert(t1 >= t0)
```
