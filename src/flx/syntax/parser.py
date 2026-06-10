"""Recursive-descent + Pratt parser for Flex.

Statement boundaries are implicit (no significant newlines or semicolons): an
expression ends when the next token can't continue it, which is sufficient for
the MVP grammar. The pipe operator ``|>`` is desugared to a call here, so later
stages never see it.
"""

from __future__ import annotations

from flx.diagnostics import Diagnostic, FlexError, Span
from flx.syntax import ast
from flx.syntax.lexer import tokenize
from flx.syntax.tokens import Token, TokenKind

# Infix binding powers (higher binds tighter). Used for left-associative ops.
_INFIX_BP: dict[TokenKind, int] = {
    TokenKind.PIPE_GT: 1,
    TokenKind.PIPE_PIPE: 2,
    TokenKind.AMP_AMP: 3,
    TokenKind.EQ_EQ: 4,
    TokenKind.BANG_EQ: 4,
    TokenKind.LT: 5,
    TokenKind.LE: 5,
    TokenKind.GT: 5,
    TokenKind.GE: 5,
    TokenKind.PLUS: 6,
    TokenKind.MINUS: 6,
    TokenKind.STAR: 7,
    TokenKind.SLASH: 7,
    TokenKind.PERCENT: 7,
}
_UNARY_BP = 8
_POSTFIX_BP = 9

# Guard against runaway recursion on adversarial deeply-nested input.
_MAX_DEPTH = 200

_OP_TEXT: dict[TokenKind, str] = {
    TokenKind.PIPE_PIPE: "||",
    TokenKind.AMP_AMP: "&&",
    TokenKind.EQ_EQ: "==",
    TokenKind.BANG_EQ: "!=",
    TokenKind.LT: "<",
    TokenKind.LE: "<=",
    TokenKind.GT: ">",
    TokenKind.GE: ">=",
    TokenKind.PLUS: "+",
    TokenKind.MINUS: "-",
    TokenKind.STAR: "*",
    TokenKind.SLASH: "/",
    TokenKind.PERCENT: "%",
}

# Tokens that can begin an expression (used to decide if `return` has a value).
_EXPR_START = {
    TokenKind.INT,
    TokenKind.STRING,
    TokenKind.IDENT,
    TokenKind.KW_TRUE,
    TokenKind.KW_FALSE,
    TokenKind.KW_IF,
    TokenKind.LPAREN,
    TokenKind.MINUS,
    TokenKind.BANG,
}


class Parser:
    def __init__(self, tokens: list[Token], file: str = "<input>") -> None:
        self.tokens = tokens
        self.file = file
        self.pos = 0
        self._depth = 0

    # --- token helpers --------------------------------------------------------

    def _peek(self) -> Token:
        return self.tokens[self.pos]

    def _peek_at(self, offset: int) -> Token:
        i = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[i]

    def _at(self, kind: TokenKind) -> bool:
        return self._peek().kind is kind

    def _advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.kind is not TokenKind.EOF:
            self.pos += 1
        return tok

    def _eat(self, kind: TokenKind) -> bool:
        if self._at(kind):
            self._advance()
            return True
        return False

    def _expect(self, kind: TokenKind, what: str) -> Token:
        if not self._at(kind):
            got = self._peek()
            raise self._error(f"expected {what}, found {self._describe(got)}", got.span)
        return self._advance()

    def _describe(self, tok: Token) -> str:
        if tok.kind is TokenKind.EOF:
            return "end of file"
        return f"{tok.text!r}"

    def _error(self, message: str, span: Span) -> FlexError:
        return FlexError([Diagnostic("PAR001", message, span)])

    # --- module / declarations ------------------------------------------------

    def parse_module(self) -> ast.Module:
        start = self._peek().span
        name = "Main"
        imports: list[str] = []
        if self._at(TokenKind.KW_MODULE):
            self._advance()
            name = self._dotted_name()
        while self._at(TokenKind.KW_IMPORT):
            self._advance()
            imports.append(self._dotted_name())

        items: list[ast.Item] = []
        while not self._at(TokenKind.EOF):
            if self._at(TokenKind.KW_FN):
                items.append(self._fn())
            elif self._at(TokenKind.KW_TEST):
                items.append(self._test())
            elif self._at(TokenKind.KW_TYPE):
                items.append(self._type_decl([]))
            elif self._at(TokenKind.KW_DERIVE):
                items.append(self._type_decl(self._derive_list()))
            elif self._at(TokenKind.KW_MACRO):
                items.append(self._macro())
            else:
                tok = self._peek()
                raise self._error(
                    f"expected a function, test, type, or macro, found {self._describe(tok)}",
                    tok.span,
                )
        end = self._peek().span
        return ast.Module(name, imports, items, start.to(end))

    def _dotted_name(self) -> str:
        parts = [self._expect(TokenKind.IDENT, "a name").text]
        while self._eat(TokenKind.DOT):
            parts.append(self._expect(TokenKind.IDENT, "a name").text)
        return ".".join(parts)

    def _fn(self) -> ast.FnDecl:
        start = self._advance().span  # `fn`
        name = self._expect(TokenKind.IDENT, "a function name").text
        self._expect(TokenKind.LPAREN, "'('")
        params: list[ast.Param] = []
        if not self._at(TokenKind.RPAREN):
            params.append(self._param())
            while self._eat(TokenKind.COMMA):
                params.append(self._param())
        self._expect(TokenKind.RPAREN, "')'")

        return_type: ast.TypeExpr | None = None
        if self._eat(TokenKind.ARROW):
            return_type = self._type()
        effects = self._uses_clause()
        self._expect(TokenKind.EQ, "'='")
        body = self._block_or_expr_body()
        return ast.FnDecl(name, params, return_type, effects, body, start.to(body.span))

    def _param(self) -> ast.Param:
        name_tok = self._expect(TokenKind.IDENT, "a parameter name")
        self._expect(TokenKind.COLON, "':'")
        ty = self._type()
        return ast.Param(name_tok.text, ty, name_tok.span.to(ty.span or name_tok.span))

    def _type(self) -> ast.TypeExpr:
        name_tok = self._expect(TokenKind.IDENT, "a type")
        args: list[ast.TypeExpr] = []
        span = name_tok.span
        if self._at(TokenKind.LT):
            self._advance()
            args.append(self._type())
            while self._eat(TokenKind.COMMA):
                args.append(self._type())
            end = self._expect(TokenKind.GT, "'>'").span
            span = name_tok.span.to(end)
        return ast.TypeExpr(name_tok.text, args, span)

    def _uses_clause(self) -> list[str]:
        if not self._eat(TokenKind.KW_USES):
            return []
        self._expect(TokenKind.LBRACE, "'{'")
        effects: list[str] = []
        if not self._at(TokenKind.RBRACE):
            effects.append(self._expect(TokenKind.IDENT, "an effect name").text)
            while self._eat(TokenKind.COMMA):
                effects.append(self._expect(TokenKind.IDENT, "an effect name").text)
        self._expect(TokenKind.RBRACE, "'}'")
        return effects

    def _test(self) -> ast.TestDecl:
        start = self._advance().span  # `test`
        name = self._expect(TokenKind.STRING, "a test name string").text
        effects = self._uses_clause()
        body = self._block()
        return ast.TestDecl(name, effects, body, start.to(body.span))

    def _derive_list(self) -> list[str]:
        self._advance()  # `derive`
        self._expect(TokenKind.LPAREN, "'('")
        traits = [self._expect(TokenKind.IDENT, "a trait name").text]
        while self._eat(TokenKind.COMMA):
            traits.append(self._expect(TokenKind.IDENT, "a trait name").text)
        self._expect(TokenKind.RPAREN, "')'")
        return traits

    def _type_decl(self, derives: list[str]) -> ast.Item:
        start = self._advance().span  # `type`
        name = self._expect(TokenKind.IDENT, "a type name").text
        type_params = self._opt_type_params()
        self._expect(TokenKind.EQ, "'='")
        if self._at(TokenKind.LBRACE):
            return self._record_decl(start, name, type_params, derives)
        return self._adt_decl(start, name, type_params, derives)

    def _macro(self) -> ast.MacroDecl:
        start = self._advance().span  # `macro`
        name = self._expect(TokenKind.IDENT, "a macro name").text
        self._expect(TokenKind.LPAREN, "'('")
        params: list[str] = []
        if not self._at(TokenKind.RPAREN):
            params.append(self._expect(TokenKind.IDENT, "a macro parameter").text)
            while self._eat(TokenKind.COMMA):
                params.append(self._expect(TokenKind.IDENT, "a macro parameter").text)
        self._expect(TokenKind.RPAREN, "')'")
        self._expect(TokenKind.EQ, "'='")
        body = self._expr()
        return ast.MacroDecl(name, params, body, start.to(body.span))

    def _opt_type_params(self) -> list[str]:
        params: list[str] = []
        if self._eat(TokenKind.LT):
            params.append(self._expect(TokenKind.IDENT, "a type parameter").text)
            while self._eat(TokenKind.COMMA):
                params.append(self._expect(TokenKind.IDENT, "a type parameter").text)
            self._expect(TokenKind.GT, "'>'")
        return params

    def _record_decl(
        self, start: Span, name: str, type_params: list[str], derives: list[str]
    ) -> ast.RecordDecl:
        self._expect(TokenKind.LBRACE, "'{'")
        fields: list[ast.RecordField] = []
        while not self._at(TokenKind.RBRACE) and not self._at(TokenKind.EOF):
            fname = self._expect(TokenKind.IDENT, "a field name")
            self._expect(TokenKind.COLON, "':'")
            ftype = self._type()
            fields.append(ast.RecordField(fname.text, ftype, fname.span))
            self._eat(TokenKind.COMMA)
        end = self._expect(TokenKind.RBRACE, "'}'").span
        return ast.RecordDecl(name, type_params, fields, start.to(end), derives)

    def _adt_decl(
        self, start: Span, name: str, type_params: list[str], derives: list[str]
    ) -> ast.AdtDecl:
        variants: list[ast.Variant] = []
        self._eat(TokenKind.PIPE)  # optional leading `|`
        variants.append(self._variant())
        while self._eat(TokenKind.PIPE):
            variants.append(self._variant())
        end = variants[-1].span
        return ast.AdtDecl(name, type_params, variants, start.to(end), derives)

    def _variant(self) -> ast.Variant:
        name_tok = self._expect(TokenKind.IDENT, "a variant name")
        payload: list[ast.TypeExpr] = []
        end = name_tok.span
        if self._at(TokenKind.LPAREN):
            self._advance()
            payload.append(self._type())
            while self._eat(TokenKind.COMMA):
                payload.append(self._type())
            end = self._expect(TokenKind.RPAREN, "')'").span
        return ast.Variant(name_tok.text, payload, name_tok.span.to(end))

    # --- blocks / statements --------------------------------------------------

    def _block_or_expr_body(self) -> ast.Block:
        if self._at(TokenKind.LBRACE) and not self._record_ahead():
            return self._block()
        expr = self._expr()
        return ast.Block([ast.ExprStmt(expr, expr.span)], expr.span)

    def _record_ahead(self) -> bool:
        """At a `{`, whether it begins a record literal rather than a block."""
        nxt = self._peek_at(1)
        if nxt.kind is TokenKind.RBRACE:
            return True
        return nxt.kind is TokenKind.IDENT and self._peek_at(2).kind in (
            TokenKind.EQ,
            TokenKind.KW_WITH,
        )

    def _block(self) -> ast.Block:
        start = self._expect(TokenKind.LBRACE, "'{'").span
        stmts: list[ast.Stmt] = []
        while not self._at(TokenKind.RBRACE) and not self._at(TokenKind.EOF):
            stmts.append(self._stmt())
        end = self._expect(TokenKind.RBRACE, "'}'").span
        return ast.Block(stmts, start.to(end))

    def _stmt(self) -> ast.Stmt:
        tok = self._peek()
        if tok.kind is TokenKind.KW_LET:
            return self._let(mutable=False)
        if tok.kind is TokenKind.KW_MUT:
            return self._let(mutable=True)
        if tok.kind is TokenKind.KW_WHILE:
            return self._while()
        if tok.kind is TokenKind.KW_RETURN:
            return self._return()

        expr = self._expr()
        # Assignment to a mutable binding: `name = expr`.
        if isinstance(expr, ast.NameExpr) and self._at(TokenKind.EQ):
            self._advance()
            value = self._expr()
            return ast.AssignStmt(expr.name, value, expr.span.to(value.span))
        return ast.ExprStmt(expr, expr.span)

    def _let(self, *, mutable: bool) -> ast.Stmt:
        start = self._advance().span  # `let` / `mut`
        name = self._expect(TokenKind.IDENT, "a binding name").text
        self._expect(TokenKind.EQ, "'='")
        value = self._expr()
        span = start.to(value.span)
        if mutable:
            return ast.MutStmt(name, value, span)
        return ast.LetStmt(name, value, span)

    def _while(self) -> ast.Stmt:
        start = self._advance().span  # `while`
        cond = self._expr()
        body = self._block()
        return ast.WhileStmt(cond, body, start.to(body.span))

    def _return(self) -> ast.Stmt:
        start = self._advance().span  # `return`
        value: ast.Expr | None = None
        # Only treat what follows as the return value if it's on the same line.
        if self._peek().kind in _EXPR_START and self._peek().span.start.line == start.end.line:
            value = self._expr()
        end = value.span if value is not None else start
        return ast.ReturnStmt(value, start.to(end))

    # --- expressions (Pratt) --------------------------------------------------

    def _expr(self, min_bp: int = 0) -> ast.Expr:
        self._depth += 1
        if self._depth > _MAX_DEPTH:
            raise self._error("expression nests too deeply", self._peek().span)
        try:
            return self._expr_bp(min_bp)
        finally:
            self._depth -= 1

    def _expr_bp(self, min_bp: int) -> ast.Expr:
        lhs = self._prefix()
        while True:
            tok = self._peek()
            kind = tok.kind
            # A postfix `(` (call) or `.` (member) only continues the current
            # expression when it's on the same line, so a new statement that
            # starts with `(`/`.` is not glued onto the previous one.
            same_line = tok.span.start.line == lhs.span.end.line
            if kind is TokenKind.LPAREN and min_bp < _POSTFIX_BP and same_line:
                lhs = self._finish_call(lhs)
                continue
            if kind is TokenKind.DOT and min_bp < _POSTFIX_BP and same_line:
                self._advance()
                name_tok = self._expect(TokenKind.IDENT, "a member name")
                lhs = ast.MemberExpr(lhs, name_tok.text, lhs.span.to(name_tok.span))
                continue
            if kind is TokenKind.QUESTION and min_bp < _POSTFIX_BP:
                self._advance()
                lhs = ast.TryExpr(lhs, lhs.span.to(tok.span))
                continue
            bp = _INFIX_BP.get(kind)
            if bp is None or bp <= min_bp:
                break
            self._advance()
            rhs = self._expr(bp)
            if kind is TokenKind.PIPE_GT:
                lhs = self._desugar_pipe(lhs, rhs)
            else:
                lhs = ast.BinaryExpr(_OP_TEXT[kind], lhs, rhs, lhs.span.to(rhs.span))
        return lhs

    def _desugar_pipe(self, lhs: ast.Expr, rhs: ast.Expr) -> ast.Expr:
        span = lhs.span.to(rhs.span)
        if isinstance(rhs, ast.CallExpr):
            return ast.CallExpr(rhs.callee, [lhs, *rhs.args], span)
        return ast.CallExpr(rhs, [lhs], span)

    def _finish_call(self, callee: ast.Expr) -> ast.Expr:
        self._advance()  # `(`
        args: list[ast.Expr] = []
        if not self._at(TokenKind.RPAREN):
            args.append(self._expr())
            while self._eat(TokenKind.COMMA):
                args.append(self._expr())
        end = self._expect(TokenKind.RPAREN, "')'").span
        return ast.CallExpr(callee, args, callee.span.to(end))

    def _prefix(self) -> ast.Expr:
        tok = self._peek()
        if tok.kind is TokenKind.INT:
            self._advance()
            return ast.IntLit(int(tok.text), tok.span)
        if tok.kind is TokenKind.STRING:
            self._advance()
            return ast.StringLit(tok.text, tok.span)
        if tok.kind in (TokenKind.KW_TRUE, TokenKind.KW_FALSE):
            self._advance()
            return ast.BoolLit(tok.kind is TokenKind.KW_TRUE, tok.span)
        if tok.kind is TokenKind.IDENT:
            self._advance()
            return ast.NameExpr(tok.text, tok.span)
        if tok.kind in (TokenKind.MINUS, TokenKind.BANG):
            self._advance()
            operand = self._expr(_UNARY_BP)
            op = "-" if tok.kind is TokenKind.MINUS else "!"
            return ast.UnaryExpr(op, operand, tok.span.to(operand.span))
        if tok.kind is TokenKind.LPAREN:
            self._advance()
            inner = self._expr()
            self._expect(TokenKind.RPAREN, "')'")
            return inner
        if tok.kind is TokenKind.KW_IF:
            return self._if()
        if tok.kind is TokenKind.KW_MATCH:
            return self._match()
        if tok.kind is TokenKind.KW_REGION:
            return self._region()
        if tok.kind is TokenKind.KW_COMPTIME:
            start = self._advance().span
            body = self._block()
            return ast.ComptimeExpr(body, start.to(body.span))
        if tok.kind is TokenKind.KW_QUOTE:
            start = self._advance().span
            body = self._block()
            return ast.QuoteExpr(body, start.to(body.span))
        if tok.kind is TokenKind.KW_UNQUOTE:
            start = self._advance().span
            self._expect(TokenKind.LPAREN, "'('")
            inner = self._expr()
            end = self._expect(TokenKind.RPAREN, "')'").span
            return ast.UnquoteExpr(inner, start.to(end))
        if tok.kind is TokenKind.LBRACE:
            return self._record_expr()
        raise self._error(f"expected an expression, found {self._describe(tok)}", tok.span)

    def _record_expr(self) -> ast.Expr:
        start = self._expect(TokenKind.LBRACE, "'{'").span
        # `{ field = v, ... }` (construction) vs `{ base with field = v, ... }`.
        if self._at(TokenKind.IDENT) and self._peek_at(1).kind is TokenKind.EQ:
            fields, end = self._field_inits()
            return ast.RecordExpr(fields, start.to(end))
        if self._at(TokenKind.RBRACE):
            end = self._advance().span
            return ast.RecordExpr([], start.to(end))
        base = self._expr()
        self._expect(TokenKind.KW_WITH, "'with'")
        fields, end = self._field_inits()
        return ast.RecordUpdateExpr(base, fields, start.to(end))

    def _field_inits(self) -> tuple[list[ast.FieldInit], Span]:
        fields: list[ast.FieldInit] = []
        while not self._at(TokenKind.RBRACE) and not self._at(TokenKind.EOF):
            fname = self._expect(TokenKind.IDENT, "a field name")
            self._expect(TokenKind.EQ, "'='")
            value = self._expr()
            fields.append(ast.FieldInit(fname.text, value, fname.span.to(value.span)))
            self._eat(TokenKind.COMMA)
        end = self._expect(TokenKind.RBRACE, "'}'").span
        return fields, end

    def _match(self) -> ast.Expr:
        start = self._advance().span  # `match`
        scrutinee = self._expr()
        self._expect(TokenKind.LBRACE, "'{'")
        arms: list[ast.MatchArm] = []
        while not self._at(TokenKind.RBRACE) and not self._at(TokenKind.EOF):
            pattern = self._pattern()
            self._expect(TokenKind.FAT_ARROW, "'=>'")
            body = self._expr()
            arms.append(ast.MatchArm(pattern, body, pattern.span.to(body.span)))
        end = self._expect(TokenKind.RBRACE, "'}'").span
        return ast.MatchExpr(scrutinee, arms, start.to(end))

    def _pattern(self) -> ast.Pattern:
        tok = self._expect(TokenKind.IDENT, "a pattern")
        if tok.text == "_":
            return ast.WildcardPattern(tok.span)
        if not tok.text[0].isupper():
            return ast.BindPattern(tok.text, tok.span)
        args: list[ast.Pattern] = []
        end = tok.span
        if self._at(TokenKind.LPAREN):
            self._advance()
            args.append(self._pattern())
            while self._eat(TokenKind.COMMA):
                args.append(self._pattern())
            end = self._expect(TokenKind.RPAREN, "')'").span
        return ast.CtorPattern(tok.text, args, tok.span.to(end))

    def _region(self) -> ast.Expr:
        start = self._advance().span  # `region`
        name = self._expect(TokenKind.IDENT, "a region name").text
        body = self._block()
        return ast.RegionExpr(name, body, start.to(body.span))

    def _if(self) -> ast.Expr:
        start = self._advance().span  # `if`
        cond = self._expr()
        then_block = self._block()
        else_block: ast.Block | None = None
        end = then_block.span
        if self._eat(TokenKind.KW_ELSE):
            if self._at(TokenKind.KW_IF):
                inner = self._if()
                else_block = ast.Block([ast.ExprStmt(inner, inner.span)], inner.span)
            else:
                else_block = self._block()
            end = else_block.span
        return ast.IfExpr(cond, then_block, else_block, start.to(end))


def parse(source: str, file: str = "<input>") -> ast.Module:
    return Parser(tokenize(source, file), file).parse_module()
