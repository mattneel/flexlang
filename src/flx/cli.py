"""Command-line entry point for the Flex compiler.

The subcommands mirror the surface defined in ``docs/MVP.md`` §18. They are
scaffolded stubs today; the pipeline (parse -> check -> ... -> run) is filled
in incrementally as the compiler is built out.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from flx import __version__

# Single-file commands: each takes a path to a `.flx` source file.
_FILE_COMMANDS: dict[str, str] = {
    "parse": "Parse a .flx file and print the AST",
    "check": "Parse, resolve, typecheck, effect-check, and region-check",
    "emit-hir": "Emit typed HIR",
    "emit-mir": "Emit MIR",
    "emit-mlir": "Emit MLIR text",
    "run": "Compile and run",
    "build": "Build a native executable",
    "expand": "Show macro/desugar-expanded source",
    "explain-effects": "Explain function and test effects",
    "explain-cost": "Explain allocation/cost behavior",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flx",
        description="Flex compiler — a native functional systems language.",
    )
    parser.add_argument("--version", action="version", version=f"flx {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    for name, help_text in _FILE_COMMANDS.items():
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("path", help="path to a .flx source file")
        if name == "build":
            cmd.add_argument("-o", "--output", help="output executable path")

    test_cmd = sub.add_parser("test", help="Discover, compile, and run tests")
    test_cmd.add_argument("path", nargs="?", help="optional .flx file or directory")
    test_cmd.add_argument("--filter", dest="filter", help="only run tests matching a substring")
    test_cmd.add_argument(
        "--format",
        choices=["pretty", "json", "junit"],
        default="pretty",
        help="test output format",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    print(f"flx {args.command}: not yet implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
