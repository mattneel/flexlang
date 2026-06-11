# Lists and Iteration

`List<T>` is Flex's growable, indexable collection. Everything here runs
identically on the interpreter and the native backend.

## Building and using lists

```flex
module Main
import Std.IO
import Std.List

fn main() -> I64 uses { Log } = {
  let xs = [10, 20, 30]        // a list literal
  List.push(xs, 40)            // grow in place
  List.set(xs, 0, 11)          // replace an element
  println(to_str(xs[2]))       // index (panics out of bounds)
  println(to_str(List.len(xs)))

  mut ys: List<String> = []    // an empty list needs a type annotation
  List.push(ys, "hi")

  mut total = 0
  for x in xs { total = total + x }   // for-in iterates any List
  for i in range(0, 5) { total = total + i }  // Std.List.range: [a, b)
  total
}
```

The built-in operations are `[…]` literals, `xs[i]`, `List.push(xs, v)`,
`List.len(xs)`, `List.set(xs, i, v)`, and `for x in xs { … }`. Elements can be
any type — integers, strings, records, ADTs, other lists.

## Reference semantics

A list value is a reference to one growable buffer. `let ys = xs` aliases the
same list; pushing through either name is visible through both. Passing a list
to a function passes the reference — the callee sees (and may grow) the
caller's list. Records and ADTs holding lists hold the reference too.

```flex
let xs = [1]
let ys = xs
List.push(ys, 2)
List.len(xs)        // 2 — same list
```

`==`/`assert_eq` are not defined on lists (reference identity would be a lie
and deep comparison is not free); compare lengths and elements explicitly.

## Mutation during iteration

`for x in xs` snapshots the list's **length** when the loop starts: elements
pushed during the loop are not visited (and an unconditional push can't make
the loop infinite). Element reads stay live, so `List.set` on a not-yet-visited
index is observed.

## Bounds

`xs[i]` and `List.set` panic on an out-of-bounds index, identically on both
backends:

```text
flx: runtime error: index 5 out of bounds (len 2)
```

## Type annotations on bindings

`let` and `mut` accept an optional type: `let xs: List<I64> = []`. The
annotation is what makes an empty list literal typable; it also pins generic
constructors (`let r: Result<I64, String> = Ok(2)`).

## Strings as data

`import Std.Str` provides byte-level string access (Flex strings are UTF-8
bytes; indexing is by byte, so multi-byte sequences can split — lossless, but
not yet Unicode-aware):

```flex
byte_at("A", 0)        // 65 (panics out of bounds)
substr("hello", 1, 3)  // "ell" (clamps at the ends)
char_at("hello", 1)    // "e" — one BYTE as a string
split("a,b,,c", ",")   // ["a", "b", "", "c"]
parse_int("-42")       // Some(-42); None on empty or non-digit input
```

## Program arguments

`Env.argv()` yields the program's own arguments as a `List<String>` — the
arguments only, no executable path. It observes process state: `uses
{ Process }`.

```flex
fn main() -> I64 uses { Process, Log } = {
  for a in Env.argv() { println(a) }
  0
}
```

```console
$ flx run tool.flx alpha beta
alpha
beta
```

Put `flx` flags (like `--native`) before the file; everything after the file
goes to the program (a leading `--` is stripped).

## Not yet

- `xs[i] = v` assignment syntax — use `List.set(xs, i, v)`.
- First-class functions, so no `map`/`filter` yet (M4).
- `pop`, `slice`, sorting — the surface is deliberately small while the
  allocation story (regions) matures. Lists are heap-allocated and reclaimed
  at process exit.
