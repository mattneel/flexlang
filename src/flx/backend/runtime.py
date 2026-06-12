"""C runtime shared by `flx run` and `flx test` builds.

Contains runtime functions that compiled Flex code may call regardless of test
context (currently just the non-exhaustive-match trap; the checker proves
exhaustiveness, so it should be unreachable, but the symbol must still link).
"""

from __future__ import annotations

BASE_RUNTIME_C = """#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <signal.h>
#include <unistd.h>
#include <setjmp.h>

typedef struct { const char *ptr; long long len; } FlxStr;

// Runtime panics (division by zero, index out of bounds) are fatal in a
// program run, but inside `flx test` they fail just the ONE test: the harness
// arms a recovery point per test and we long-jump back to it, mirroring the
// interpreter's per-test exception handling.
static jmp_buf *__flx_test_recover_env = NULL;
void __flx_set_test_recover(void *env) { __flx_test_recover_env = (jmp_buf *)env; }
static void __flx_runtime_fail(const char *msg) {
    if (__flx_test_recover_env) {
        printf("  runtime error: %s\\n", msg);
        fflush(stdout);
        longjmp(*__flx_test_recover_env, 1);
    }
    fflush(stdout);
    fprintf(stderr, "flx: runtime error: %s\\n", msg);
    exit(1);
}

// Deep recursion must not die as a raw SIGSEGV: report it like the
// interpreter's stack guard does and exit 1. The handler runs on its own
// stack (the overflowed one is unusable) and uses only async-signal-safe
// calls. A fixed buffer because SIGSTKSZ is not a constant on glibc >= 2.34.
static char __flx_sigstack[65536];
static void __flx_on_segv(int sig) {
    (void)sig;
    static const char msg[] =
        "flx: runtime error: stack overflow (recursion too deep)\\n";
    ssize_t ignored = write(2, msg, sizeof(msg) - 1);
    (void)ignored;
    _exit(1);
}
__attribute__((constructor)) static void __flx_install_stack_guard(void) {
    stack_t ss;
    memset(&ss, 0, sizeof ss);
    ss.ss_sp = __flx_sigstack;
    ss.ss_size = sizeof __flx_sigstack;
    sigaltstack(&ss, 0);
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = __flx_on_segv;
    sa.sa_flags = SA_ONSTACK;
    sigaction(SIGSEGV, &sa, 0);
}

void __flx_match_fail(void) {
    fputs("flex: non-exhaustive match reached\\n", stderr);
    abort();
}
// Guarded signed division/remainder. Raw LLVM sdiv/srem are undefined on a zero
// divisor and on INT64_MIN / -1; we trap the former (matching the interpreter's
// message + exit code) and define the latter as the 64-bit wrapping result.
long long __flx_idiv(long long a, long long b) {
    if (b == 0) __flx_runtime_fail("division by zero");
    if (b == -1 && a == INT64_MIN) return INT64_MIN;
    return a / b;
}
long long __flx_imod(long long a, long long b) {
    if (b == 0) __flx_runtime_fail("division by zero");
    if (b == -1) return 0;
    return a % b;
}
void __flx_log(const char *p, long long n) {
    fwrite(p, 1, (size_t)n, stdout);
    fputc('\\n', stdout);
    // Flush per line so output survives an extern call that aborts/raises
    // (the interpreter flushes before every extern call; this matches it).
    fflush(stdout);
}
void __flx_int_to_str(long long n, FlxStr *out) {
    char *buf = (char *)malloc(24);
    out->len = (long long)sprintf(buf, "%lld", n);
    out->ptr = buf;
}
// Console output without a newline (Log.print). Flushed like __flx_log.
void __flx_print(const char *p, long long n) {
    fwrite(p, 1, (size_t)n, stdout);
    fflush(stdout);
}
// One line from stdin for Fs.read_line(): returns 1 with *out filled (trailing
// newline stripped), or 0 at end of input — the lowering builds Some/None from
// the flag, so a blank line (1, "") and EOF (0) are distinguishable.
long long __flx_read_line_opt(FlxStr *out) {
    char *line = NULL;
    size_t cap = 0;
    long long n = (long long)getline(&line, &cap, stdin);
    if (n <= 0) {
        free(line);
        out->ptr = "";
        out->len = 0;
        return 0;
    }
    if (line[n - 1] == '\\n') n--;
    line[n] = 0;
    // Strings are NUL-terminated: an embedded NUL would give strlen-based ops
    // (length, ++) a shorter extent than the stored length, so the line is
    // truncated at the first NUL on BOTH backends.
    out->ptr = line;
    out->len = (long long)strlen(line);
    return 1;
}
// Monotonic wall clock in milliseconds (Time.monotonic_ms).
long long __flx_monotonic_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
// Wrap a C string returned by an extern call as a Flex String (NULL -> "").
void __flx_cstr_wrap(const char *p, FlxStr *out) {
    out->ptr = p ? p : "";
    out->len = p ? (long long)strlen(p) : 0;
}
void __flx_str_concat(const char *p1, long long n1, const char *p2, long long n2, FlxStr *out) {
    char *buf = (char *)malloc((size_t)(n1 + n2) + 1);
    memcpy(buf, p1, (size_t)n1);
    memcpy(buf + n1, p2, (size_t)n2);
    buf[n1 + n2] = 0;
    out->ptr = buf;
    out->len = n1 + n2;
}
// Heap cell for a boxed ADT payload (a payload that doesn't fit the i64 slot
// by value: strings, records, non-enum ADTs, multi-field payloads). Boxes are
// immutable once written and reclaimed at process exit; region-based
// reclamation is the roadmap.
void *__flx_box(long long size) {
    void *p = malloc((size_t)size);
    if (!p) {
        fflush(stdout);
        fputs("flx: runtime error: out of memory\\n", stderr);
        exit(1);
    }
    return p;
}
// The growable list (List<T>): a header with an i64-slot element array.
// Elements use the SAME slot codec as ADT payloads — inline scalars by value,
// everything else a __flx_box address. Lists have reference semantics.
typedef struct { long long len; long long cap; long long *data; } FlxList;
void *__flx_list_new(void) {
    FlxList *l = (FlxList *)__flx_box((long long)sizeof(FlxList));
    l->len = 0;
    l->cap = 0;
    l->data = NULL;
    return l;
}
void __flx_list_push(void *lp, long long v) {
    FlxList *l = (FlxList *)lp;
    if (l->len == l->cap) {
        l->cap = l->cap ? l->cap * 2 : 8;
        l->data = (long long *)realloc(l->data, (size_t)l->cap * sizeof(long long));
        if (!l->data) {
            fflush(stdout);
            fputs("flx: runtime error: out of memory\\n", stderr);
            exit(1);
        }
    }
    l->data[l->len++] = v;
}
static void __flx_bounds(long long i, long long len) {
    if (i < 0 || i >= len) {
        char msg[80];
        snprintf(msg, sizeof msg, "index %lld out of bounds (len %lld)", i, len);
        __flx_runtime_fail(msg);
    }
}
long long __flx_list_get(void *lp, long long i) {
    FlxList *l = (FlxList *)lp;
    __flx_bounds(i, l->len);
    return l->data[i];
}
void __flx_list_set(void *lp, long long i, long long v) {
    FlxList *l = (FlxList *)lp;
    __flx_bounds(i, l->len);
    l->data[i] = v;
}
long long __flx_list_len(void *lp) {
    return ((FlxList *)lp)->len;
}
// Shortest %g representation that round-trips (the interpreter runs the same
// loop through Python's C-printf formatting, so the text matches). NaN is
// canonicalized first: x86 produces sign-set NaNs at runtime, which glibc
// would print as "-nan" while Python never signs a NaN.
void __flx_f64_fmt(char *buf, size_t cap, double x) {
    if (x != x) {
        snprintf(buf, cap, "nan");
        return;
    }
    for (int prec = 1; prec <= 17; prec++) {
        snprintf(buf, cap, "%.*g", prec, x);
        if (strtod(buf, NULL) == x) return;
    }
}
void __flx_f64_to_str(double x, FlxStr *out) {
    char *buf = (char *)__flx_box(32);
    __flx_f64_fmt(buf, 32, x);
    out->ptr = buf;
    out->len = (long long)strlen(buf);
}
// Checked F64 -> I64 truncation: NaN/infinity/out-of-range has no honest
// answer (LLVM fptosi would be poison), so it panics like indexing does.
long long __flx_f64_to_i64(double x) {
    if (!(x == x) || x >= 9223372036854775808.0 || x < -9223372036854775808.0) {
        char msg[64];
        char num[32];
        __flx_f64_fmt(num, sizeof num, x);
        snprintf(msg, sizeof msg, "cannot convert %s to I64", num);
        __flx_runtime_fail(msg);
    }
    return (long long)x;
}
// Byte-indexed string primitives (Std.Str): byte_at panics out of bounds,
// substr clamps. Strings are byte strings; UTF-8 awareness is the roadmap.
long long __flx_byte_at(const char *p, long long n, long long i) {
    __flx_bounds(i, n);
    return (long long)(unsigned char)p[i];
}
void __flx_substr(const char *p, long long n, long long start, long long count, FlxStr *out) {
    if (start < 0) start = 0;
    if (start > n) start = n;
    if (count < 0) count = 0;
    if (count > n - start) count = n - start;
    char *buf = (char *)__flx_box(count + 1);
    memcpy(buf, p + start, (size_t)count);
    buf[count] = 0;
    out->ptr = buf;
    out->len = count;
}
// Byte -> String construction (Std.Str from_byte/from_bytes). Bytes must be
// 1..255: byte 0 is the NUL terminator and cannot be carried by a string.
static void __flx_byte_check(long long b) {
    if (b < 1 || b > 255) {
        char msg[96]; /* 47 static chars + up to 20 digits w/ sign + NUL */
        snprintf(msg, sizeof msg, "byte %lld is outside 1..255 (strings are NUL-terminated)", b);
        __flx_runtime_fail(msg);
    }
}
void __flx_from_byte(long long b, FlxStr *out) {
    __flx_byte_check(b);
    char *buf = (char *)__flx_box(2);
    buf[0] = (char)b;
    buf[1] = 0;
    out->ptr = buf;
    out->len = 1;
}
void __flx_from_bytes(void *lp, FlxStr *out) {
    FlxList *l = (FlxList *)lp;
    char *buf = (char *)__flx_box(l->len + 1);
    for (long long i = 0; i < l->len; i++) {
        __flx_byte_check(l->data[i]);
        buf[i] = (char)l->data[i];
    }
    buf[l->len] = 0;
    out->ptr = buf;
    out->len = l->len;
}
// Float <-> text (Std.Str parse_float / to_str_fixed). parse_f64 is strtod of
// the longest valid prefix (0.0 if none) — the interpreter calls the SAME
// libc strtod via ctypes, so the bits match by construction; Std.Str's
// parse_float validates the strict whole-string grammar before calling it.
double __flx_parse_f64(const char *p) {
    return strtod(p, NULL);
}
void __flx_f64_fixed(double x, long long d, FlxStr *out) {
    if (d < 0 || d > 100) {
        char msg[64];
        snprintf(msg, sizeof msg, "decimals %lld is outside 0..100", d);
        __flx_runtime_fail(msg);
    }
    if (x != x) {
        out->ptr = "nan"; /* never "-nan"; matches to_str's canonical form */
        out->len = 3;
        return;
    }
    int need = snprintf(NULL, 0, "%.*f", (int)d, x);
    char *buf = (char *)__flx_box(need + 1);
    snprintf(buf, (size_t)need + 1, "%.*f", (int)d, x);
    out->ptr = buf;
    out->len = need;
}
// Program arguments, captured by the run shim's main(). Env.argv() yields the
// USER arguments only (argv[0] is the executable path, which differs across
// backends, so it is deliberately excluded).
static int g_argc = 0;
static char **g_argv = NULL;
void __flx_set_args(int argc, char **argv) {
    g_argc = argc;
    g_argv = argv;
}
void *__flx_argv(void) {
    FlxList *l = (FlxList *)__flx_list_new();
    for (int i = 1; i < g_argc; i++) {
        FlxStr *s = (FlxStr *)__flx_box((long long)sizeof(FlxStr));
        s->ptr = g_argv[i];
        s->len = (long long)strlen(g_argv[i]);
        __flx_list_push(l, (long long)s);
    }
    return l;
}
"""

# MLIR external declarations matching BASE_RUNTIME_C, prepended to every module.
BASE_RUNTIME_DECLS = (
    "func.func private @__flx_match_fail()\n"
    "func.func private @__flx_idiv(i64, i64) -> i64\n"
    "func.func private @__flx_imod(i64, i64) -> i64\n"
    "func.func private @__flx_log(!llvm.ptr, i64)\n"
    "func.func private @__flx_print(!llvm.ptr, i64)\n"
    "func.func private @__flx_read_line_opt(!llvm.ptr) -> i64\n"
    "func.func private @__flx_monotonic_ms() -> i64\n"
    "func.func private @__flx_int_to_str(i64, !llvm.ptr)\n"
    "func.func private @__flx_str_concat(!llvm.ptr, i64, !llvm.ptr, i64, !llvm.ptr)\n"
    "func.func private @__flx_cstr_wrap(!llvm.ptr, !llvm.ptr)\n"
    "func.func private @__flx_box(i64) -> !llvm.ptr\n"
    "func.func private @__flx_list_new() -> !llvm.ptr\n"
    "func.func private @__flx_list_push(!llvm.ptr, i64)\n"
    "func.func private @__flx_list_get(!llvm.ptr, i64) -> i64\n"
    "func.func private @__flx_list_set(!llvm.ptr, i64, i64)\n"
    "func.func private @__flx_list_len(!llvm.ptr) -> i64\n"
    "func.func private @__flx_byte_at(!llvm.ptr, i64, i64) -> i64\n"
    "func.func private @__flx_substr(!llvm.ptr, i64, i64, i64, !llvm.ptr)\n"
    "func.func private @__flx_from_byte(i64, !llvm.ptr)\n"
    "func.func private @__flx_from_bytes(!llvm.ptr, !llvm.ptr)\n"
    "func.func private @__flx_parse_f64(!llvm.ptr) -> f64\n"
    "func.func private @__flx_f64_fixed(f64, i64, !llvm.ptr)\n"
    "func.func private @__flx_argv() -> !llvm.ptr\n"
    "func.func private @__flx_f64_to_str(f64, !llvm.ptr)\n"
    "func.func private @__flx_f64_to_i64(f64) -> i64\n"
)
