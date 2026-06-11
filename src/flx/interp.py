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

import ctypes
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from flx.syntax import ast

if TYPE_CHECKING:
    from flx.sema.check import CheckResult

_I64_MASK = (1 << 64) - 1
_I64_SIGN = 1 << 63
# Each Flex call frame costs several Python frames, so this must be small enough
# that our clean "stack overflow" guard fires before Python's own recursion
# limit (raised to 20k by _ensure_recursion_headroom) would.
_DEPTH_LIMIT = 2000


def _wrap(value: int) -> int:
    """Wrap a Python int into the signed 64-bit range (two's complement), matching
    the native `arith` overflow behavior."""
    value &= _I64_MASK
    return value - (1 << 64) if value & _I64_SIGN else value


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


def _read_line() -> str:
    """One line of stdin with the trailing newline stripped; "" at EOF. Reads
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
        return line[:-1] if line.endswith("\n") else line
    fgetc, stream = via_libc
    buf = bytearray()
    while True:
        ch = fgetc(stream)
        if ch in (-1, 0x0A):  # EOF / '\n'
            break
        buf.append(ch & 0xFF)
    return buf.decode("utf-8", "surrogateescape")


class Interpreter:
    def __init__(self, checked: CheckResult, max_steps: int | None = None) -> None:
        self.checked = checked
        self.functions = {fn.name: fn for fn in checked.module.functions}
        self.constructors = checked.constructors
        self.method_targets = checked.method_targets
        # Variants of payloadless enums lower to a scalar tag natively, so the
        # native harness reports assert failures on them with the tag index.
        self.enum_index: dict[str, int] = {}
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
            module_name = self.checked.file_module.get(test.span.file, default_module)
            self.in_test = True
            try:
                self.exec_block(test.body, _Env())
                print(f"ok {module_name} / {test.name}")
                passed += 1
            except _TestFail as fail:
                if fail.reason:
                    print(fail.reason)
                print(f"fail {module_name} / {test.name}")
                failed += 1
            except _Return:
                # a `?` propagated an Err/None out of the test body: native lowers
                # this to an explicit-failure call, so match its output exactly.
                print("  explicit failure")
                print(f"fail {module_name} / {test.name}")
                failed += 1
            finally:
                self.in_test = False
        print(f"\n{passed} passed, {failed} failed")
        return 0 if failed == 0 else 1

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
        if isinstance(stmt, ast.WhileStmt):
            while self.eval(stmt.cond, env):
                self.exec_block(stmt.body, env)
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
        if isinstance(expr, ast.BoolLit):
            return expr.value
        if isinstance(expr, ast.StringLit):
            return expr.value
        if isinstance(expr, ast.NameExpr):
            if env.has(expr.name):
                return env.get(expr.name)
            if expr.name in self.constructors:
                return Variant(expr.name)
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
            return f"{left}{right}"
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        # The remaining operators are integer arithmetic/comparison, which the
        # checker only admits on I64 operands.
        assert isinstance(left, int) and isinstance(right, int)
        if op == "+":
            return _wrap(left + right)
        if op == "-":
            return _wrap(left - right)
        if op == "*":
            return _wrap(left * right)
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

        if isinstance(callee, ast.MemberExpr):
            obj = callee.obj
            if isinstance(obj, ast.NameExpr) and obj.name == "Log":
                message = str(self.eval(expr.args[0], env))
                end = "" if callee.name == "print" else "\n"  # flx_print vs flx_log
                try:
                    print(message, end=end, flush=True)
                except UnicodeEncodeError:
                    # Bytes that came from C via surrogateescape: write them raw,
                    # exactly as native flx_log/flx_print would.
                    sys.stdout.flush()
                    raw = message.encode("utf-8", "surrogateescape") + end.encode()
                    sys.stdout.buffer.write(raw)
                    sys.stdout.buffer.flush()
                return None
            if isinstance(obj, ast.NameExpr) and obj.name == "Fs" and callee.name == "read_line":
                sys.stdout.flush()
                return _read_line()
            if (
                isinstance(obj, ast.NameExpr)
                and obj.name == "Time"
                and (callee.name == "monotonic_ms")
            ):
                import time as _time

                return _wrap(_time.monotonic_ns() // 1_000_000)
            if callee.name in self.constructors:  # qualified ctor, e.g. E.Code(x)
                return Variant(callee.name, self._ctor_payload(expr.args, env))
            raise FlexRuntimeError(f"cannot interpret call to .{callee.name}")

        assert isinstance(callee, ast.NameExpr)
        name = callee.name
        if name in _BUILTINS:
            return self._builtin(name, expr, env)
        if name == "to_str":
            return str(self.eval(expr.args[0], env))
        if name in self.constructors:
            return Variant(name, self._ctor_payload(expr.args, env))
        if name in self.extern_fns:
            return self._call_extern(name, [self.eval(a, env) for a in expr.args])
        func = self.functions.get(name)
        if func is None:
            raise FlexRuntimeError(f"call to unknown function {name!r}")
        return self.call(func, [self.eval(a, env) for a in expr.args])

    def _call_extern(self, name: str, args: list[object]) -> object:
        param_kinds, ret_kind = self.checked.extern_abi[name]
        ctype_of = {"i64": ctypes.c_longlong, "i32": ctypes.c_int, "str": ctypes.c_char_p}
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
            equal = a == b
            if name == "assert_eq" and not equal:
                raise _TestFail(self._eq_reason("assert_eq", a, b))
            if name == "assert_ne" and equal:
                raise _TestFail(self._eq_reason("assert_ne", a, b))
            return None
        # fail(msg) / panic(msg)
        message = self.eval(expr.args[0], env) if expr.args else None
        raise _TestFail(f"  {message}" if message is not None else "  explicit failure")

    def _scalar(self, value: object) -> int | None:
        """The i64 the native harness would compare for an assert failure, or None
        for an aggregate (record / ADT-with-payload) it reports generically."""
        if value is None:
            return 0  # unit materializes as i64 0 natively
        if isinstance(value, bool):
            return 1 if value else 0
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


def run_main(checked: CheckResult) -> int:
    _ensure_recursion_headroom()
    try:
        return Interpreter(checked).run_main()
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
