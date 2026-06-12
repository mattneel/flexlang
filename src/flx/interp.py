"""A tree-walking interpreter for checked Flex programs.

This runs `flx run` / `flx test` in pure Python, with no LLVM/MLIR toolchain — so
`uvx --from flexlang flx -- test x.flx` works on a bare install. It consumes the
same monomorphized :class:`~flx.sema.check.CheckResult` the native backend does
(concrete functions, `constructors`, `method_targets`), so semantics match: the
native backend is the optimizing path, this is the portable reference path.

Value representation: I64 -> ``int`` (wrapped to 64-bit signed), Bool -> ``bool``,
Unit -> ``None``, String -> ``str``, a record -> ``dict`` (structural ``==``), and
an ADT value -> :class:`Variant`. Control flow that escapes an expression — early
``return`` and the ``?`` operator — unwinds via :class:`_Return`; an assertion or
``fail``/``panic`` in a test unwinds via :class:`_TestFail`.
"""

from __future__ import annotations

import contextlib
import ctypes
import io
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from flx.diagnostics import Span
from flx.syntax import ast
from flx.types import (
    F64,
    INT_INFO,
    STRING,
    UNIT,
    AdtType,
    FnType,
    ListType,
    MapType,
    PrimType,
    RecordType,
    Type,
    is_int_type,
)

if TYPE_CHECKING:
    from flx.sema.check import CheckResult

_I64_MASK = (1 << 64) - 1
_I64_SIGN = 1 << 63
# Each Flex call frame costs several Python frames, so this must be small enough
# that our clean "stack overflow" guard fires before Python's own recursion
# limit (raised to 20k by _ensure_recursion_headroom) would.
_DEPTH_LIMIT = 2000


def _float_binary(op: str, a: float, b: float) -> object:
    """IEEE-754 semantics, matching native arith.{addf,subf,mulf,divf,remf} and
    cmpf: division by zero yields inf/nan (Python raises, so it's emulated) and
    remainder is C fmod."""
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        if b == 0.0:
            if a == 0.0 or math.isnan(a):
                return math.nan
            return math.copysign(math.inf, a) * math.copysign(1.0, b)
        return a / b
    if op == "%":
        if b == 0.0 or math.isnan(a) or math.isinf(a):
            return math.nan
        return math.fmod(a, b)
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    raise FlexRuntimeError(f"bad float operator {op!r}")


def _f64_str(x: float) -> str:
    """Shortest %g string that round-trips, via the same 1..17 precision loop
    the native runtime runs — Python's %-formatting IS C printf, so the two
    backends produce identical text by construction. NaN is canonicalized
    ("nan", never "-nan": x86 sign-set NaNs would print signed under glibc)."""
    if x != x:
        return "nan"
    for precision in range(1, 18):
        s = "%.*g" % (precision, x)  # noqa: UP031 — C-printf parity is the point
        if float(s) == x:
            return s
    return s  # unreachable: 17 significant digits always round-trip


def _wrap(value: int) -> int:
    """Wrap a Python int into the signed 64-bit range (two's complement), matching
    the native `arith` overflow behavior."""
    value &= _I64_MASK
    return value - (1 << 64) if value & _I64_SIGN else value


def _os_error_message(exc: OSError) -> str:
    return exc.strerror or str(exc)


def _int_bounds_name(name: str) -> tuple[int, int]:
    width, signed = INT_INFO[name]
    if signed:
        return (-(1 << (width - 1)), (1 << (width - 1)) - 1)
    return (0, (1 << width) - 1)


def _logical_int_value(value: int, type_name: str | None) -> int:
    if type_name in INT_INFO and not INT_INFO[type_name][1]:
        return value & ((1 << INT_INFO[type_name][0]) - 1)
    return value


def _checked_int_convert(value: object, source_type: str | None, target_type: str) -> int:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FlexRuntimeError(f"cannot convert {_f64_str(value)} to {target_type}")
        value = int(value)
    assert isinstance(value, int)
    logical = _logical_int_value(value, source_type)
    lo, hi = _int_bounds_name(target_type)
    if not lo <= logical <= hi:
        raise FlexRuntimeError(f"value {logical} is outside {target_type} range ({lo}..{hi})")
    return logical


@dataclass(frozen=True)
class Variant:
    """A runtime ADT value: a constructor tag and its (single, optional) payload."""

    tag: str
    payload: object = None


class FlexRuntimeError(Exception):
    """A runtime fault (division by zero, recursion limit, non-exhaustive match)."""


class _Return(Exception):
    def __init__(self, value: object) -> None:
        self.value = value


class _TestFail(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


class _Env:
    """A lexical scope frame; `assign` mutates the binding wherever it was defined."""

    __slots__ = ("parent", "vars")

    def __init__(self, parent: _Env | None = None) -> None:
        self.vars: dict[str, object] = {}
        self.parent = parent

    def get(self, name: str) -> object:
        env: _Env | None = self
        while env is not None:
            if name in env.vars:
                return env.vars[name]
            env = env.parent
        raise KeyError(name)

    def has(self, name: str) -> bool:
        env: _Env | None = self
        while env is not None:
            if name in env.vars:
                return True
            env = env.parent
        return False

    def define(self, name: str, value: object) -> None:
        self.vars[name] = value

    def assign(self, name: str, value: object) -> None:
        env: _Env | None = self
        while env is not None:
            if name in env.vars:
                env.vars[name] = value
                return
            env = env.parent
        raise KeyError(name)


_PROPAGATE = {"Err", "None"}  # `?` short-circuits on these builtin variants
_UNWRAP = {"Ok", "Some"}


def _struct_eq(a: object, b: object) -> bool:
    """Structural equality with FLOAT-correct semantics. Python's container
    equality identity-shortcuts its elements, so a record holding one NaN
    object would compare equal to itself — the native field-wise cmpf says
    NaN != NaN. Recurse explicitly and compare floats by value."""
    if isinstance(a, float) or isinstance(b, float):
        return isinstance(a, float) and isinstance(b, float) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_struct_eq(v, b[k]) for k, v in a.items())
    if isinstance(a, Variant) and isinstance(b, Variant):
        return a.tag == b.tag and _struct_eq(a.payload, b.payload)
    if isinstance(a, tuple) and isinstance(b, tuple):
        return len(a) == len(b) and all(_struct_eq(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def _show_value(value: object) -> str:
    if value is None:
        return "()"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return _f64_str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "[" + ", ".join(_show_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_show_value(v)}" for k, v in value.items()) + "}"
    if isinstance(value, Variant):
        if value.payload is None:
            return value.tag
        if isinstance(value.payload, tuple):
            return value.tag + "(" + ", ".join(_show_value(v) for v in value.payload) + ")"
        return value.tag + "(" + _show_value(value.payload) + ")"
    return str(value)


def _fflush_libc() -> None:
    """Flush every C stdio stream (fflush(NULL)) so extern-call output lands
    immediately rather than at process exit."""
    try:
        libc = ctypes.CDLL(None)
        fflush = libc.fflush
        fflush.argtypes = [ctypes.c_void_p]
        fflush.restype = ctypes.c_int
        fflush(None)
    except OSError, AttributeError:  # no libc to flush (non-POSIX)
        pass


def _libc_fgetc() -> tuple[Callable[[ctypes.c_void_p], int], ctypes.c_void_p] | None:
    """libc's fgetc plus its own `FILE *stdin`. Natively, read_line (getline) and
    extern calls like getchar() share the single C stdio input buffer; reading
    through Python's sys.stdin instead would create a second, competing
    read-ahead buffer over fd 0 and the two would starve each other."""
    try:
        libc = ctypes.CDLL(None)
        fgetc = libc.fgetc
        fgetc.argtypes = [ctypes.c_void_p]
        fgetc.restype = ctypes.c_int
        for symbol in ("stdin", "__stdinp"):  # glibc / macOS
            try:
                return fgetc, ctypes.c_void_p.in_dll(libc, symbol)
            except ValueError:
                continue
    except OSError, AttributeError:
        pass
    return None


def _read_line() -> str | None:
    """One line of stdin with the trailing newline stripped, or None at end of
    input — a blank line is "" and EOF is None, distinguishably. Reads
    byte-wise through libc stdio (see _libc_fgetc); byte-lossless via
    surrogateescape, matching extern string marshalling."""
    via_libc = None
    try:
        if sys.stdin.fileno() == 0:  # pytest/StringIO stand-ins have no real fd
            via_libc = _libc_fgetc()
    except OSError, ValueError:
        pass
    if via_libc is None:
        line = sys.stdin.readline()
        if line == "":
            return None  # EOF: readline yields "" only at end of input
        line = line[:-1] if line.endswith("\n") else line
        # Truncate at an embedded NUL (strings are NUL-terminated; the native
        # runtime's strlen-based extent must match the stored length).
        return line.split("\x00", 1)[0]
    fgetc, stream = via_libc
    buf = bytearray()
    saw_input = False
    while True:
        ch = fgetc(stream)
        if ch == -1:  # EOF: a final unterminated line still returns its bytes
            break
        saw_input = True
        if ch == 0x0A:  # '\n'
            break
        buf.append(ch & 0xFF)
    if not saw_input:
        return None
    nul = buf.find(0)
    if nul >= 0:
        del buf[nul:]  # see above: the first NUL ends the string
    return buf.decode("utf-8", "surrogateescape")


def _print_raw(message: str, end: str = "\n") -> None:
    """print() with native-identical bytes: non-ASCII output (surrogate-escaped
    bytes especially) is written raw to the stdout buffer — Python's text-mode
    encoder is locale-dependent and may replace rather than raise."""
    if message.isascii() and end.isascii():
        print(message, end=end, flush=True)
        return
    if not hasattr(sys.stdout, "buffer"):
        print(message, end=end, flush=True)
        return
    sys.stdout.flush()
    raw = (message + end).encode("utf-8", "surrogateescape")
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def _print_raw_err(message: str, end: str = "\n") -> None:
    """stderr twin of _print_raw, preserving byte-for-byte output."""
    if message.isascii() and end.isascii():
        print(message, end=end, flush=True, file=sys.stderr)
        return
    sys.stderr.flush()
    raw = (message + end).encode("utf-8", "surrogateescape")
    sys.stderr.buffer.write(raw)
    sys.stderr.buffer.flush()


_strtod_fn: object = None  # lazily configured ctypes strtod, or False if unavailable

# The fallback's strtod prefix grammar: optional whitespace and sign, then a
# hex float, a decimal float, or inf/infinity/nan (case-insensitive). NO
# underscores — Python's float() accepts "1_000" but strtod stops at the '_'.
_STRTOD_PREFIX = re.compile(
    r"[ \t\n\r\f\v]*[+-]?("
    r"0[xX][0-9a-fA-F]+(\.[0-9a-fA-F]*)?([pP][+-]?[0-9]+)?"
    r"|[0-9]+(\.[0-9]*)?([eE][+-]?[0-9]+)?"
    r"|\.[0-9]+([eE][+-]?[0-9]+)?"
    r"|[iI][nN][fF]([iI][nN][iI][tT][yY])?"
    r"|[nN][aA][nN]"
    r")"
)


def _strtod(s: str) -> float:
    """C strtod of the longest valid prefix (0.0 if none) — the SAME libc call
    the native runtime makes, so the result bits match by construction (same
    rounding, same overflow-to-inf, same denormals, same locale)."""
    global _strtod_fn
    if _strtod_fn is None:
        try:
            libc = ctypes.CDLL(None)
            fn = libc.strtod
            fn.restype = ctypes.c_double
            fn.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
            _strtod_fn = fn
        except OSError, AttributeError:
            _strtod_fn = False
    if _strtod_fn is False:
        # No libc to share (non-POSIX stand-in): emulate strtod's
        # longest-valid-prefix contract, then convert with Python's parser
        # (correctly rounded for the same grammar).
        m = _STRTOD_PREFIX.match(s)
        if m is None:
            return 0.0
        text = m.group(0).strip()
        try:
            if text.lstrip("+-").lower().startswith("0x"):
                return float.fromhex(text)
            return float(text)
        except ValueError:
            return 0.0
    raw = s.encode("utf-8", "surrogateescape")
    return float(_strtod_fn(raw, None))  # type: ignore[operator]


def _checked_byte(b: object) -> int:
    """Validate a string byte for from_byte/from_bytes: 1..255 (byte 0 is the
    NUL terminator and cannot be carried). The message matches the native
    runtime byte-for-byte."""
    assert isinstance(b, int)
    if not 1 <= b <= 255:
        raise FlexRuntimeError(f"byte {b} is outside 1..255 (strings are NUL-terminated)")
    return b


def _checked_binary_byte(b: object) -> int:
    assert isinstance(b, int)
    if not 0 <= b <= 255:
        raise FlexRuntimeError(f"byte {b} is outside 0..255")
    return b


class Interpreter:
    def __init__(
        self,
        checked: CheckResult,
        max_steps: int | None = None,
        args: tuple[str, ...] = (),
    ) -> None:
        self.checked = checked
        self.args = args  # program arguments for Env.argv (user args, no argv[0])
        self.functions = {fn.name: fn for fn in checked.module.functions}
        self.types = checked.expr_types
        self.constructors = checked.constructors
        self.method_targets = checked.method_targets
        self.qualified_calls = checked.qualified_calls
        # Variants of payloadless enums lower to a scalar tag natively, so the
        # native harness reports assert failures on them with the tag index.
        self.enum_index: dict[str, int] = {}
        self.adts = {adt.name for adt in checked.module.adts}
        for adt in checked.module.adts:
            if all(not v.payload for v in adt.variants):
                for i, variant in enumerate(adt.variants):
                    self.enum_index[variant.name] = i
        self.in_test = False
        self.depth = 0
        # Optional execution budget (used when evaluating manifests, which must
        # terminate): counted per evaluated expression; None = unbounded.
        self.max_steps = max_steps
        self.steps = 0
        # C-ABI foreign functions, dispatched through ctypes against the symbols
        # already loaded in this process (libc and friends).
        self.extern_fns = checked.extern_fns
        self._extern_cache: dict[str, object] = {}

    def _module_of(self, span: Span | None, default: str) -> str:
        if span is None:
            return default
        for name, module_span in self.checked.module_spans:
            if (
                module_span.file == span.file
                and module_span.start.offset <= span.start.offset
                and span.end.offset <= module_span.end.offset
            ):
                return name
        return self.checked.file_module.get(span.file, default)

    def _show_impl_symbol(self, ty: Type) -> str | None:
        if not isinstance(ty, (PrimType, RecordType, AdtType)):
            return None
        symbol = f"t$Show$0${ty.name}$show"
        return symbol if symbol in self.functions else None

    def _show_typed(self, value: object, ty: Type) -> str:
        if is_int_type(ty) or ty in (F64, STRING, UNIT):
            return _show_value(value)
        if isinstance(ty, ListType) and isinstance(value, list):
            return "[" + ", ".join(self._show_typed(v, ty.elem) for v in value) + "]"
        if isinstance(ty, MapType) and isinstance(value, dict):
            return (
                "{"
                + ", ".join(f"{k}: {self._show_typed(v, ty.value)}" for k, v in value.items())
                + "}"
            )
        symbol = self._show_impl_symbol(ty)
        if symbol is not None:
            return str(self.call(self.functions[symbol], [value]))
        return _show_value(value)

    # --- entry points ---------------------------------------------------------

    def run_main(self) -> int:
        fn = self.functions.get("main")
        if fn is None:
            raise FlexRuntimeError("no `main` function to run")
        value = self.call(fn, [])
        return _wrap(value) & 0xFF if isinstance(value, int) and not isinstance(value, bool) else 0

    def run_tests(self, test_filter: str | None) -> int:
        tests = [
            t for t in self.checked.module.tests if test_filter is None or test_filter in t.name
        ]
        if not tests:
            print("running 0 tests\n")
            print("0 passed, 0 failed")
            return 0
        plural = "" if len(tests) == 1 else "s"
        print(f"running {len(tests)} test{plural}\n")
        default_module = self.checked.module.name
        passed = failed = 0
        for test in tests:
            # Imported tests report under their own module, not the entry's.
            module_name = self._module_of(test.span, default_module)
            self.in_test = True
            # Labels and failure reports go through _print_raw: a test name (or
            # an asserted string) can carry raw bytes, which native printf
            # emits verbatim — the interpreter must match byte-for-byte.
            try:
                self.exec_block(test.body, _Env())
                _print_raw(f"ok {module_name} / {test.name}")
                passed += 1
            except _TestFail as fail:
                if fail.reason:
                    _print_raw(fail.reason)
                _print_raw(f"fail {module_name} / {test.name}")
                failed += 1
            except _Return:
                # a `?` propagated an Err/None out of the test body: native lowers
                # this to an explicit-failure call, so match its output exactly.
                print("  explicit failure")
                _print_raw(f"fail {module_name} / {test.name}")
                failed += 1
            except FlexRuntimeError as exc:
                # A panic (index out of bounds, division by zero) fails the ONE
                # test it happened in; the rest of the suite still runs. The
                # native harness recovers identically via setjmp/longjmp.
                _print_raw(f"  runtime error: {exc}")
                _print_raw(f"fail {module_name} / {test.name}")
                failed += 1
            finally:
                self.in_test = False
        print(f"\n{passed} passed, {failed} failed", flush=True)
        return 0 if failed == 0 else 1

    def run_tests_structured(self, test_filter: str | None, fmt: str) -> int:
        results = self.test_results(test_filter)
        failed = sum(1 for r in results if r["status"] == "failed")
        if fmt == "json":
            payload = {
                "summary": {
                    "total": len(results),
                    "passed": len(results) - failed,
                    "failed": failed,
                },
                "tests": results,
            }
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        elif fmt == "junit":
            suite = ET.Element(
                "testsuite",
                {
                    "name": self.checked.module.name,
                    "tests": str(len(results)),
                    "failures": str(failed),
                },
            )
            for result in results:
                case = ET.SubElement(
                    suite,
                    "testcase",
                    {"classname": str(result["module"]), "name": str(result["name"])},
                )
                if result["output"]:
                    out = ET.SubElement(case, "system-out")
                    out.text = str(result["output"])
                if result["status"] == "failed":
                    fail = ET.SubElement(case, "failure", {"message": str(result["message"])})
                    fail.text = str(result["message"])
            print(ET.tostring(suite, encoding="unicode"))
        else:
            raise FlexRuntimeError(f"unknown test format {fmt!r}")
        return 0 if failed == 0 else 1

    def test_results(self, test_filter: str | None) -> list[dict[str, object]]:
        tests = [
            t for t in self.checked.module.tests if test_filter is None or test_filter in t.name
        ]
        default_module = self.checked.module.name
        results: list[dict[str, object]] = []
        for test in tests:
            module_name = self._module_of(test.span, default_module)
            self.in_test = True
            output = io.StringIO()
            status = "passed"
            message = ""
            try:
                with contextlib.redirect_stdout(output):
                    self.exec_block(test.body, _Env())
            except _TestFail as fail:
                status = "failed"
                message = fail.reason.strip()
            except _Return:
                status = "failed"
                message = "explicit failure"
            except FlexRuntimeError as exc:
                status = "failed"
                message = f"runtime error: {exc}"
            finally:
                self.in_test = False
            results.append(
                {
                    "module": module_name,
                    "name": test.name,
                    "label": f"{module_name} / {test.name}",
                    "status": status,
                    "message": message,
                    "output": output.getvalue(),
                }
            )
        return results

    # --- functions / blocks ---------------------------------------------------

    def call(self, fn: ast.FnDecl, args: list[object]) -> object:
        self.depth += 1
        if self.depth > _DEPTH_LIMIT:
            self.depth -= 1
            raise FlexRuntimeError("stack overflow (recursion too deep)")
        env = _Env()
        for param, arg in zip(fn.params, args, strict=True):
            env.define(param.name, arg)
        try:
            return self.exec_block(fn.body, env)
        except _Return as ret:
            return ret.value
        finally:
            self.depth -= 1

    def exec_block(self, block: ast.Block, parent: _Env) -> object:
        env = _Env(parent)
        value: object = None
        for stmt in block.stmts:
            value = self.exec_stmt(stmt, env)
        return value

    def exec_stmt(self, stmt: ast.Stmt, env: _Env) -> object:
        if isinstance(stmt, ast.ExprStmt):
            return self.eval(stmt.expr, env)
        if isinstance(stmt, (ast.LetStmt, ast.MutStmt)):
            env.define(stmt.name, self.eval(stmt.value, env))
            return None
        if isinstance(stmt, ast.AssignStmt):
            env.assign(stmt.name, self.eval(stmt.value, env))
            return None
        if isinstance(stmt, ast.IndexAssignStmt):
            xs = self.eval(stmt.obj, env)
            i = self.eval(stmt.index, env)
            assert isinstance(xs, list) and isinstance(i, int)
            if not 0 <= i < len(xs):
                raise FlexRuntimeError(f"index {i} out of bounds (len {len(xs)})")
            xs[i] = self.eval(stmt.value, env)
            return None
        if isinstance(stmt, ast.WhileStmt):
            while self.eval(stmt.cond, env):
                self.exec_block(stmt.body, env)
            return None
        if isinstance(stmt, ast.ForStmt):
            xs = self.eval(stmt.iter, env)
            assert isinstance(xs, list)
            # The length is snapshotted at loop entry (matching the native
            # lowering): elements pushed during the loop are not visited, and
            # an unconditional push can't turn the loop infinite. Element
            # reads stay live, so List.set during the loop is visible — and a
            # List.pop that SHRINKS the list mid-loop panics like the native
            # bounds check, never a raw Python IndexError.
            for i in range(len(xs)):
                if i >= len(xs):
                    raise FlexRuntimeError(f"index {i} out of bounds (len {len(xs)})")
                child = _Env(env)
                child.define(stmt.name, xs[i])
                self.exec_block(stmt.body, child)
            return None
        if isinstance(stmt, ast.ReturnStmt):
            raise _Return(self.eval(stmt.value, env) if stmt.value is not None else None)
        raise FlexRuntimeError(f"cannot interpret statement {type(stmt).__name__}")

    # --- expressions ----------------------------------------------------------

    def eval(self, expr: ast.Expr, env: _Env) -> object:
        if self.max_steps is not None:
            self.steps += 1
            if self.steps > self.max_steps:
                raise FlexRuntimeError("evaluation exceeded the step limit")
        if isinstance(expr, ast.IntLit):
            return expr.value
        if isinstance(expr, ast.FloatLit):
            return expr.value
        if isinstance(expr, ast.BoolLit):
            return expr.value
        if isinstance(expr, ast.StringLit):
            return expr.value
        if isinstance(expr, ast.BytesLit):
            return self._bytes_lit(expr, env)
        if isinstance(expr, ast.NameExpr):
            if env.has(expr.name):
                return env.get(expr.name)
            if expr.name in self.constructors:
                return Variant(expr.name)
            if expr.name in self.functions:
                # A bare (pure) function reference — the checker approved it.
                return self.functions[expr.name]
            raise FlexRuntimeError(f"unbound name {expr.name!r}")
        if isinstance(expr, ast.UnaryExpr):
            return self._unary(expr, env)
        if isinstance(expr, ast.BinaryExpr):
            return self._binary(expr, env)
        if isinstance(expr, ast.IfExpr):
            if self.eval(expr.cond, env):
                return self.exec_block(expr.then_block, env)
            return self.exec_block(expr.else_block, env) if expr.else_block is not None else None
        if isinstance(expr, ast.MemberExpr):
            # `Type.Variant` — a payloadless qualified constructor, e.g.
            # MathError.DivideByZero (the object names an ADT type, not a value).
            if (
                isinstance(expr.obj, ast.NameExpr)
                and not env.has(expr.obj.name)
                and expr.name in self.constructors
            ):
                return Variant(expr.name)
            obj = self.eval(expr.obj, env)
            if isinstance(obj, dict) and expr.name in obj:
                return obj[expr.name]
            raise FlexRuntimeError(f"no field .{expr.name}")
        if isinstance(expr, ast.CallExpr):
            return self._call(expr, env)
        if isinstance(expr, ast.UnitLit):
            return None
        if isinstance(expr, ast.ListExpr):
            return [self.eval(item, env) for item in expr.items]
        if isinstance(expr, ast.IndexExpr):
            xs = self.eval(expr.obj, env)
            i = self.eval(expr.index, env)
            assert isinstance(xs, list) and isinstance(i, int)
            if not 0 <= i < len(xs):
                raise FlexRuntimeError(f"index {i} out of bounds (len {len(xs)})")
            return xs[i]
        if isinstance(expr, ast.RecordExpr):
            return {f.name: self.eval(f.value, env) for f in expr.fields}
        if isinstance(expr, ast.RecordUpdateExpr):
            base = self.eval(expr.base, env)
            assert isinstance(base, dict)
            updated = dict(base)
            for f in expr.fields:
                updated[f.name] = self.eval(f.value, env)
            return updated
        if isinstance(expr, ast.RegionExpr):
            return self.exec_block(expr.body, env)  # regions are shallow at runtime
        if isinstance(expr, ast.BlockExpr):
            return self.exec_block(expr.body, env)
        if isinstance(expr, ast.TryExpr):
            return self._try(expr, env)
        if isinstance(expr, ast.MatchExpr):
            return self._match(expr, env)
        raise FlexRuntimeError(f"cannot interpret expression {type(expr).__name__}")

    def _unary(self, expr: ast.UnaryExpr, env: _Env) -> object:
        v = self.eval(expr.operand, env)
        if expr.op == "-":
            if isinstance(v, float):
                return -v
            assert isinstance(v, int)
            return _wrap(-v)
        if expr.op == "!":
            return not v
        raise FlexRuntimeError(f"bad unary operator {expr.op!r}")

    def _binary(self, expr: ast.BinaryExpr, env: _Env) -> object:
        op = expr.op
        if op == "&&":
            return bool(self.eval(expr.left, env)) and bool(self.eval(expr.right, env))
        if op == "||":
            return bool(self.eval(expr.left, env)) or bool(self.eval(expr.right, env))
        left = self.eval(expr.left, env)
        right = self.eval(expr.right, env)
        if op == "++":
            out = f"{left}{right}"
            if not out.isascii():
                # Re-canonicalize: adjacent surrogate-escaped bytes can complete
                # a UTF-8 sequence (from_byte(195) ++ from_byte(169) IS "é" on
                # the wire, and native strcmp says so) — equal byte strings must
                # be equal Python strings.
                out = out.encode("utf-8", "surrogateescape").decode("utf-8", "surrogateescape")
            return out
        if op == "==":
            return _struct_eq(left, right)
        if op == "!=":
            return not _struct_eq(left, right)
        if isinstance(left, float) or isinstance(right, float):
            return _float_binary(op, float(left), float(right))  # type: ignore[arg-type]
        # The remaining operators are integer arithmetic/comparison/bitwise,
        # which the checker only admits on I64 operands.
        assert isinstance(left, int) and isinstance(right, int)
        if op == "+":
            return _wrap(left + right)
        if op == "-":
            return _wrap(left - right)
        if op == "*":
            return _wrap(left * right)
        if op == "&":
            return _wrap(left & right)
        if op == "|":
            return _wrap(left | right)
        if op == "^":
            return _wrap(left ^ right)
        if op == "<<":
            # Shift counts are masked to 0..63 (wasm/Java-style), the same rule
            # the native lowering applies — no poison, no platform variance.
            return _wrap(left << (right & 63))
        if op == ">>":
            return _wrap(left >> (right & 63))  # Python >> is arithmetic
        if op in ("/", "%"):
            if right == 0:
                raise FlexRuntimeError("division by zero")
            q = abs(left) // abs(right)
            if (left < 0) != (right < 0):
                q = -q
            return _wrap(q if op == "/" else left - q * right)
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        raise FlexRuntimeError(f"bad binary operator {op!r}")

    def _try(self, expr: ast.TryExpr, env: _Env) -> object:
        value = self.eval(expr.expr, env)
        if isinstance(value, Variant):
            if value.tag in _PROPAGATE:
                raise _Return(value)
            if value.tag in _UNWRAP:
                return value.payload
        raise FlexRuntimeError("`?` applied to a non-Result/Option value")

    def _ctor_payload(self, args: list[ast.Expr], env: _Env) -> object:
        """A variant's runtime payload: None (no fields), the bare value (one
        field), or a tuple (multi-field) — mirroring the native layouts."""
        if not args:
            return None
        if len(args) == 1:
            return self.eval(args[0], env)
        return tuple(self.eval(a, env) for a in args)

    def _match(self, expr: ast.MatchExpr, env: _Env) -> object:
        value = self.eval(expr.scrutinee, env)
        for arm in expr.arms:
            bindings: dict[str, object] = {}
            if self._match_pattern(arm.pattern, value, bindings):
                child = _Env(env)
                for name, bound in bindings.items():
                    child.define(name, bound)
                return self.eval(arm.body, child)
        raise FlexRuntimeError("non-exhaustive match at runtime")

    def _match_pattern(self, pat: ast.Pattern, value: object, out: dict[str, object]) -> bool:
        if isinstance(pat, ast.WildcardPattern):
            return True
        if isinstance(pat, ast.BindPattern):
            out[pat.name] = value
            return True
        if isinstance(pat, ast.LiteralPattern):
            return value == pat.value
        if isinstance(pat, ast.CtorPattern):
            if not isinstance(value, Variant) or value.tag != pat.name:
                return False
            if not pat.args:
                return True
            if len(pat.args) == 1:
                return self._match_pattern(pat.args[0], value.payload, out)
            payload = value.payload
            assert isinstance(payload, tuple)
            return all(
                self._match_pattern(p, v, out) for p, v in zip(pat.args, payload, strict=True)
            )
        raise FlexRuntimeError(f"cannot interpret pattern {type(pat).__name__}")

    # --- calls ----------------------------------------------------------------

    def _call(self, expr: ast.CallExpr, env: _Env) -> object:
        callee = expr.callee

        qualified_symbol = self.qualified_calls.get(id(expr))
        if qualified_symbol is not None:
            fn = self.functions[qualified_symbol]
            return self.call(fn, [self.eval(a, env) for a in expr.args])

        # Statically-resolved method / generic call: the checker recorded the
        # concrete target symbol; methods pass the receiver as argument zero.
        symbol = self.method_targets.get(id(expr))
        if symbol is not None:
            fn = self.functions[symbol]
            if isinstance(callee, ast.MemberExpr):
                args = [self.eval(callee.obj, env), *(self.eval(a, env) for a in expr.args)]
            else:
                args = [self.eval(a, env) for a in expr.args]
            return self.call(fn, args)

        if isinstance(self.types.get(id(callee)), FnType):
            bound = self.eval(callee, env)
            if isinstance(bound, ast.FnDecl):
                return self.call(bound, [self.eval(a, env) for a in expr.args])
            raise FlexRuntimeError("function-typed expression did not evaluate to a function")

        if isinstance(callee, ast.MemberExpr):
            if callee.name == "eq":
                obj_value = self.eval(callee.obj, env)
                if isinstance(obj_value, (list, dict)):
                    return _struct_eq(obj_value, self.eval(expr.args[0], env))
            if callee.name == "show":
                obj_value = self.eval(callee.obj, env)
                if isinstance(obj_value, (list, dict)):
                    obj_ty = self.types.get(id(callee.obj))
                    if obj_ty is not None:
                        return self._show_typed(obj_value, obj_ty)
                    return _show_value(obj_value)
            obj = callee.obj
            # A user type or binding named Log/Str/Env/... shadows the intrinsic
            # module (the checker routes those to ctor/method paths instead).
            shadowed = isinstance(obj, ast.NameExpr) and (
                env.has(obj.name) or obj.name in self.adts
            )
            if isinstance(obj, ast.NameExpr) and obj.name == "Log" and not shadowed:
                message = str(self.eval(expr.args[0], env))
                end = "" if callee.name == "print" else "\n"  # flx_print vs flx_log
                if callee.name == "error":
                    _print_raw_err(message, end)
                    return None
                _print_raw(message, end)
                return None
            if isinstance(obj, ast.NameExpr) and obj.name == "Fs" and not shadowed:
                if callee.name == "read_line":
                    sys.stdout.flush()
                    line = _read_line()
                    return Variant("None") if line is None else Variant("Some", line)
                if callee.name == "read_text":
                    path = str(self.eval(expr.args[0], env))
                    try:
                        raw = Path(path).read_bytes()
                        if b"\x00" in raw:
                            return Variant(
                                "Err",
                                "file contains NUL byte; use a List<I64> byte buffer",
                            )
                        return Variant("Ok", raw.decode("utf-8", "surrogateescape"))
                    except OSError as exc:
                        return Variant("Err", _os_error_message(exc))
                if callee.name == "write_text":
                    path = str(self.eval(expr.args[0], env))
                    text = str(self.eval(expr.args[1], env))
                    try:
                        Path(path).write_bytes(text.encode("utf-8", "surrogateescape"))
                        return Variant("Ok", None)
                    except OSError as exc:
                        return Variant("Err", _os_error_message(exc))
                if callee.name == "append_text":
                    path = str(self.eval(expr.args[0], env))
                    text = str(self.eval(expr.args[1], env))
                    try:
                        with Path(path).open("ab") as out:
                            out.write(text.encode("utf-8", "surrogateescape"))
                        return Variant("Ok", None)
                    except OSError as exc:
                        return Variant("Err", _os_error_message(exc))
            if (
                isinstance(obj, ast.NameExpr)
                and obj.name == "Time"
                and (callee.name == "monotonic_ms")
                and not shadowed
            ):
                import time as _time

                return _wrap(_time.monotonic_ns() // 1_000_000)
            if isinstance(obj, ast.NameExpr) and obj.name == "List" and not shadowed:
                return self._list_op(callee.name, expr, env)
            if isinstance(obj, ast.NameExpr) and obj.name == "Map" and not shadowed:
                return self._map_op(callee.name, expr, env)
            if isinstance(obj, ast.NameExpr) and obj.name == "Str" and not shadowed:
                return self._str_op(callee.name, expr, env)
            if isinstance(obj, ast.NameExpr) and obj.name == "Bytes" and not shadowed:
                return self._bytes_op(callee.name, expr, env)
            if (
                isinstance(obj, ast.NameExpr)
                and obj.name == "Env"
                and callee.name == "argv"
                and not shadowed
            ):
                return list(self.args)
            if callee.name in self.constructors:  # qualified ctor, e.g. E.Code(x)
                return Variant(callee.name, self._ctor_payload(expr.args, env))
            raise FlexRuntimeError(f"cannot interpret call to .{callee.name}")

        assert isinstance(callee, ast.NameExpr)
        name = callee.name
        if env.has(name):
            # A function VALUE (a parameter or let-bound reference) shadows the
            # global namespaces — call through it.
            bound = env.get(name)
            if isinstance(bound, ast.FnDecl):
                return self.call(bound, [self.eval(a, env) for a in expr.args])
        if name in _BUILTINS:
            return self._builtin(name, expr, env)
        if name == "to_str":
            value = self.eval(expr.args[0], env)
            if isinstance(value, float):
                return _f64_str(value)
            arg_ty = self.checked.expr_types.get(id(expr.args[0]))
            ty_name = getattr(arg_ty, "name", "")
            if isinstance(value, int) and ty_name in {"U8", "U16", "U32", "U64"}:
                return str(value & ((1 << int(ty_name[1:])) - 1))
            return str(value)
        if name == "to_f64":
            value = self.eval(expr.args[0], env)
            arg_ty = self.checked.expr_types.get(id(expr.args[0]))
            ty_name = getattr(arg_ty, "name", None)
            assert isinstance(value, int)
            return float(_logical_int_value(value, ty_name))
        if name.startswith("to_") and len(name) >= 5:
            target = name[3:].upper()
            if target in INT_INFO:
                arg_ty = self.checked.expr_types.get(id(expr.args[0]))
                return _checked_int_convert(
                    self.eval(expr.args[0], env), getattr(arg_ty, "name", None), target
                )
        if name in self.constructors:
            return Variant(name, self._ctor_payload(expr.args, env))
        if name in self.extern_fns:
            return self._call_extern(name, [self.eval(a, env) for a in expr.args])
        func = self.functions.get(name)
        if func is None:
            raise FlexRuntimeError(f"call to unknown function {name!r}")
        return self.call(func, [self.eval(a, env) for a in expr.args])

    def _bytes_lit(self, expr: ast.BytesLit, env: _Env) -> list[int]:
        out: list[int] = []
        for part in expr.parts:
            if isinstance(part, ast.StringLit):
                out.extend(part.value.encode("utf-8", "surrogateescape"))
            else:
                out.append(_checked_binary_byte(self.eval(part, env)))
        return out

    def _bytes_op(self, op: str, expr: ast.CallExpr, env: _Env) -> object:
        bs = self.eval(expr.args[0], env)
        assert isinstance(bs, list)
        if op == "len":
            return len(bs)
        if op == "at":
            i = self.eval(expr.args[1], env)
            assert isinstance(i, int)
            if not 0 <= i < len(bs):
                raise FlexRuntimeError(f"index {i} out of bounds (len {len(bs)})")
            return bs[i]
        if op == "to_hex":
            return "".join(f"{_checked_binary_byte(b):02x}" for b in bs)
        raise FlexRuntimeError(f"cannot interpret Bytes.{op}")

    def _list_op(self, op: str, expr: ast.CallExpr, env: _Env) -> object:
        xs = self.eval(expr.args[0], env)
        assert isinstance(xs, list)
        if op == "len":
            return len(xs)
        if op == "push":
            xs.append(self.eval(expr.args[1], env))
            return None
        if op == "pop":
            return Variant("Some", xs.pop()) if xs else Variant("None")
        if op == "set":
            i = self.eval(expr.args[1], env)
            assert isinstance(i, int)
            if not 0 <= i < len(xs):
                raise FlexRuntimeError(f"index {i} out of bounds (len {len(xs)})")
            xs[i] = self.eval(expr.args[2], env)
            return None
        raise FlexRuntimeError(f"cannot interpret List.{op}")

    def _map_op(self, op: str, expr: ast.CallExpr, env: _Env) -> object:
        # Python dicts ARE insertion-ordered maps, matching the native
        # entries-array semantics move for move: set on a live key keeps its
        # position, remove deletes, re-insert goes to the end.
        if op == "new":
            return {}
        m = self.eval(expr.args[0], env)
        assert isinstance(m, dict)
        if op == "len":
            return len(m)
        if op == "keys":
            return list(m.keys())
        if op == "values":
            return list(m.values())
        key = self.eval(expr.args[1], env)
        assert isinstance(key, str)
        if op == "set":
            m[key] = self.eval(expr.args[2], env)
            return None
        if op == "get":
            return Variant("Some", m[key]) if key in m else Variant("None")
        if op == "has":
            return key in m
        if op == "remove":
            m.pop(key, None)
            return None
        raise FlexRuntimeError(f"cannot interpret Map.{op}")

    def _str_op(self, op: str, expr: ast.CallExpr, env: _Env) -> object:
        if op == "from_byte":
            b = self.eval(expr.args[0], env)
            assert isinstance(b, int)
            return bytes([_checked_byte(b)]).decode("utf-8", "surrogateescape")
        if op == "from_bytes":
            xs = self.eval(expr.args[0], env)
            assert isinstance(xs, list)
            raw = bytes(_checked_byte(b) for b in xs)
            # Decoding canonicalizes: completed UTF-8 sequences become their
            # characters, stray bytes stay surrogates — same form as literals.
            return raw.decode("utf-8", "surrogateescape")
        if op == "parse_f64":
            return _strtod(str(self.eval(expr.args[0], env)))
        if op == "to_str_fixed":
            x = self.eval(expr.args[0], env)
            d = self.eval(expr.args[1], env)
            assert isinstance(x, float) and isinstance(d, int)
            if not 0 <= d <= 100:
                raise FlexRuntimeError(f"decimals {d} is outside 0..100")
            if math.isnan(x):
                return "nan"  # never "-nan"; matches to_str's canonical form
            return f"{x:.{d}f}"
        if op == "to_hex":
            n = self.eval(expr.args[0], env)
            assert isinstance(n, int)
            return format(n & _I64_MASK, "x")
        if op == "to_unsigned":
            n = self.eval(expr.args[0], env)
            assert isinstance(n, int)
            return str(n & _I64_MASK)
        # BYTE semantics, matching the native runtime exactly: index the UTF-8
        # bytes (surrogateescape keeps split sequences lossless).
        data = str(self.eval(expr.args[0], env)).encode("utf-8", "surrogateescape")
        if op == "byte_at":
            i = self.eval(expr.args[1], env)
            assert isinstance(i, int)
            if not 0 <= i < len(data):
                raise FlexRuntimeError(f"index {i} out of bounds (len {len(data)})")
            return data[i]
        if op == "substr":
            start = self.eval(expr.args[1], env)
            count = self.eval(expr.args[2], env)
            assert isinstance(start, int) and isinstance(count, int)
            start = min(max(start, 0), len(data))
            end = min(start + max(count, 0), len(data))
            return data[start:end].decode("utf-8", "surrogateescape")
        raise FlexRuntimeError(f"cannot interpret Str.{op}")

    def _call_extern(self, name: str, args: list[object]) -> object:
        param_kinds, ret_kind = self.checked.extern_abi[name]
        ctype_of = {
            "i64": ctypes.c_longlong,
            "i32": ctypes.c_int,
            "str": ctypes.c_char_p,
            "f64": ctypes.c_double,
        }
        cfn = self._extern_cache.get(name)
        if cfn is None:
            try:
                cfn = getattr(ctypes.CDLL(None), name)
            except OSError, AttributeError:
                raise FlexRuntimeError(
                    f"extern symbol {name!r} not found in this process"
                ) from None
            cfn.argtypes = [ctype_of[k] for k in param_kinds]
            cfn.restype = None if ret_kind == "unit" else ctype_of[ret_kind]
            self._extern_cache[name] = cfn
        cargs: list[object] = []
        for v, kind in zip(args, param_kinds, strict=True):
            if kind == "str":
                # surrogateescape round-trips arbitrary bytes that earlier came
                # back from C, so what we hand to C is byte-identical to native.
                cargs.append(str(v).encode("utf-8", "surrogateescape"))
            elif kind == "f64":
                assert isinstance(v, float)
                cargs.append(v)
            else:
                assert isinstance(v, int)
                wrapped = _wrap(v)
                if kind == "i32":  # truncate exactly as native arith.trunci does
                    wrapped = ((wrapped + (1 << 31)) & 0xFFFFFFFF) - (1 << 31)
                cargs.append(wrapped)
        # Python and libc buffer stdout independently inside this one process:
        # flush ours before the call and libc's after, so interleaved output
        # appears in call order — exactly as it would from a native binary.
        sys.stdout.flush()
        try:
            result = cfn(*cargs)  # type: ignore[operator]
        except (ctypes.ArgumentError, OSError) as exc:
            raise FlexRuntimeError(f"extern call {name!r} failed: {exc}") from None
        finally:
            _fflush_libc()
        if ret_kind == "str":
            # A NULL char* comes back as the empty string (no null pointers in
            # Flex); non-UTF-8 bytes are preserved losslessly via surrogateescape
            # so length and round-trips match the native backend byte-for-byte.
            return result.decode("utf-8", "surrogateescape") if result is not None else ""
        if ret_kind == "unit":
            return None
        if ret_kind == "f64":
            assert isinstance(result, float)
            return result
        # i32 results arrive sign-extended by ctypes (c_int), matching native extsi.
        assert isinstance(result, int)
        return _wrap(result)

    def _builtin(self, name: str, expr: ast.CallExpr, env: _Env) -> object:
        if name == "assert":
            if not self.eval(expr.args[0], env):
                raise _TestFail("  assertion failed")
            return None
        if name in ("assert_eq", "assert_ne"):
            a = self.eval(expr.args[0], env)
            b = self.eval(expr.args[1], env)
            symbol = self.checked.assert_impls.get(id(expr))
            if symbol is not None:
                # The checker routed this assertion through an Eq impl (a type
                # that isn't structurally comparable); both backends dispatch it.
                fn = self.functions[symbol]
                equal = bool(self.call(fn, [a, b]))
            else:
                equal = _struct_eq(a, b)
            if name == "assert_eq" and not equal:
                raise _TestFail(self._eq_reason("assert_eq", a, b))
            if name == "assert_ne" and equal:
                raise _TestFail(self._eq_reason("assert_ne", a, b))
            return None
        # fail(msg) / panic(msg)
        message = self.eval(expr.args[0], env) if expr.args else None
        raise _TestFail(f"  {message}" if message is not None else "  explicit failure")

    def _scalar(self, value: object) -> int | str | None:
        """The i64 the native harness would compare for an assert failure, or None
        for an aggregate (record / ADT-with-payload) it reports generically."""
        if value is None:
            return 0  # unit materializes as i64 0 natively
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, float):
            return _f64_str(value)
        if isinstance(value, int):
            return value
        if isinstance(value, Variant) and value.payload is None and value.tag in self.enum_index:
            return self.enum_index[value.tag]
        return None

    def _eq_reason(self, kind: str, a: object, b: object) -> str:
        if isinstance(a, str) and isinstance(b, str):
            # Mirrors the native string reporters exactly.
            if kind == "assert_eq":
                return f'  assert_eq failed: actual "{a}", expected "{b}"'
            return f'  assert_ne failed: both are "{a}"'
        sa, sb = self._scalar(a), self._scalar(b)
        if sa is None or sb is None:
            return "  assertion failed"
        if kind == "assert_eq":
            return f"  assert_eq failed: actual {sa}, expected {sb}"
        return f"  assert_ne failed: both are {sa}"


_BUILTINS = {"assert", "assert_eq", "assert_ne", "fail", "panic"}


def _ensure_recursion_headroom() -> None:
    # The interpreter recurses through Python frames; give deep-but-bounded Flex
    # recursion room before our own _DEPTH_LIMIT guard trips with a clean error.
    if sys.getrecursionlimit() < 20_000:
        sys.setrecursionlimit(20_000)


def run_main(checked: CheckResult, args: tuple[str, ...] = ()) -> int:
    _ensure_recursion_headroom()
    try:
        return Interpreter(checked, args=args).run_main()
    except RecursionError:
        # Deep non-call nesting can blow Python's stack before our own call-depth
        # guard fires; surface it as the same clean runtime error either way.
        raise FlexRuntimeError("stack overflow (recursion too deep)") from None


def run_tests(checked: CheckResult, test_filter: str | None = None) -> int:
    _ensure_recursion_headroom()
    try:
        return Interpreter(checked).run_tests(test_filter)
    except RecursionError:
        raise FlexRuntimeError("stack overflow (recursion too deep)") from None


def run_tests_structured(checked: CheckResult, test_filter: str | None, fmt: str) -> int:
    _ensure_recursion_headroom()
    try:
        return Interpreter(checked).run_tests_structured(test_filter, fmt)
    except RecursionError:
        raise FlexRuntimeError("stack overflow (recursion too deep)") from None
