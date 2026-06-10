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

typedef struct { const char *ptr; long long len; } FlxStr;

void __flx_match_fail(void) {
    fputs("flex: non-exhaustive match reached\\n", stderr);
    abort();
}
// Guarded signed division/remainder. Raw LLVM sdiv/srem are undefined on a zero
// divisor and on INT64_MIN / -1; we trap the former (matching the interpreter's
// message + exit code) and define the latter as the 64-bit wrapping result.
long long flx_idiv(long long a, long long b) {
    if (b == 0) {
        fflush(stdout);
        fputs("flx: runtime error: division by zero\\n", stderr);
        exit(1);
    }
    if (b == -1 && a == INT64_MIN) return INT64_MIN;
    return a / b;
}
long long flx_imod(long long a, long long b) {
    if (b == 0) {
        fflush(stdout);
        fputs("flx: runtime error: division by zero\\n", stderr);
        exit(1);
    }
    if (b == -1) return 0;
    return a % b;
}
void flx_log(const char *p, long long n) {
    fwrite(p, 1, (size_t)n, stdout);
    fputc('\\n', stdout);
}
void flx_int_to_str(long long n, FlxStr *out) {
    char *buf = (char *)malloc(24);
    out->len = (long long)sprintf(buf, "%lld", n);
    out->ptr = buf;
}
void flx_str_concat(const char *p1, long long n1, const char *p2, long long n2, FlxStr *out) {
    char *buf = (char *)malloc((size_t)(n1 + n2) + 1);
    memcpy(buf, p1, (size_t)n1);
    memcpy(buf + n1, p2, (size_t)n2);
    buf[n1 + n2] = 0;
    out->ptr = buf;
    out->len = n1 + n2;
}
"""

# MLIR external declarations matching BASE_RUNTIME_C, prepended to every module.
BASE_RUNTIME_DECLS = (
    "func.func private @__flx_match_fail()\n"
    "func.func private @flx_idiv(i64, i64) -> i64\n"
    "func.func private @flx_imod(i64, i64) -> i64\n"
    "func.func private @flx_log(!llvm.ptr, i64)\n"
    "func.func private @flx_int_to_str(i64, !llvm.ptr)\n"
    "func.func private @flx_str_concat(!llvm.ptr, i64, !llvm.ptr, i64, !llvm.ptr)\n"
)
