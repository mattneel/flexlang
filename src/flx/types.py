"""The MVP type system: primitives, function types, and an error sentinel."""

from __future__ import annotations

from dataclasses import dataclass


class Type:
    """Base class for Flex types."""


@dataclass(frozen=True)
class PrimType(Type):
    name: str

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class FnType(Type):
    params: tuple[Type, ...]
    ret: Type

    def __str__(self) -> str:
        args = ", ".join(str(p) for p in self.params)
        return f"({args}) -> {self.ret}"


@dataclass(frozen=True, eq=False)
class RecordType(Type):
    """A named record. Identity is NOMINAL (the name): record names are unique
    per program, the checker keeps one object per name (created field-less and
    settled in place so fields may reference later declarations), and a stable
    hash must not depend on the fields settling."""

    name: str
    fields: tuple[tuple[str, Type], ...]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RecordType):
            return NotImplemented
        return self.name == other.name

    def __hash__(self) -> int:
        return hash(self.name)

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class VariantDef:
    name: str
    payload: tuple[Type, ...]


@dataclass(frozen=True, eq=False)
class AdtType(Type):
    """A monomorphic ADT instantiation (e.g. Result<I64, MathError>).

    Identity is NOMINAL — `name` plus `type_args` — never the variants. ADTs may
    be recursive (a variant payload can contain the ADT itself), so structural
    comparison or hashing through `variants` would not terminate. Type names are
    unique per program (duplicates are TYPE002), so nominal equality is exact.
    The checker's instantiation cache hands out one object per (name, type_args),
    created with empty variants and settled in place to tie recursive knots."""

    name: str
    variants: tuple[VariantDef, ...]
    type_args: tuple[Type, ...] = ()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AdtType):
            return NotImplemented
        return self.name == other.name and self.type_args == other.type_args

    def __hash__(self) -> int:
        return hash((self.name, self.type_args))

    def __str__(self) -> str:
        if self.type_args:
            return f"{self.name}<{', '.join(str(a) for a in self.type_args)}>"
        return self.name


@dataclass(frozen=True)
class ListType(Type):
    """A homogeneous list, `List<T>` — a heap container with reference
    semantics, lowered on both backends."""

    elem: Type

    def __str__(self) -> str:
        return f"List<{self.elem}>"


@dataclass(frozen=True)
class MapType(Type):
    """An insertion-ordered map with String keys, `Map<String, V>` — keys are
    String for now, so only the value type varies. Reference semantics, like
    List. Structural equality on the value type (like ListType)."""

    value: Type

    def __str__(self) -> str:
        return f"Map<String, {self.value}>"


@dataclass(frozen=True)
class ErrorType(Type):
    """Placeholder produced after a type error, to suppress cascades."""

    def __str__(self) -> str:
        return "<error>"


I64 = PrimType("I64")
F64 = PrimType("F64")
BOOL = PrimType("Bool")
UNIT = PrimType("Unit")
STRING = PrimType("String")
REGION = PrimType("Region")
ERROR = ErrorType()

# Type names usable in annotations for the MVP.
PRIMITIVES: dict[str, Type] = {
    "I64": I64,
    "F64": F64,
    "Bool": BOOL,
    "Unit": UNIT,
    "String": STRING,
    "Region": REGION,
}
