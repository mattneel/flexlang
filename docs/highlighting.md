# Syntax Highlighting

Flex ships a [Pygments](https://pygments.org) lexer (`FlexLexer`, alias
`flex`). It powers three things from a single grammar:

1. the `flx highlight` command (truecolor / 256-color terminal output, or HTML);
2. any Pygments-aware tool — `pygmentize`, [Rich](https://github.com/Textualize/rich), Sphinx, mkdocs;
3. **this book** — every `flex` / `flx` code block you see is rendered by an
   mdBook preprocessor (`flx-mdbook`) that calls the same lexer.

## In the terminal

```sh
flx highlight examples/add.flx
pygmentize -l flex examples/add.flx
pygmentize examples/add.flx          # picks Flex by the .flx extension
```

> Pygments' built-in **Felix** lexer also claims `*.flx`. The Flex lexer sets a
> higher priority so extension lookup resolves to Flex; use the `flex` alias
> (not `flx`) for unambiguous explicit selection.

## What gets highlighted

The grammar tracks the language's lexical surface: declaration and control
keywords, `uses { … }` effect sets, function contracts (`pure`, `no_alloc`,
`no_panic`), primitive types, ADT constructors, the pipe (`|>`) and
result-propagation (`?`) operators, and `//` comments.

```flex
module Demo

type Shape =
  | Circle(F64)
  | Rect(F64, F64)

fn area(s: Shape) -> F64 =
{
  match s {
    Circle(r) => 3.14159 * r * r
    Rect(w, h) => w * h
  }
}

test "area of unit rect" uses { Log } {
  let a = area(Rect(1.0, 1.0))?
  assert_eq(a, 1.0)
}
```
