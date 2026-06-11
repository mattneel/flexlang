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

typedef struct { const char *ptr; long long len; } FlxStr;

void __flx_match_fail(void) {
    fputs("flex: non-exhaustive match reached\\n", stderr);
    abort();
}
// Guarded signed division/remainder. Raw LLVM sdiv/srem are undefined on a zero
// divisor and on INT64_MIN / -1; we trap the former (matching the interpreter's
// message + exit code) and define the latter as the 64-bit wrapping result.
long long __flx_idiv(long long a, long long b) {
    if (b == 0) {
        fflush(stdout);
        fputs("flx: runtime error: division by zero\\n", stderr);
        exit(1);
    }
    if (b == -1 && a == INT64_MIN) return INT64_MIN;
    return a / b;
}
long long __flx_imod(long long a, long long b) {
    if (b == 0) {
        fflush(stdout);
        fputs("flx: runtime error: division by zero\\n", stderr);
        exit(1);
    }
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
// One line from stdin, trailing newline stripped; "" at EOF (Fs.read_line).
void __flx_read_line(FlxStr *out) {
    char *line = NULL;
    size_t cap = 0;
    long long n = (long long)getline(&line, &cap, stdin);
    if (n <= 0) {
        free(line);
        out->ptr = "";
        out->len = 0;
        return;
    }
    if (line[n - 1] == '\\n') n--;
    line[n] = 0;
    out->ptr = line;
    out->len = n;
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
"""

# MLIR external declarations matching BASE_RUNTIME_C, prepended to every module.
BASE_RUNTIME_DECLS = (
    "func.func private @__flx_match_fail()\n"
    "func.func private @__flx_idiv(i64, i64) -> i64\n"
    "func.func private @__flx_imod(i64, i64) -> i64\n"
    "func.func private @__flx_log(!llvm.ptr, i64)\n"
    "func.func private @__flx_print(!llvm.ptr, i64)\n"
    "func.func private @__flx_read_line(!llvm.ptr)\n"
    "func.func private @__flx_monotonic_ms() -> i64\n"
    "func.func private @__flx_int_to_str(i64, !llvm.ptr)\n"
    "func.func private @__flx_str_concat(!llvm.ptr, i64, !llvm.ptr, i64, !llvm.ptr)\n"
    "func.func private @__flx_cstr_wrap(!llvm.ptr, !llvm.ptr)\n"
    "func.func private @__flx_box(i64) -> !llvm.ptr\n"
)
