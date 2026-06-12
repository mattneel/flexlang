"""Flex — a native functional systems language (prototype compiler)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("flexlang")
except PackageNotFoundError:
    __version__ = "0+unknown"
