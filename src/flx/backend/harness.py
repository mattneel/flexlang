"""Generate the C test harness that drives compiled Flex test functions.

Each test lowers to an MLIR ``@flx_test_<i>() -> i32`` returning 0 on success
or 1 after the first failed assertion (which calls one of the ``__flx_*`` runtime
functions defined here). The generated ``main`` runs each test and prints the
report described in ``docs/MVP.md`` §3.3 / §5.6.
"""

from __future__ import annotations

# Runtime assertion-failure reporters, called from compiled test code.
_RUNTIME = r"""#include <stdio.h>

void __flx_assert_fail(void) {
    printf("  assertion failed\n");
}
void __flx_assert_eq_fail(long long actual, long long expected) {
    printf("  assert_eq failed: actual %lld, expected %lld\n", actual, expected);
}
void __flx_assert_ne_fail(long long a, long long b) {
    printf("  assert_ne failed: both are %lld\n", a);
}
void __flx_explicit_fail(void) {
    printf("  explicit failure\n");
}
"""


def _c_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def generate_harness(module_name: str, tests: list[tuple[int, str]]) -> str:
    """Build the C harness. ``tests`` pairs each selected test's MLIR index with
    its name, so filtering stays aligned with the emitted ``@flx_test_<i>``."""
    n = len(tests)
    plural = "" if n == 1 else "s"
    lines = [_RUNTIME]
    for i, _ in tests:
        lines.append(f"extern int flx_test_{i}(void);")
    lines.append("")
    lines.append("int main(void) {")
    lines.append(f'    printf("running {n} test{plural}\\n\\n");')
    lines.append("    int passed = 0, failed = 0;")
    for i, name in tests:
        label = _c_string(f"{module_name} / {name}")
        lines.append(f"    if (flx_test_{i}() == 0) {{")
        lines.append(f'        printf("ok {label}\\n");')
        lines.append("        passed++;")
        lines.append("    } else {")
        lines.append(f'        printf("fail {label}\\n");')
        lines.append("        failed++;")
        lines.append("    }")
    lines.append('    printf("\\n%d passed, %d failed\\n", passed, failed);')
    lines.append("    return failed == 0 ? 0 : 1;")
    lines.append("}")
    return "\n".join(lines) + "\n"
