"""C runtime shared by `flx run` and `flx test` builds.

Contains runtime functions that compiled Flex code may call regardless of test
context (currently just the non-exhaustive-match trap; the checker proves
exhaustiveness, so it should be unreachable, but the symbol must still link).
"""

from __future__ import annotations

BASE_RUNTIME_C = """#include <stdio.h>
#include <stdlib.h>

void __flx_match_fail(void) {
    fputs("flex: non-exhaustive match reached\\n", stderr);
    abort();
}
void flx_log(const char *p, long long n) {
    fwrite(p, 1, (size_t)n, stdout);
    fputc('\\n', stdout);
}
"""

# MLIR external declarations matching BASE_RUNTIME_C, prepended to every module.
BASE_RUNTIME_DECLS = (
    "func.func private @__flx_match_fail()\nfunc.func private @flx_log(!llvm.ptr, i64)\n"
)
