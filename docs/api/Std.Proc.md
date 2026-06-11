# Std.Proc

*Generated from the `doc` declarations in `Proc.flx` by `flx docs build`. Examples are executed by `flx docs check`.*

Process introspection.

*since 0.0.1 · status: implemented*

## pid

```flx
fn pid() -> I64 uses { Process }
```

This process's id.

**Example: pids are positive** — ✓ checked by `flx docs check`:

```flx
assert(pid() > 0)
```
