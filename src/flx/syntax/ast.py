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
class IndexExpr(Expr):
    """`xs[i]` — list element access (panics on an out-of-bounds index)."""

    obj: Expr
    index: Expr
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
class UnitLit(Expr):
    """`()` — the unit value."""

    span: Span


@dataclass(frozen=True)
class ListExpr(Expr):
    items: list[Expr]
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


@dataclass(frozen=True)
class UnquoteSpliceExpr(Expr):
    """`unquote_splice(e)` — splice a comptime list of fragments into a quote."""

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
class LiteralPattern(Pattern):
    """An I64 or Bool literal in pattern position, e.g. `Some(0)`."""

    value: int | bool
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


@dataclass(frozen=True)
class BlockExpr(Expr):
    """A `{ ... }` block in expression position (match arm bodies); its value is
    the trailing expression."""

    body: Block
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
    annotation: TypeExpr | None = None  # `let x: T = ...`


@dataclass(frozen=True)
class MutStmt(Stmt):
    name: str
    value: Expr
    span: Span
    annotation: TypeExpr | None = None  # `mut x: T = ...`


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
class ForStmt(Stmt):
    """`for name in iter { body }` — iterates a List at runtime (and comptime
    fragments during macro expansion)."""

    name: str
    iter: Expr
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
class TypeParam:
    name: str
    bounds: list[str]  # trait names this parameter must satisfy
    span: Span


@dataclass(frozen=True)
class FnDecl(Item):
    name: str
    params: list[Param]
    return_type: TypeExpr | None
    effects: list[str]
    body: Block
    span: Span
    type_params: list[TypeParam] = field(default_factory=list)
    pub: bool = False


@dataclass(frozen=True)
class ExternFnDecl(Item):
    """A C-ABI foreign function: `extern fn strlen(s: String) -> I64 uses { ... }`.

    A trust declaration — the author asserts the C symbol's signature and its
    effects; the effect system propagates them to callers like any other call."""

    name: str
    params: list[Param]
    return_type: TypeExpr | None
    effects: list[str]
    span: Span
    pub: bool = False


@dataclass(frozen=True)
class TargetDecl(Item):
    """A `build.flx` target: an effect-checked, runnable unit of build logic.
    `target name uses { Fs, Process } { body }`."""

    name: str
    effects: list[str]
    body: Block
    span: Span


@dataclass(frozen=True)
class DefaultTargetDecl(Item):
    """`target default = name` — which target `flx build` runs bare."""

    name: str
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
    pub: bool = False


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
    pub: bool = False


@dataclass(frozen=True)
class MacroDecl(Item):
    name: str
    params: list[str]
    body: Expr
    span: Span


@dataclass(frozen=True)
class TraitMethod:
    name: str
    params: list[Param]
    return_type: TypeExpr | None
    span: Span


@dataclass(frozen=True)
class TraitDecl(Item):
    name: str
    methods: list[TraitMethod]
    span: Span
    pub: bool = False


@dataclass(frozen=True)
class ImplDecl(Item):
    trait: str
    type_name: str
    methods: list[FnDecl]
    span: Span


@dataclass(frozen=True)
class Module:
    name: str
    imports: list[str]
    items: list[Item]
    span: Span
    # span of each `import` statement, parallel to `imports` (for diagnostics)
    import_spans: list[Span] = field(default_factory=list)

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

    @property
    def traits(self) -> list[TraitDecl]:
        return [it for it in self.items if isinstance(it, TraitDecl)]

    @property
    def impls(self) -> list[ImplDecl]:
        return [it for it in self.items if isinstance(it, ImplDecl)]

    @property
    def targets(self) -> list[TargetDecl]:
        return [it for it in self.items if isinstance(it, TargetDecl)]

    @property
    def externs(self) -> list[ExternFnDecl]:
        return [it for it in self.items if isinstance(it, ExternFnDecl)]

    @property
    def default_target(self) -> str | None:
        for it in self.items:
            if isinstance(it, DefaultTargetDecl):
                return it.name
        return None
