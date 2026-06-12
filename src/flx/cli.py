"""Command-line entry point for the Flex compiler.

The subcommands mirror the surface defined in ``docs/MVP.md`` §18. They are
scaffolded stubs today; the pipeline (parse -> check -> ... -> run) is filled
in incrementally as the compiler is built out.
"""

from __future__ import annotations

import argparse
import os
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
        if name in ("check", "run", "build"):
            cmd.add_argument(
                "path",
                nargs="?",
                help="path to a .flx source file (default: the package.flx entry in cwd)",
            )
        else:
            cmd.add_argument("path", help="path to a .flx source file")
        if name == "build":
            cmd.add_argument("-o", "--output", help="output executable path")
            cmd.add_argument(
                "--explain",
                action="store_true",
                help="list build.flx targets and their declared effects",
            )
        if name == "run":
            cmd.add_argument(
                "--native",
                action="store_true",
                help="compile and run through the native LLVM backend (needs MLIR/LLVM 22)",
            )
            cmd.add_argument(
                "--quiet-status",
                action="store_true",
                help="suppress the flx status line for nonzero program exits",
            )
            cmd.add_argument(
                "args",
                nargs=argparse.REMAINDER,
                help="arguments for the program (Env.argv); put flx flags before the file",
            )
            cmd.add_argument(
                "--interpret",
                action="store_true",
                help="force the pure-Python interpreter (the default backend)",
            )

    test_cmd = sub.add_parser("test", help="Discover, compile, and run tests")
    test_cmd.add_argument("path", nargs="?", help="optional .flx file or directory")
    test_cmd.add_argument("--filter", dest="filter", help="only run tests matching a substring")
    test_cmd.add_argument(
        "--docs",
        action="store_true",
        help="also run the examples nested in this file's doc declarations",
    )
    test_cmd.add_argument(
        "--native",
        action="store_true",
        help="run tests through the native LLVM backend (needs MLIR/LLVM 22)",
    )
    test_cmd.add_argument(
        "--interpret",
        action="store_true",
        help="force the pure-Python interpreter (the default backend)",
    )
    test_cmd.add_argument(
        "--format",
        choices=["pretty", "json", "junit"],
        default="pretty",
        help="test output format",
    )

    sub.add_parser("doctor", help="Check the optional native backend toolchain (LLVM/MLIR)")

    deps_cmd = sub.add_parser("deps", help="Lock, vendor, or verify package dependencies")
    deps_sub = deps_cmd.add_subparsers(dest="deps_command", metavar="<deps-command>")
    for name, help_text in {
        "lock": "Write flex.lock with content hashes for path dependencies",
        "vendor": "Copy locked path dependencies into vendor/ and write flex.lock",
        "verify": "Verify dependencies against flex.lock",
    }.items():
        cmd = deps_sub.add_parser(name, help=help_text)
        cmd.add_argument(
            "path",
            nargs="?",
            help="optional package directory or package.flx (default: current package)",
        )

    release_cmd = sub.add_parser("release", help="Release maintenance commands")
    release_sub = release_cmd.add_subparsers(dest="release_command", metavar="<release-command>")
    preflight = release_sub.add_parser("preflight", help="Check release readiness before publish")
    preflight.add_argument(
        "--allow-dirty",
        action="store_true",
        help="run artifact checks even when the git working tree is dirty",
    )

    docs_cmd = sub.add_parser("docs", help="Check, build, or explain the documentation")
    docs_sub = docs_cmd.add_subparsers(dest="docs_command", metavar="<docs-command>")
    docs_check = docs_sub.add_parser(
        "check", help="Prove the docs: run every example, verify every expected error"
    )
    docs_check.add_argument(
        "path",
        nargs="?",
        help="optional .flx file or directory to check instead of bundled compiler docs",
    )
    docs_check.add_argument(
        "--both",
        action="store_true",
        help="run doc examples on the native backend as well as the interpreter",
    )
    docs_build = docs_sub.add_parser(
        "build", help="Render doc declarations into the book (then mdbook build)"
    )
    docs_build.add_argument(
        "path",
        nargs="?",
        help="optional .flx file or directory to render instead of bundled compiler docs",
    )
    docs_build.add_argument(
        "--output",
        help="output docs directory for local docs build (default: docs)",
    )
    docs_build.add_argument(
        "--check",
        action="store_true",
        help="verify the committed generated pages are current (CI gate)",
    )
    docs_explain = docs_sub.add_parser("explain", help="Explain a diagnostic code")
    docs_explain.add_argument("code", help="a diagnostic code, e.g. EFFECT001")

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
    if not __debug__:
        print(
            "flx: Python optimized mode (-O) is not supported; "
            "compiler assertions guard internal invariants",
            file=sys.stderr,
        )
        return 1
    try:
        code = _dispatch(argv)
        # Flush HERE so a broken pipe surfaces as BrokenPipeError inside this
        # try — interpreter-shutdown flushes happen after any handler could
        # run and would die as an unhandled "Exception ignored" + exit 120.
        sys.stdout.flush()
        sys.stderr.flush()
        return code
    except BrokenPipeError:
        # A reader went away (`flx ... | head`). A native binary dies of
        # SIGPIPE and reports 128+13; every flx command matches it. Point the
        # standard streams at the void so interpreter shutdown's buffered
        # flush doesn't print a second traceback.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.dup2(devnull, sys.stderr.fileno())
        return 141


def _dispatch(argv: Sequence[str] | None = None) -> int:
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
    if args.command == "deps":
        from flx import package as pkg

        if args.deps_command == "lock":
            return pkg.cmd_deps_lock(args.path)
        if args.deps_command == "vendor":
            return pkg.cmd_deps_vendor(args.path)
        if args.deps_command == "verify":
            return pkg.cmd_deps_verify(args.path)
        print("usage: flx deps <lock|vendor|verify>", file=sys.stderr)
        return 2
    if args.command == "release":
        from flx import release

        if args.release_command == "preflight":
            return release.cmd_preflight(allow_dirty=args.allow_dirty)
        print("usage: flx release <preflight>", file=sys.stderr)
        return 2
    if args.command == "docs":
        from flx import docsengine

        if args.docs_command == "check":
            return docsengine.cmd_docs_check(native=args.both, path=args.path)
        if args.docs_command == "build":
            return docsengine.cmd_docs_build(
                check_only=args.check,
                source_path=args.path,
                docs_dir=Path(args.output) if args.output else None,
            )
        if args.docs_command == "explain":
            return docsengine.cmd_docs_explain(args.code)
        print("usage: flx docs <check|build|explain>", file=sys.stderr)
        return 2
    if args.command == "parse":
        return driver.cmd_parse(args.path)
    if args.command == "expand":
        return driver.cmd_expand(args.path)
    if args.command == "check":
        return driver.cmd_check(args.path)
    if args.command == "emit-mlir":
        return driver.cmd_emit_mlir(args.path)
    if args.command == "run":
        # The CLI is interpreter-first: default to the interpreter unless --native.
        prog_args = list(getattr(args, "args", []) or [])
        if prog_args and prog_args[0] == "--":
            prog_args = prog_args[1:]  # `flx run f.flx -- --flag` passes --flag through
        return driver.cmd_run(
            args.path,
            interpret=not args.native,
            native=args.native,
            args=tuple(prog_args),
            announce=not args.quiet_status,
        )
    if args.command == "build":
        # An explicit .flx file (or path) compiles a native executable. A bare
        # word is a target name in ./build.flx: `flx build [target] [--explain]`,
        # falling back to a native build of the package entry when there is no
        # build.flx. A file in the cwd that merely shares a target's name does
        # not hijack the target.
        looks_like_path = args.path is not None and (
            args.path.endswith(".flx") or os.sep in args.path
        )
        if looks_like_path:
            return driver.cmd_build(args.path, args.output)
        from flx import build as build_runner

        if args.path is None and not args.explain and not Path("build.flx").is_file():
            return driver.cmd_build(None, args.output)
        return build_runner.run_build(args.path, args.explain)
    if args.command == "test":
        if args.native and args.format != "pretty":
            print(
                f"flx test --format {args.format}: only the interpreter supports structured output",
                file=sys.stderr,
            )
            return 2
        code = driver.cmd_test(
            args.path,
            args.filter,
            interpret=not args.native,
            native=args.native,
            fmt=args.format,
        )
        if getattr(args, "docs", False) and args.path:
            from flx import docsengine

            docs_code = docsengine.run_file_docs(args.path, native=args.native)
            code = code or docs_code
        return code

    print(f"flx {args.command}: not yet implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
