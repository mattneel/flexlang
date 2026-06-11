# Packages and Builds

Flex has no TOML, no YAML, and no build DSL. A package describes itself in
**Flex**, and builds **are Flex programs** — with the same type checker and the
same effect system as everything else. The principle is the one the whole
language is built on: *powerful, but declared*.

Two files, two roles:

| File | Role | Effects |
|---|---|---|
| `package.flx` | **data** — name, version, entry, dependencies | none (provably pure) |
| `build.flx` | **logic** — effect-checked, runnable targets | declared per target |

## `package.flx` — the manifest

A manifest is a pure function returning a typed `Manifest` record:

```flex
module Package

fn manifest() -> Manifest = {
  {
    name = "demo-app",
    version = "0.1.0",
    entry = "main.flx",
    dependencies = [ { name = "Mathlib", path = "../mathlib" } ]
  }
}
```

`flx` reads it by **evaluating** `manifest()` on the interpreter with **zero
effect capabilities granted**. `manifest()` may not declare `uses { ... }`
(PKG003), and the checker rejects any effectful call from an effect-free
function — so reading a manifest is provably pure data extraction, enforced by
the type system rather than by a file format. It is also *typed*: `Manifest`
and `Dependency` are real record types (available only inside package files),
so a malformed manifest is a type error, not a runtime surprise.

With a `package.flx` in the directory, the path argument becomes optional:

```sh
flx run          # runs the manifest's entry
flx test         # tests it
flx check        # checks it (and `flx check package.flx` validates the manifest)
```

### Dependencies

Dependencies are **path dependencies** (registry/versioned deps come later).
Each names a directory that becomes an additional import-resolution root, so
`import Mathlib` in the app resolves to `Mathlib.flx` in the dependency — with
`pub`/private visibility enforced across the package boundary, transitive
dependencies included, and ambiguous imports (two roots providing the same
module) rejected (MOD004). See
[`examples/package-demo/`](https://github.com/mattneel/flexlang/tree/main/examples/package-demo)
for a complete two-package project.

## `build.flx` — the effect-checked build graph

Build logic is `target` declarations. A target's signature **declares its
effects**, and the checker holds it to that — a build that shells out, reads
files, or touches the network says so in its type:

```flex
module Build

target default = ci

target check uses { Fs } {
  flx.check("main.flx")?
}

target test uses { Fs } {
  flx.test("main.flx")?
}

target ci uses { Fs } {
  check()?
  test()?
}
```

The rules are the language's own rules:

- `sh("...")` requires `Process`; `flx.check/test/run/expand/build` (which drive
  the compiler in-process over a file glob) require `Fs`. A target that uses
  them without declaring the effect fails type-checking (EFFECT001).
- **Calling another target demands that target's effects** — effects propagate
  up the build graph like any call. `ci` cannot launder `test`'s capabilities.
- Each step returns `Result<Unit, String>` and `?` propagates the first
  failure out of the target; a failed step fails the build with its reason.
- Targets run **at most once** per invocation (the graph is memoized), in
  dependency order.

Run it:

```sh
flx build              # the default target
flx build check        # a named target
flx build --explain    # list targets and their declared effects
```

```console
$ flx build --explain
target check
  uses: Fs
target test
  uses: Fs
target ci  (default)
  uses: Fs
```

The Flex repository **builds itself** this way — see
[`build.flx`](https://github.com/mattneel/flexlang/blob/main/build.flx) at the
repo root: `flx build` runs ruff, mypy, pytest, and the Flex examples through
an effect-checked target graph.

## How it runs (and where it's going)

Targets and manifests execute on the same tree-walking interpreter that powers
`flx run`/`flx test` — no LLVM toolchain needed for any of this. The surface
language is real Flex, so the long-term path (compiling `build.flx` to native
and self-hosting the runner) changes nothing for users. The thesis, stated
once: **Flex builds are just Flex programs with explicit effects.**
