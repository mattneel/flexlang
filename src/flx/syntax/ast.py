"""The Flex abstract syntax tree.

Nodes are immutable and carry source spans for diagnostics. Types are computed
separately by the checker (see :mod:`flx.sema.check`), keyed by node identity,
so the AST stays pure syntax.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flx.diagnostics import Span

# --- type annotations ---------------------------------------------------------


@dataclass(frozen=True)
class TypeExpr:
    name: str
    args: list[TypeExpr] = field(default_factory=list)
    span: Span | None = None


# --- expressions --------------------------------------------------------------


class Expr:
    """Marker base for expressions."""

    span: Span


@dataclass(frozen=True)
class IntLit(Expr):
    value: int
    span: Span


@dataclass(frozen=True)
class BoolLit(Expr):
    value: bool
    span: Span


@dataclass(frozen=True)
class StringLit(Expr):
    value: str
    span: Span


@dataclass(frozen=True)
class NameExpr(Expr):
    name: str
    span: Span


@dataclass(frozen=True)
class MemberExpr(Expr):
    obj: Expr
    name: str
    span: Span


@dataclass(frozen=True)
class UnaryExpr(Expr):
    op: str
    operand: Expr
    span: Span


@dataclass(frozen=True)
class BinaryExpr(Expr):
    op: str
    left: Expr
    right: Expr
    span: Span


@dataclass(frozen=True)
class CallExpr(Expr):
    callee: Expr
    args: list[Expr]
    span: Span


@dataclass(frozen=True)
class IfExpr(Expr):
    cond: Expr
    then_block: Block
    else_block: Block | None
    span: Span


@dataclass(frozen=True)
class FieldInit:
    name: str
    value: Expr
    span: Span


@dataclass(frozen=True)
class RecordExpr(Expr):
    fields: list[FieldInit]
    span: Span


@dataclass(frozen=True)
class RecordUpdateExpr(Expr):
    base: Expr
    fields: list[FieldInit]
    span: Span


@dataclass(frozen=True)
class RegionExpr(Expr):
    name: str
    body: Block
    span: Span


@dataclass(frozen=True)
class TryExpr(Expr):
    """The `?` result-propagation operator."""

    expr: Expr
    span: Span


@dataclass(frozen=True)
class ComptimeExpr(Expr):
    """`comptime { ... }` — evaluated at compile time, folded to a literal."""

    body: Block
    span: Span


@dataclass(frozen=True)
class QuoteExpr(Expr):
    """`quote { ... }` — a templated AST value (used in macro bodies)."""

    body: Block
    span: Span


@dataclass(frozen=True)
class UnquoteExpr(Expr):
    """`unquote(e)` — splice the AST value of `e` into a surrounding quote."""

    expr: Expr
    span: Span


# --- patterns -----------------------------------------------------------------


class Pattern:
    """Marker base for match patterns."""

    span: Span


@dataclass(frozen=True)
class WildcardPattern(Pattern):
    span: Span


@dataclass(frozen=True)
class BindPattern(Pattern):
    name: str
    span: Span


@dataclass(frozen=True)
class CtorPattern(Pattern):
    name: str
    args: list[Pattern]
    span: Span


@dataclass(frozen=True)
class MatchArm:
    pattern: Pattern
    body: Expr
    span: Span


@dataclass(frozen=True)
class MatchExpr(Expr):
    scrutinee: Expr
    arms: list[MatchArm]
    span: Span


# --- statements ---------------------------------------------------------------


class Stmt:
    """Marker base for statements."""

    span: Span


@dataclass(frozen=True)
class LetStmt(Stmt):
    name: str
    value: Expr
    span: Span


@dataclass(frozen=True)
class MutStmt(Stmt):
    name: str
    value: Expr
    span: Span


@dataclass(frozen=True)
class AssignStmt(Stmt):
    name: str
    value: Expr
    span: Span


@dataclass(frozen=True)
class WhileStmt(Stmt):
    cond: Expr
    body: Block
    span: Span


@dataclass(frozen=True)
class ReturnStmt(Stmt):
    value: Expr | None
    span: Span


@dataclass(frozen=True)
class ExprStmt(Stmt):
    expr: Expr
    span: Span


@dataclass(frozen=True)
class Block:
    stmts: list[Stmt]
    span: Span

    @property
    def tail(self) -> Expr | None:
        """The trailing expression that gives the block its value, if any."""
        if self.stmts and isinstance(self.stmts[-1], ExprStmt):
            return self.stmts[-1].expr
        return None


# --- declarations -------------------------------------------------------------


@dataclass(frozen=True)
class Param:
    name: str
    type: TypeExpr
    span: Span


class Item:
    """Marker base for top-level items."""

    span: Span


@dataclass(frozen=True)
class FnDecl(Item):
    name: str
    params: list[Param]
    return_type: TypeExpr | None
    effects: list[str]
    body: Block
    span: Span


@dataclass(frozen=True)
class TestDecl(Item):
    name: str
    effects: list[str]
    body: Block
    span: Span


@dataclass(frozen=True)
class RecordField:
    name: str
    type: TypeExpr
    span: Span


@dataclass(frozen=True)
class RecordDecl(Item):
    name: str
    type_params: list[str]
    fields: list[RecordField]
    span: Span
    derives: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Variant:
    name: str
    payload: list[TypeExpr]
    span: Span


@dataclass(frozen=True)
class AdtDecl(Item):
    name: str
    type_params: list[str]
    variants: list[Variant]
    span: Span
    derives: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MacroDecl(Item):
    name: str
    params: list[str]
    body: Expr
    span: Span


@dataclass(frozen=True)
class Module:
    name: str
    imports: list[str]
    items: list[Item]
    span: Span

    @property
    def functions(self) -> list[FnDecl]:
        return [it for it in self.items if isinstance(it, FnDecl)]

    @property
    def tests(self) -> list[TestDecl]:
        return [it for it in self.items if isinstance(it, TestDecl)]

    @property
    def records(self) -> list[RecordDecl]:
        return [it for it in self.items if isinstance(it, RecordDecl)]

    @property
    def adts(self) -> list[AdtDecl]:
        return [it for it in self.items if isinstance(it, AdtDecl)]

    @property
    def macros(self) -> list[MacroDecl]:
        return [it for it in self.items if isinstance(it, MacroDecl)]
