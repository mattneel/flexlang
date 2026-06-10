# Traits and Generics

Flex has **traits** (sets of method signatures), **impls** (their concrete
implementations), and **bounded generic functions** that abstract over any type
satisfying a trait. All dispatch is **static** — resolved at compile time and
lowered to direct calls. There are no vtables, no boxing, and no runtime type
information; a method call costs exactly as much as the equivalent free function.

## Traits

A trait names a set of methods. Inside a trait, `Self` stands for the type that
will eventually implement it:

```flex
trait Show =
{
  fn show(self: Self) -> String
}
```

`Show` and `Eq` are built in (they are what `derive` targets), but you can
declare your own. A trait may list several methods.

## Impls

An `impl` provides a trait's methods for one concrete type:

```flex
type Point = { x: I64, y: I64 }

impl Show for Point =
{
  fn show(self: Point) -> String = { "(" ++ to_str(self.x) ++ ", " ++ to_str(self.y) ++ ")" }
}
```

You can implement a trait for primitives and ADTs too (`impl Show for I64`,
`impl Show for Color`). Once an impl is in scope, call its methods with the
familiar receiver syntax — they resolve to that impl statically:

```flex
let p = { x = 3, y = 4 }
p.show()        // -> "(3, 4)"
```

Field access wins over methods, so `p.x` is always the field. The checker
rejects an impl that is missing a method (`IMPL003`), adds an unknown one
(`IMPL004`), whose signature disagrees with the trait (`IMPL005`), or that
duplicates an existing impl (`IMPL006`). Calling a method with no matching impl
is `DISP001`.

## `derive`

For records and (single-payload) ADTs, `derive` writes the impl for you:

```flex
derive(Eq, Show) type Color =
  | Red
  | Green
  | Blue
```

This generates `impl Eq for Color` and `impl Show for Color` as ordinary impls —
run `flx expand` to see exactly what they look like. Derived methods dispatch
just like hand-written ones, so `Green.show()` and `c.eq(d)` work immediately.

## Bounded generic functions

A generic function abstracts over a type parameter. A **bound** (`T: Show`)
constrains it to types that implement a trait, and lets the body call that
trait's methods on values of type `T`:

```flex
fn announce<T: Show>(label: String, value: T) -> String uses { Log } =
{
  let line = label ++ ": " ++ value.show()
  Log.info(line)
  line
}
```

Effects propagate through generic calls like any other call — a caller of
`announce` must itself declare `uses { Log }`. Multiple bounds combine with `+`
(`<T: Show + Eq>`), and a function may take several type parameters
(`<A, B>`). A parameter with no bound is fully unconstrained:

```flex
fn first<A, B>(a: A, b: B) -> A = { a }
```

The compiler infers each type parameter from the call's arguments
(`announce("count", 7)` picks `T = I64`). It reports a type that does not satisfy
a bound (`BOUND001`), a parameter it cannot infer from the arguments
(`BOUND003`), and an unknown trait used as a bound (`BOUND004`).

## How it works: monomorphization

Generics are compiled by **monomorphization**: the compiler makes one
specialized copy of the function for each distinct set of type arguments it is
actually called with, substitutes the concrete types in, and type-checks and
lowers each copy as an ordinary function. `announce("point", p)` and
`announce("count", 7)` produce two specializations — one over `Point`, one over
`I64` — and each call goes directly to its copy.

Because each copy is checked independently, an error that depends on the concrete
type only appears when the function is instantiated at an offending type, and it
points at the generic's body. Specialization runs to a fixed point, so a generic
that (transitively) calls other generics pulls in everything it needs;
non-terminating instantiation (polymorphic recursion) is reported as `MONO001`.

## A complete example

```flex
{{#include ../examples/traits.flx}}
```

```sh
flx run    examples/traits.flx   # prints the announcements, exits 7
flx test   examples/traits.flx
flx expand examples/traits.flx   # shows the derived Show impl for Color
```
