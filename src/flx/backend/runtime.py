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
"""

# MLIR external declarations matching BASE_RUNTIME_C, prepended to every module.
BASE_RUNTIME_DECLS = "func.func private @__flx_match_fail()\n"
