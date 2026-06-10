"""Command-line entry point for the Flex compiler.

The subcommands mirror the surface defined in ``docs/MVP.md`` §18. They are
scaffolded stubs today; the pipeline (parse -> check -> ... -> run) is filled
in incrementally as the compiler is built out.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from flx import __version__
from flx.highlight import DEFAULT_STYLE, FORMATS

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

    sub.add_parser("doctor", help="Check the optional native backend toolchain (LLVM/MLIR)")

    hl = sub.add_parser("highlight", help="Syntax-highlight a .flx file")
    hl.add_argument("path", help="path to a .flx source file")
    hl.add_argument(
        "--format",
        dest="format",
        choices=list(FORMATS),
        default="auto",
        help="output format (default: auto-detect the terminal)",
    )
    hl.add_argument(
        "--style",
        default=DEFAULT_STYLE,
        help=f"Pygments style name (default: {DEFAULT_STYLE})",
    )

    return parser


def _run_highlight(path: str, fmt: str, style: str) -> int:
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"flx highlight: {exc}", file=sys.stderr)
        return 1

    from flx.highlight import render

    rendered = render(source, fmt=fmt, style=style, tty=sys.stdout.isatty())
    sys.stdout.write(rendered)
    if not rendered.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "highlight":
        return _run_highlight(args.path, args.format, args.style)

    from flx import driver

    if args.command == "doctor":
        return driver.cmd_doctor()
    if args.command == "parse":
        return driver.cmd_parse(args.path)
    if args.command == "expand":
        return driver.cmd_expand(args.path)
    if args.command == "check":
        return driver.cmd_check(args.path)
    if args.command == "emit-mlir":
        return driver.cmd_emit_mlir(args.path)
    if args.command == "run":
        return driver.cmd_run(args.path)
    if args.command == "build":
        return driver.cmd_build(args.path, args.output)
    if args.command == "test":
        if args.path is None:
            print(
                "flx test: a .flx file is required (directory discovery is not yet supported)",
                file=sys.stderr,
            )
            return 2
        return driver.cmd_test(args.path, args.filter)

    print(f"flx {args.command}: not yet implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
