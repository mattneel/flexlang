# Flex Language SPEC.md

## 0. Project Summary

**Flex** is a native functional programming language for explicit systems programming.

Flex source files use the `.flx` extension.

The command-line tool is named `flx`.

Flex combines:

* F#/Elm-style functional syntax
* Elixir-style pipe ergonomics
* hygienic comptime macros
* first-class `test "name" { ... }` blocks
* explicit `Result`-based failure
* explicit side effects
* immutable-by-default values
* opt-in local mutation via `mut`
* region-based scoped allocation
* predictable performance and inspectable cost
* MLIR/LLVM-native compilation

Canonical tool shape:

```sh
flx check src/main.flx
flx test
flx run src/main.flx
flx build src/main.flx
flx emit-mlir src/main.flx
flx explain-cost src/main.flx
flx explain-effects src/main.flx
flx expand src/main.flx
```

The prototype compiler should be written in **Python** using **xDSL/MLIR** for IR construction and lowering experiments.

The goal is not to immediately build a production compiler. The goal is to validate the language shape quickly:

```text
.flx source
  -> parser
  -> AST
  -> name resolution
  -> type checking
  -> macro/desugar expansion
  -> HIR
  -> MIR
  -> MLIR/xDSL
  -> MLIR tooling
  -> LLVM/native binary
```

---

## 1. Design Thesis

Flex exists because there is a missing language shape:

> Write like F#.
> Macro like Elixir.
> Allocate like Zig.
> Ship like C.

Flex should feel like a high-level functional language at the API boundary while preserving a systems-language cost model underneath.

The core principle:

> Function signatures should tell the truth about what can fail, what can allocate, and what effects can happen.

Example:

```flx
fn transcode(input: Path, output: Path)
  -> Result<Stats, TranscodeError>
  uses { Fs, Alloc, Log, Time } =
{
  region scratch {
    let bytes = Fs.read(input, scratch)?
    let frames = Decode.parse(bytes, scratch)?
    let stats = Encode.write(frames, output)?
    Log.info("transcoded")
    Ok(stats)
  }
}
```

This signature says:

* the function returns either `Stats` or `TranscodeError`
* it may touch the filesystem
* it may allocate
* it may log
* it may read time
* temporary memory lives inside `scratch`

No hidden exceptions.
No hidden global runtime.
No hidden GC requirement.
No hidden reflection magic.

---

## 2. Implementation Strategy

### 2.1 Prototype Stack

The first implementation should prioritize iteration speed.

Use:

* **Python 3.12+**
* **xDSL** for MLIR-style IR construction/prototyping
* **Lark** or another simple Python parser generator for the first parser
* **pytest** for compiler tests
* **ruff** for formatting/linting
* **mypy** or **pyright** once the internal compiler model stabilizes
* external MLIR/LLVM tools where available:

  * `mlir-opt`
  * `mlir-translate`
  * `llc`
  * `clang`

The first compiler does not need to be fast. It needs to be clear, testable, and easy to change.

### 2.2 Long-Term Compiler Split

Prototype:

```text
Python parser/typechecker/HIR/MIR
Python xDSL MLIR emitter
external MLIR/LLVM tools
```

Later:

```text
Frontend: possibly Python, OCaml, Rust, or self-hosted Flex
MLIR dialects/passes: C++ or xDSL depending on maturity
CLI/package/build system: Rust or Python initially
```

Do not commit prematurely to a forever implementation language.

The key asset is the Flex frontend architecture and IR design.

---

## 3. MVP Scope

The MVP should compile a small subset of Flex to native code and run first-class tests.

### 3.1 MVP Must Support

* `.flx` files
* `flx parse`
* `flx check`
* `flx fmt`
* `flx test`
* `flx emit-mlir`
* `flx run`
* integer literals
* boolean literals
* string literals, initially simple or runtime-backed
* functions
* immutable `let`
* mutable local `mut`
* `if`
* `while`
* simple records
* simple ADTs
* `match`
* `Result<T, E>`
* `Ok(value)`
* `Err(error)`
* `?` result propagation
* explicit `uses { ... }` effects in function signatures
* first-class `test "name" { ... }` blocks
* `assert`
* `assert_eq`
* test harness generation
* `region name { ... }` syntax, even if early lowering is shallow
* basic MLIR emission
* basic native executable generation

### 3.2 MVP Can Defer

* full hygienic macros
* full comptime execution
* full region lifetime inference
* generic type inference
* trait/typeclass resolution
* GPU lowering
* package manager
* LSP
* optimizer
* borrow checker
* async/concurrency
* persistent collections
* FFI
* full standard library
* property tests
* fuzz tests
* benchmark blocks

### 3.3 MVP Success Criteria

The MVP succeeds when this program can parse, typecheck, emit MLIR, lower to LLVM, and run:

```flx
module Main

fn add(a: I64, b: I64) -> I64 =
{
  a + b
}

fn main() -> I64 =
{
  add(20, 22)
}

test "add works" {
  assert_eq(add(20, 22), 42)
}
```

Commands:

```sh
flx run examples/add.flx
flx test examples/add.flx
```

Expected test output:

```text
running 1 test

ok Main / add works

1 passed, 0 failed
```

---

## 4. Language Syntax

Flex syntax should feel ML/F#/Elm-adjacent, but use braces where useful for parser simplicity and systems readability.

### 4.1 Modules

```flx
module Video.Decode {
  pub fn width() -> I64 = { 1920 }
}

module Main {
  import Video.Decode

  fn main() -> I64 = { Video.Decode.width() }
}
```

The legacy header form is still accepted as a shorthand for a single module
whose body is the rest of the file:

```flx
module Main

fn main() -> I64 = { 0 }
```

A file may contain more than one `module Name { ... }` block. Module blocks are
lexical scopes for `pub`/private visibility; a private declaration is visible
inside its own block only.

### 4.2 Imports

```flx
import Core.Result
import Std.Fs
import Very.Long.Module as Mod
import Math.Stats.{mean, median}
```

Imports load modules by name. The conventional path `Core/Result.flx` is tried
first; if no such file exists, the loader can find a `module Core.Result { ... }`
block in another loaded root file. Public functions can also be called with the
full module prefix, e.g. `Core.Result.unwrap(x)`.

Plain imports make a module's public declarations available unqualified in the
current module. Aliased imports bind the alias for qualified calls
(`Mod.run()`), without importing public names unqualified. Selective imports
bind only the listed public names unqualified. Import scope is per module, so a
transitive dependency's public declarations do not leak into the importer.

### 4.3 Functions

```flx
fn add(a: I64, b: I64) -> I64 =
{
  a + b
}
```

Expression-bodied shorthand may come later:

```flx
fn add(a: I64, b: I64) -> I64 = a + b
```

Functions may declare effects:

```flx
fn read_config(path: Path)
  -> Result<Config, ConfigError>
  uses { Fs, Alloc } =
{
  ...
}
```

### 4.4 Immutable Bindings

Bindings are immutable by default.

```flx
let x = 10
let y = x + 1
```

Reassignment to immutable bindings is illegal.

```flx
let x = 1
x = 2 // compile error
```

### 4.5 Mutable Locals

Mutation is explicit.

```flx
mut total = 0

for x in xs {
  total = total + x
}

total
```

Local mutation inside a function is not automatically considered a side effect if it does not escape.

This is valid pure code:

```flx
fn sum(xs: Slice<I64>) -> I64 =
{
  mut total = 0

  for x in xs {
    total = total + x
  }

  total
}
```

### 4.6 Records

```flx
type User =
{
  id: U64
  email: String
  active: Bool
}
```

Record construction:

```flx
let user =
{
  id = 1
  email = "matt@example.com"
  active = true
}
```

Record update:

```flx
let updated = { user with active = false }
```

Records are immutable by default.

### 4.7 ADTs / Discriminated Unions

```flx
type Option<T> =
  | Some(T)
  | None

type Result<T, E> =
  | Ok(T)
  | Err(E)
```

Domain example:

```flx
type DecodeError =
  | BadHeader
  | UnsupportedCodec(String)
  | CorruptFrame(I64)
```

### 4.8 Pattern Matching

```flx
match result {
  Ok(value) => value
  Err(error) => 0
}
```

ADTs should eventually require exhaustive matches.

MVP may implement basic exhaustiveness later, but the AST and type model should be designed for it.

### 4.9 Pipe Operator

The pipe operator is core syntax.

```flx
users
|> Array.filter(User.is_active)
|> Array.map(User.summary)
```

Initial desugaring:

```flx
x |> f
```

becomes:

```flx
f(x)
```

For multi-argument functions:

```flx
x |> f(a, b)
```

becomes:

```flx
f(x, a, b)
```

This convention can be revisited, but first-argument pipe is simple and Elixir/F#-friendly.

---

## 5. First-Class Tests

Flex supports first-class `test` blocks as top-level declarations.

Tests live next to the code they exercise.

```flx
module Math

fn add(a: I64, b: I64) -> I64 =
{
  a + b
}

test "add returns the sum" {
  assert_eq(add(2, 3), 5)
}
```

Test blocks are compiled only in test builds.

```sh
flx test
flx test src/math.flx
flx test --filter "add"
```

A `test` block is semantically similar to an anonymous zero-argument function registered with the test runner.

Conceptually:

```flx
test "add returns the sum" {
  assert_eq(add(2, 3), 5)
}
```

lowers to something like:

```flx
fn __test_add_returns_the_sum() -> TestResult =
{
  assert_eq(add(2, 3), 5)
  Ok(())
}
```

The generated test function name should be stable, hygienic, and not visible to normal user code.

### 5.1 Test Syntax

Basic test:

```flx
test "parse_int accepts digits" {
  let result = parse_int("123")
  assert_eq(result, Ok(123))
}
```

Tests may use `?` for early failure:

```flx
test "parse_int returns value" {
  let value = parse_int("123")?
  assert_eq(value, 123)
}
```

Tests may declare effects explicitly:

```flx
test "loads config file" uses { Fs, Alloc } {
  region scratch {
    let config = Config.load("fixtures/app.toml", scratch)?
    assert_eq(config.port, 8080)
  }
}
```

Tests may use local mutation:

```flx
test "counter increments" {
  mut x = 0
  x = x + 1
  assert_eq(x, 1)
}
```

### 5.2 Assertions

Initial test standard library:

```flx
assert(condition)
assert_eq(actual, expected)
assert_ne(actual, expected)
fail(message)
```

Examples:

```flx
test "boolean assertion" {
  assert(2 + 2 == 4)
}

test "equality assertion" {
  assert_eq(String.length("flex"), 4)
}

test "forced failure" {
  fail("not implemented yet")
}
```

Assertions should produce useful diagnostics:

```text
test failed: parse_int returns value

  src/parse.flx:18:3
    |
 18 |   assert_eq(value, 123)
    |   ^^^^^^^^^^^^^^^^^^^^^
    |
    actual:   124
    expected: 123
```

### 5.3 Test Effects

Tests obey the normal effect system.

This should fail:

```flx
test "bad fs test" {
  Fs.read("fixture.txt")
}
```

Diagnostic:

```text
error[EFFECT001]: test uses Fs.read but does not declare Fs effect

help: add uses { Fs }

test "bad fs test" uses { Fs } {
  Fs.read("fixture.txt")
}
```

This should pass:

```flx
test "reads fixture" uses { Fs, Alloc } {
  region scratch {
    let bytes = Fs.read("fixtures/input.txt", scratch)?
    assert(bytes.len > 0)
  }
}
```

### 5.4 Test Regions

Tests should be able to use regions directly.

```flx
test "parser allocates inside scratch region" uses { Alloc } {
  region scratch {
    let ast = Parser.parse("1 + 2", scratch)?
    assert_eq(ast.kind, AstKind.Binary)
  }
}
```

Region escape rules still apply.

This should fail:

```flx
test "bad region escape" uses { Alloc } {
  let ast =
    region scratch {
      Parser.parse("1 + 2", scratch)?
    }

  assert(ast != null)
}
```

The compiler should reject values escaping from `scratch`.

### 5.5 Test Discovery

The compiler discovers test blocks during parsing.

Test metadata should include:

* module name
* test name string
* source file
* line/column
* declared effects
* lowered internal function symbol

Example internal model:

```text
TestDecl {
  module: "Math"
  name: "add returns the sum"
  source: "src/math.flx"
  span: 8:1-10:1
  effects: {}
  body: ...
}
```

### 5.6 Test Harness Lowering

`flx test` synthesizes a test harness.

Given:

```flx
test "a" { ... }
test "b" { ... }
```

The compiler creates a hidden test entrypoint that runs all discovered tests and reports:

```text
running 2 tests

ok   Math / a
fail Math / b

1 passed, 1 failed
```

MVP output can be simple plain text.

Long-term output formats:

```sh
flx test --format pretty
flx test --format json
flx test --format junit
```

### 5.7 Future Test Features

Later versions should consider:

* property tests
* fuzz tests
* snapshot tests
* compile-fail tests
* benchmark blocks
* fixtures as tracked build inputs
* deterministic test effects
* test-only imports
* test-only helper functions
* generated tests from macros
* `derive Arbitrary`
* `derive Shrink`

Potential future property syntax:

```flx
property "json roundtrip" user: Gen<User> {
  let encoded = Json.encode(user)
  let decoded = Json.decode<User>(encoded)?
  assert_eq(decoded, user)
}
```

Potential future benchmark syntax:

```flx
bench "parse large file" uses { Fs, Alloc } {
  region scratch {
    let bytes = Fs.read("fixtures/large.flex", scratch)?
    Parser.parse(bytes, scratch)?
  }
}
```

Tests are part of the language, not an afterthought.

---

## 6. Failure Model

Flex uses explicit `Result` values for expected failure.

```flx
type Result<T, E> =
  | Ok(T)
  | Err(E)
```

Expected failure includes:

* parse errors
* validation errors
* missing files
* network errors
* database errors
* permission errors
* unsupported input
* recoverable domain failures

Unexpected programmer bugs use `panic`.

```flx
panic("unreachable")
```

`panic` is for:

* violated invariants
* impossible states
* compiler bugs
* unchecked indexing mistakes
* internal corruption

No exceptions in MVP.

### 6.1 Result Propagation

The `?` operator unwraps `Ok(value)` or returns `Err(error)` from the current function/test.

```flx
fn load_user(path: Path) -> Result<User, LoadError> uses { Fs, Alloc } =
{
  let bytes = Fs.read(path)?
  let json = Json.parse(bytes)?
  let user = User.decode(json)?
  Ok(user)
}
```

MVP desugaring:

```flx
let x = expr?
```

becomes roughly:

```flx
match expr {
  Ok(value) => let x = value
  Err(error) => return Err(error)
}
```

Later versions need typed error conversion.

---

## 7. Effects

Effects are explicit in function signatures and test declarations.

```flx
fn read_config(path: Path)
  -> Result<Config, ConfigError>
  uses { Fs, Alloc } =
{
  ...
}
```

```flx
test "loads config" uses { Fs, Alloc } {
  ...
}
```

Effects describe what the function or test is allowed to do.

Initial effect names:

```flx
Fs
Http
Db
Log
Time
Alloc
Random
Process
Unsafe
```

Pure functions omit `uses`.

```flx
fn validate(user: User) -> Result<User, ValidationError> =
{
  ...
}
```

The MVP effect checker can be simple:

* every function has a declared effect set
* every test has a declared effect set
* calling a function with effects requires the caller/test to include at least those effects
* pure functions cannot call effectful functions
* allocation eventually maps to `Alloc`

Example:

```flx
fn pure_add(a: I64, b: I64) -> I64 =
{
  a + b
}

fn bad(path: Path) -> Result<String, FsError> =
{
  Fs.read(path) // compile error: missing uses { Fs }
}
```

### 7.1 Capabilities Later

Long-term, effects should probably be capability-backed.

Instead of global magic:

```flx
fn save_user(user: User) -> Result<(), DbError> uses { Db }
```

Prefer:

```flx
fn save_user(db: Db, user: User) -> Result<(), DbError> =
{
  db.save(user)
}
```

The effect system can infer or check the capability effects.

This keeps tests simple:

```flx
save_user(FakeDb.memory(), user)
```

No DI framework. No mocks. Just values.

---

## 8. Regions and Allocation

Regions are scoped allocators with compiler-enforced escape rules.

```flx
region scratch {
  let ast = Parser.parse(source, scratch)?
  let checked = Typecheck.run(ast, scratch)?
  Codegen.emit(checked, output)?
}
```

Everything allocated in `scratch` dies at the end of the region.

The goal:

* no `defer` spaghetti
* no hidden GC dependency
* no manual free chains
* no escaping references to dead memory
* predictable bulk allocation
* functional style without allocation chaos

### 8.1 Region Escape Rule

This should be illegal:

```flx
fn bad(source: String) -> Ast =
{
  region scratch {
    let ast = Parser.parse(source, scratch)?
    ast // compile error: ast allocated in scratch escapes
  }
}
```

This should be legal:

```flx
fn parse_into(source: String, out: Region) -> Result<Ast, ParseError> uses { Alloc } =
{
  Parser.parse(source, out)
}
```

MVP does not need full lifetime inference, but it should represent region ownership in HIR/MIR.

### 8.2 Allocation Is Explicit

Any function that allocates should eventually declare `uses { Alloc }`.

```flx
fn parse(source: String, region: Region)
  -> Result<Ast, ParseError>
  uses { Alloc } =
{
  ...
}
```

In MVP, allocation checking can be shallow. The syntax and IR should still preserve allocation intent.

---

## 9. Performance Model

Flex must keep performance predictable and inspectable.

The language should eventually support:

```flx
fn checksum(bytes: Slice<U8>) -> U32
  no_alloc
  no_panic
  pure =
{
  ...
}
```

Initial contracts:

* `pure`
* `no_alloc`
* `no_panic`

MVP may parse these but not enforce them fully.

### 9.1 Cost Visibility

The CLI should eventually explain cost.

```sh
flx explain-cost src/users.flx
```

Example output:

```text
Function active_names:

Array.filter:
  allocates Array<User>

Array.map:
  allocates Array<UserSummary>

Array.sort:
  sorts in-place

Total:
  2 dynamic array allocations

Suggestion:
  use Iter.filter |> Iter.map |> Iter.collect_into to avoid intermediate arrays
```

This is core to Flex.

The language can be ergonomic by default, but the cost must be inspectable under pressure.

### 9.2 Collections Should Expose Cost

Different collection modules imply different cost models:

```flx
Array.map   // eager, may allocate
Iter.map    // lazy/stack iterator, should not allocate unless closure captures
Stream.map  // effectful/lazy, may suspend
List.map    // persistent list, allocates nodes
```

MVP can start with simple arrays/slices later. The design should not collapse all collection operations into hidden allocation soup.

---

## 10. Macros and Comptime

Flex should eventually support hygienic comptime macros.

The dream feature:

```flx
macro derive_json(T) =
  comptime {
    let fields = reflect.fields(T)

    quote {
      impl JsonDecode for unquote(T) {
        fn decode(json: Json) -> Result<unquote(T), DecodeError> =
          unquote(build_decoder_ast(T, fields))
      }
    }
  }
```

Flex macros should combine:

* `comptime`
* `quote`
* `unquote`
* `unquote_splice`
* hygienic symbol generation
* typed macro output
* deterministic build inputs
* inspectable expansion

### 10.1 Macro Principles

Macros must be:

1. **Hygienic by default**
   Generated identifiers should not accidentally capture caller names.

2. **Typed after expansion**
   Macro output goes through the same type checker as handwritten code.

3. **Inspectable**
   Users can run:

   ```sh
   flx expand src/api.flx
   ```

4. **Deterministic unless explicitly effectful**
   Comptime file reads, env access, shell commands, randomness, and time must be explicit.

5. **Zero runtime reflection tax**
   Macros generate concrete code at compile time.

### 10.2 MVP Macro Scope

Do not implement full macros in MVP.

Instead:

* reserve syntax
* design AST nodes for `quote`, `unquote`, `comptime`
* implement `flx expand` as a desugaring viewer
* support showing lowered pipe, `?`, and test harness expansion
* implement a tiny built-in derive later, such as `derive Eq`

---

## 11. Data Layout

Flex must eventually expose layout control.

Examples:

```flx
repr(C)
type Vec2 =
{
  x: F32
  y: F32
}

repr(packed)
type Header =
{
  magic: U32
  flags: U16
  version: U16
}

repr(u8)
type Token =
  | Ident(String)
  | Int(I64)
  | LParen
  | RParen
```

The CLI should eventually support:

```sh
flx layout Token
```

Example output:

```text
Token
  size: 24 bytes
  align: 8 bytes
  tag: u8
  payload: max 16 bytes
```

MVP does not need full layout control, but the IR should not prevent it.

---

## 12. Unsafe

Flex should eventually provide a small, explicit `unsafe` block.

```flx
unsafe {
  ptr.write(value)
}
```

Rules:

* unsafe is lexical
* unsafe is visually obvious
* unsafe may require `uses { Unsafe }`
* unsafe should not infect unrelated code
* unsafe should be easy to audit

No unsafe in MVP unless required by backend runtime stubs.

---

## 13. Concurrency

Flex should not copy Elixir’s actor runtime.

Long-term concurrency should be structured and explicit.

Possible future syntax:

```flx
with task_group group {
  let user_task = group.spawn(fetch_user(id))
  let orders_task = group.spawn(fetch_orders(id))

  let user = user_task.await?
  let orders = orders_task.await?

  Ok({ user = user, orders = orders })
}
```

Concurrency is an effect/capability, not the soul of the language.

MVP has no concurrency.

---

## 14. MLIR Backend Strategy

Flex should not lower to raw LLVM IR too early.

Use MLIR so the compiler can preserve useful high-level structure before lowering.

Initial dialect targets:

* `builtin`
* `func`
* `arith`
* `cf`
* `scf`
* `memref`
* `llvm`

Later GPU/HPC dialects:

* `linalg`
* `affine`
* `vector`
* `gpu`

### 14.1 Backend Pipeline

Initial CPU pipeline:

```text
Flex MIR
  -> xDSL/MLIR module
  -> canonicalize
  -> lower control flow
  -> lower memrefs
  -> lower to LLVM dialect
  -> translate to LLVM IR
  -> object file
  -> native executable
```

Initial implementation may emit textual MLIR and shell out to tools.

That is acceptable for the prototype.

### 14.2 Preserve High-Level Loops

Do not lower array operations and loops into opaque pointer soup too early.

This:

```flx
users
|> Array.filter(User.is_active)
|> Array.map(User.summary)
```

should preserve enough structure that future passes can reason about allocation, fusion, vectorization, or GPU lowering.

MVP can lower simply, but HIR/MIR should keep operations distinct.

---

## 15. Internal Compiler Architecture

### 15.1 Source Pipeline

```text
SourceText
  -> Tokens
  -> Parsed AST
  -> Resolved AST
  -> Typed HIR
  -> Expanded HIR
  -> Flex MIR
  -> MLIR/xDSL
  -> LLVM/native
```

### 15.2 AST

AST should preserve syntax.

Examples:

* module declarations
* imports
* type declarations
* function declarations
* test declarations
* let bindings
* mut bindings
* if expressions
* match expressions
* region blocks
* effect annotations
* macro syntax placeholders
* pipe expressions
* record construction
* record update
* ADT construction

AST is for source fidelity and diagnostics.

### 15.3 HIR

HIR should represent checked language meaning.

HIR should include:

* resolved symbols
* type information
* effect information
* region information
* test metadata
* desugared pipe expressions
* desugared `?`
* expanded test harnesses in test mode
* expanded macros later
* explicit bindings
* typed patterns

HIR is where typechecking and effect checking live.

### 15.4 MIR

MIR should be simpler and closer to control flow.

MIR should include:

* basic blocks eventually
* explicit branches
* explicit returns
* explicit region enter/exit
* explicit allocation operations
* lowered pattern matches
* lowered result propagation
* lowered test functions
* generated test runner entrypoint
* simple SSA-ish temporaries

MIR is the source of MLIR emission.

---

## 16. Type System

MVP type system should support:

* primitive types:

  * `I64`
  * `U64`
  * `Bool`
  * `String`
  * `Unit`
* function types
* test body typechecking
* record types
* ADTs
* `Result<T, E>`
* basic generic type declarations if manageable

Can defer:

* full Hindley-Milner inference
* higher-kinded types
* typeclasses
* row polymorphism
* effect polymorphism
* region polymorphism
* associated types
* dependent types

### 16.1 Type Inference Philosophy

Flex should eventually infer local types but keep public APIs readable.

Good:

```flx
let x = 42
```

Also good:

```flx
fn parse_user(json: Json) -> Result<User, DecodeError> =
{
  ...
}
```

Public function signatures should generally be explicit.

---

## 17. Project Layout

Suggested repository layout:

```text
flex/
  README.md
  SPEC.md
  pyproject.toml

  src/
    flx/
      __init__.py

      cli.py

      syntax/
        lexer.py
        parser.py
        grammar.lark
        ast.py

      resolve/
        names.py
        symbols.py

      types/
        types.py
        infer.py
        check.py

      effects/
        effects.py
        check.py

      regions/
        regions.py
        check.py

      testsys/
        discover.py
        harness.py
        assertions.py

      hir/
        hir.py
        lower_ast.py

      mir/
        mir.py
        lower_hir.py

      backend/
        mlir_emit.py
        xdsl_emit.py
        toolchain.py

      diagnostics/
        diagnostic.py
        render.py

      runtime/
        core.flx
        test.flx

  examples/
    hello.flx
    add.flx
    result.flx
    records.flx
    regions.flx
    tests.flx

  tests/
    test_parser.py
    test_typecheck.py
    test_effects.py
    test_regions.py
    test_testsys.py
    test_mir.py
    test_mlir_emit.py

  snapshots/
    parser/
    hir/
    mir/
    mlir/
    diagnostics/
```

---

## 18. CLI Commands

### 18.1 `flx parse`

Parse and print AST.

```sh
flx parse examples/hello.flx
```

### 18.2 `flx check`

Parse, resolve, typecheck, effect-check, and region-check.

```sh
flx check examples/hello.flx
```

### 18.3 `flx test`

Discover, compile, and run tests.

```sh
flx test
flx test examples/add.flx
flx test --filter "parse"
```

Useful test flags:

```sh
flx test --emit-harness
flx test --emit-mlir
flx test --format pretty
flx test --format json
flx test --format junit
```

MVP only needs plain output.

### 18.4 `flx emit-hir`

Emit typed HIR for debugging.

```sh
flx emit-hir examples/hello.flx
```

### 18.5 `flx emit-mir`

Emit MIR.

```sh
flx emit-mir examples/hello.flx
```

### 18.6 `flx emit-mlir`

Emit MLIR text.

```sh
flx emit-mlir examples/hello.flx
```

### 18.7 `flx run`

Compile and run.

```sh
flx run examples/hello.flx
```

### 18.8 `flx build`

Build native executable.

```sh
flx build examples/hello.flx -o hello
```

### 18.9 `flx expand`

Show macro/desugar-expanded source/HIR.

```sh
flx expand examples/routes.flx
```

MVP should at least show desugared pipe, `?`, and test harness expansion.

### 18.10 `flx explain-effects`

Explain function and test effects.

```sh
flx explain-effects examples/app.flx
```

### 18.11 `flx explain-cost`

Explain obvious allocation/cost behavior.

```sh
flx explain-cost examples/users.flx
```

MVP may stub this.

### 18.12 `flx fmt`

Format Flex source files and enforce canonical style in CI.

```sh
flx fmt src
flx fmt --check src examples
flx fmt --stdout examples/add.flx
```

---

## 19. Diagnostics

Flex diagnostics should be practical and compiler-grade.

Example:

```text
error[EFFECT001]: function calls Fs.read but does not declare Fs effect

  examples/config.flx:8:15
    |
  8 |   let bytes = Fs.read(path)?
    |               ^^^^^^^ requires effect Fs

help: add Fs to the function's effect set

  fn load_config(path: Path) -> Result<Config, ConfigError>
    uses { Fs, Alloc } =
```

Test diagnostic example:

```text
test failed: add works

  examples/add.flx:10:3
    |
 10 |   assert_eq(add(20, 22), 41)
    |   ^^^^^^^^^^^^^^^^^^^^^^^^^^
    |
    actual:   42
    expected: 41
```

Diagnostics should include:

* file
* line
* column
* error code
* explanation
* source span
* suggested fix where obvious

---

## 20. Testing Strategy For The Compiler

Use snapshot tests heavily.

Each example should have snapshots for:

```text
source
AST
HIR
MIR
MLIR
diagnostics
test harness
```

Recommended test categories:

### Parser Tests

* functions
* records
* ADTs
* match
* pipes
* regions
* effects
* result propagation
* mutable locals
* `test "name" { ... }`
* `test "name" uses { ... } { ... }`

### Typechecker Tests

* primitive arithmetic
* function calls
* record fields
* ADT constructors
* match branch compatibility
* Result propagation
* invalid mutations
* assertion typechecking
* test body typechecking

### Effect Tests

* pure function calling pure function
* pure function calling effectful function fails
* effectful caller accepts callee effects
* test missing effect fails
* test declared effect passes
* missing effect diagnostic

### Region Tests

* region syntax parses
* values allocated in region tracked in IR
* escaping region value eventually fails
* region enter/exit represented in MIR
* test region behavior

### Test System Tests

* test discovery
* test filter
* test harness generation
* passing assertion
* failing assertion
* `?` inside test
* stable generated test names

### Backend Tests

* integer arithmetic to MLIR
* function calls to MLIR
* if expression to MLIR
* simple loops to MLIR
* main function returns exit code
* test harness emits MLIR
* test binary returns success/failure exit code

---

## 21. Example Programs

### 21.1 Hello Integer

```flx
module Main

fn main() -> I64 =
{
  42
}
```

### 21.2 Local Mutation

```flx
module Main

fn sum_to(n: I64) -> I64 =
{
  mut i = 0
  mut total = 0

  while i <= n {
    total = total + i
    i = i + 1
  }

  total
}

fn main() -> I64 =
{
  sum_to(10)
}

test "sum_to works" {
  assert_eq(sum_to(10), 55)
}
```

### 21.3 Result

```flx
module Main

type MathError =
  | DivideByZero

fn div(a: I64, b: I64) -> Result<I64, MathError> =
{
  if b == 0 {
    Err(MathError.DivideByZero)
  } else {
    Ok(a / b)
  }
}

fn main() -> I64 =
{
  match div(10, 2) {
    Ok(x) => x
    Err(_) => 1
  }
}

test "division succeeds" {
  let x = div(10, 2)?
  assert_eq(x, 5)
}

test "division by zero fails" {
  assert_eq(div(10, 0), Err(MathError.DivideByZero))
}
```

### 21.4 Region Syntax

```flx
module Main

fn main() -> I64 =
{
  region scratch {
    let x = 40
    let y = 2
    x + y
  }
}

test "region block returns value" {
  let answer =
    region scratch {
      40 + 2
    }

  assert_eq(answer, 42)
}
```

### 21.5 Effects

```flx
module Main

fn log_answer(answer: I64) -> Unit uses { Log } =
{
  Log.info("answer")
}

fn main() -> I64 uses { Log } =
{
  log_answer(42)
  42
}

test "log_answer can be called from effectful test" uses { Log } {
  log_answer(42)
  assert(true)
}
```

### 21.6 First-Class Tests

```flx
module Main

fn add(a: I64, b: I64) -> I64 =
{
  a + b
}

test "add works" {
  assert_eq(add(20, 22), 42)
}

test "add is commutative" {
  assert_eq(add(1, 2), add(2, 1))
}
```

---

## 22. Non-Goals

Flex is not trying to be:

* Haskell
* Rust with nicer syntax
* Elixir on LLVM
* a BEAM clone
* a JVM/CLR language
* a GC-first language
* a theorem prover
* a dependent type research project
* a Scala macro sequel
* a C preprocessor with better clothes
* a GPU language only
* an MLIR skin

Flex owns its frontend semantics.

MLIR is the lowering ecosystem, not the language’s soul.

---

## 23. Early Milestones

### Milestone 1: Parser Spike

* create repo
* implement CLI shell
* parse module
* parse function
* parse literals
* parse `let`
* parse `mut`
* parse `if`
* parse `while`
* parse simple type declarations
* parse `test "name" { ... }`
* snapshot AST

Done when:

```sh
flx parse examples/hello.flx
flx parse examples/tests.flx
```

prints stable AST.

### Milestone 2: Typecheck Tiny Core

* primitive types
* function symbols
* local bindings
* arithmetic
* booleans
* if branch type checking
* basic diagnostics

Done when:

```sh
flx check examples/hello.flx
```

passes and invalid examples fail cleanly.

### Milestone 3: First-Class Tests

* discover test blocks
* typecheck test bodies
* implement `assert`
* implement `assert_eq`
* lower tests to hidden functions
* generate test harness
* add `flx test`

Done when:

```sh
flx test examples/add.flx
```

prints:

```text
running 1 test

ok Main / add works

1 passed, 0 failed
```

### Milestone 4: Emit Basic MLIR

* lower integer arithmetic
* lower function
* lower return
* lower test functions
* emit textual MLIR
* optionally run through MLIR tools

Done when:

```sh
flx emit-mlir examples/hello.flx
flx test --emit-mlir examples/add.flx
```

produces usable MLIR.

### Milestone 5: Native Hello And Native Tests

* shell out to MLIR/LLVM toolchain
* produce executable
* run executable
* return process exit code
* produce test executable
* failing tests produce non-zero exit code

Done when:

```sh
flx run examples/hello.flx
flx test examples/add.flx
```

work end-to-end.

### Milestone 6: ADTs And Match

* parse ADTs
* construct variants
* typecheck variants
* lower simple matches
* support `Result`
* support `?`
* support `?` inside tests

Done when result examples compile and tests run.

### Milestone 7: Effects

* parse `uses`
* annotate functions
* annotate tests
* check call graph effects
* produce missing-effect diagnostics

Done when pure/effectful examples behave correctly.

### Milestone 8: Regions

* parse `region`
* represent region enter/exit in HIR/MIR
* model region allocation
* begin escape checking
* support regions in tests

Done when region examples compile and obvious escapes fail.

### Milestone 9: Cost/Expansion Introspection

* `flx expand`
* `flx explain-effects`
* basic `flx explain-cost`
* show desugared tests/harnesses
* show desugared `?`
* show desugared pipes

Done when the compiler starts feeling like Flex, not just a toy language.

---

## 24. Guiding Principle

When design choices conflict, prefer this order:

1. Explicit semantics
2. Predictable performance
3. Clear diagnostics
4. First-class testing
5. Simple implementation
6. Syntax beauty
7. Advanced abstraction

Flex should be pleasant, but not magical.

The point is not to hide cost.

The point is to make high-level code honest.
