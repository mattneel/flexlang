"""Generate the C test harness that drives compiled Flex test functions.

Each test lowers to an MLIR ``@flx_test_<i>() -> i32`` returning 0 on success
or 1 after the first failed assertion (which calls one of the ``__flx_*`` runtime
functions defined here). The generated ``main`` runs each test and prints the
report described in ``docs/MVP.md`` §3.3 / §5.6.
"""

from __future__ import annotations

from flx.backend.runtime import BASE_RUNTIME_C

# Test-only assertion-failure reporters (the shared runtime — __flx_log,
# __flx_match_fail — comes from BASE_RUNTIME_C, prepended below).
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
void __flx_assert_streq_fail(const char *a, long long an, const char *b, long long bn) {
    printf("  assert_eq failed: actual \"");
    fwrite(a, 1, (size_t)an, stdout);
    printf("\", expected \"");
    fwrite(b, 1, (size_t)bn, stdout);
    printf("\"\n");
}
void __flx_assert_strne_fail(const char *a, long long an) {
    printf("  assert_ne failed: both are \"");
    fwrite(a, 1, (size_t)an, stdout);
    printf("\"\n");
}
void __flx_explicit_fail(void) {
    printf("  explicit failure\n");
}
void __flx_fail_msg(const char *p, long long n) {
    printf("  ");
    fwrite(p, 1, (size_t)n, stdout);
    printf("\n");
}
"""


def _c_string(text: str) -> str:
    """Escape ``text`` for use inside a C double-quoted string literal."""
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\{ord(ch):03o}")
        else:
            out.append(ch)
    return "".join(out)


def generate_harness(tests: list[tuple[int, str]]) -> str:
    """Build the C harness. ``tests`` pairs each selected test's MLIR index with
    its full report label (``Module / test name``), so filtering stays aligned
    with the emitted ``@flx_test_<i>`` and imported tests report under their own
    module."""
    n = len(tests)
    plural = "" if n == 1 else "s"
    lines = [BASE_RUNTIME_C, _RUNTIME]
    for i, _ in tests:
        lines.append(f"extern int __flx_test_{i}(void);")
    lines.append("")
    lines.append("void __flx_set_args(int, char **);")
    lines.append("void __flx_set_test_recover(void *);")
    lines.append("int main(int argc, char **argv) {")
    lines.append("    __flx_set_args(argc, argv);")
    lines.append(f'    printf("running {n} test{plural}\\n\\n");')
    lines.append("    int passed = 0, failed = 0, code;")
    lines.append("    jmp_buf recover;")
    for i, full_label in tests:
        # Pass the label as a printf ARGUMENT (not in the format string), so any
        # '%' in a test name is inert rather than a conversion specifier.
        # A runtime panic (index out of bounds, division by zero) longjmps back
        # here, failing this ONE test; the rest of the suite still runs.
        label = _c_string(full_label)
        lines.append("    if (setjmp(recover) == 0) {")
        lines.append("        __flx_set_test_recover(&recover);")
        lines.append(f"        code = __flx_test_{i}();")
        lines.append("    } else {")
        lines.append("        code = 1;")
        lines.append("    }")
        lines.append("    __flx_set_test_recover(0);")
        lines.append("    if (code == 0) {")
        lines.append(f'        printf("ok %s\\n", "{label}");')
        lines.append("        passed++;")
        lines.append("    } else {")
        lines.append(f'        printf("fail %s\\n", "{label}");')
        lines.append("        failed++;")
        lines.append("    }")
    lines.append('    printf("\\n%d passed, %d failed\\n", passed, failed);')
    lines.append("    return failed == 0 ? 0 : 1;")
    lines.append("}")
    return "\n".join(lines) + "\n"
