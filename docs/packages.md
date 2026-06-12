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

Dependencies are **path dependencies** today. Each names a directory that
becomes an additional import-resolution root, so `import Mathlib` in the app
resolves to `Mathlib.flx` in the dependency, or to a `module Mathlib { ... }`
block in another `.flx` file when the conventional path does not exist — with
`pub`/private visibility enforced across the package boundary, transitive
dependencies included, and ambiguous imports (two roots providing the same
module) rejected (MOD004). See
[`examples/package-demo/`](https://github.com/mattneel/flexlang/tree/main/examples/package-demo)
for a complete two-package project.

Lock and vendor commands make path dependencies reproducible:

```sh
flx deps lock      # write flex.lock with dependency content hashes
flx deps verify    # verify current dependency trees against flex.lock
flx deps vendor    # copy dependencies into vendor/ and write vendor paths to flex.lock
```

When `flex.lock` exists, Flex verifies dependency hashes during import-root
resolution. If a lock entry has a `vendor` path and that directory exists, Flex
uses the vendored copy; this lets reviewed sources build offline without
rewriting `package.flx`.

### Package trust model

Dependencies are trusted source code, not sandboxed plugins. Running `flx run`,
`flx test`, `flx build`, or `flx docs check` can execute dependency code through
ordinary functions, tests, docs, build targets, and declared FFI. The effect
system makes these capabilities visible and composable; it is not a hostile-code
containment boundary.

The conservative workflow is still manual: review a dependency and keep it as a
path dependency or vendored source in the tree. A future registry should add
reproducibility and identity around that decision, not implicit trust. The target
shape is Hex-like:

- immutable published versions, with retirement/advisory metadata instead of
  silent mutation;
- signed registry metadata, including explicit keys for private or self-hosted
  repositories;
- lockfiles that pin exact package versions and content hashes;
- no arbitrary install scripts;
- a first-class vendor workflow for offline and high-review builds.

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

target test uses { Fs, Process } {
  flx.test("main.flx")?
  exec(["uv", "run", "pytest", "-q"])?
}

target ci uses { Fs, Process } {
  check()?
  test()?
}
```

The rules are the language's own rules:

- `exec(["cmd", "arg"])` runs a process without a shell and requires `Process`.
  `sh("...")` is still available for shell syntax and requires both `Process`
  and `Unsafe`.
  `flx.check/test/run/expand/build` drive the compiler in-process over a file
  glob and require `Fs`. A target that uses these without declaring the effect
  fails type-checking (EFFECT001).
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
  uses: Fs, Process
target ci  (default)
  uses: Fs, Process
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
