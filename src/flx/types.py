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


@dataclass(frozen=True)
class ErrorType(Type):
    """Placeholder produced after a type error, to suppress cascades."""

    def __str__(self) -> str:
        return "<error>"


I64 = PrimType("I64")
BOOL = PrimType("Bool")
UNIT = PrimType("Unit")
STRING = PrimType("String")
REGION = PrimType("Region")
ERROR = ErrorType()

# Type names usable in annotations for the MVP.
PRIMITIVES: dict[str, Type] = {
    "I64": I64,
    "Bool": BOOL,
    "Unit": UNIT,
    "String": STRING,
    "Region": REGION,
}
