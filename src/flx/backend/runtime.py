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
#include <errno.h>
#include <limits.h>

typedef struct { const char *ptr; long long len; } FlxStr;
void *__flx_box(long long size); /* checked allocator, defined below */

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
static void __flx_oom(void) {
    fflush(stdout);
    fputs("flx: runtime error: out of memory\\n", stderr);
    exit(1);
}
static void *__flx_malloc_size(size_t size) {
    void *p = malloc(size ? size : 1);
    if (!p) __flx_oom();
    return p;
}
static size_t __flx_alloc_size(long long size) {
    if (size < 0) __flx_runtime_fail("negative allocation size");
    return (size_t)size;
}
static size_t __flx_byte_len(long long len) {
    if (len < 0) __flx_runtime_fail("negative byte length");
    return (size_t)len;
}
static size_t __flx_checked_add_size(size_t a, size_t b) {
    if (SIZE_MAX - a < b) __flx_runtime_fail("allocation size overflow");
    return a + b;
}
static size_t __flx_checked_mul_size(size_t a, size_t b) {
    if (a != 0 && b > SIZE_MAX / a) __flx_runtime_fail("allocation size overflow");
    return a * b;
}
static long long __flx_checked_add_len(long long a, long long b) {
    if (a < 0 || b < 0) __flx_runtime_fail("negative byte length");
    if (a > LLONG_MAX - b) __flx_runtime_fail("allocation size overflow");
    return a + b;
}
static size_t __flx_len_with_nul(long long len) {
    return __flx_checked_add_size(__flx_byte_len(len), 1);
}
static void *__flx_realloc_array(void *ptr, long long count, size_t elem_size) {
    size_t bytes = __flx_checked_mul_size(__flx_alloc_size(count), elem_size);
    void *p = realloc(ptr, bytes ? bytes : 1);
    if (!p) __flx_oom();
    return p;
}
static long long __flx_grow_capacity(long long cap) {
    if (cap < 0) __flx_runtime_fail("invalid negative container capacity");
    if (cap == 0) return 8;
    if (cap > LLONG_MAX / 2) __flx_runtime_fail("container capacity overflow");
    return cap * 2;
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
void __flx_error(const char *p, long long n) {
    fwrite(p, 1, (size_t)n, stderr);
    fputc('\\n', stderr);
    fflush(stderr);
}
void __flx_int_to_str(long long n, FlxStr *out) {
    char *buf = (char *)__flx_box(24);
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
static void __flx_copy_cstr(const char *msg, FlxStr *out) {
    long long n = (long long)strlen(msg);
    char *buf = (char *)__flx_malloc_size(__flx_len_with_nul(n));
    memcpy(buf, msg, __flx_len_with_nul(n));
    out->ptr = buf;
    out->len = n;
}
static char *__flx_path_copy(const char *p, long long n) {
    size_t len = __flx_byte_len(n);
    char *path = (char *)__flx_malloc_size(__flx_checked_add_size(len, 1));
    memcpy(path, p, len);
    path[len] = 0;
    return path;
}
long long __flx_read_text(const char *p, long long n, FlxStr *out) {
    char *path = __flx_path_copy(p, n);
    FILE *f = fopen(path, "rb");
    if (!f) {
        __flx_copy_cstr(strerror(errno), out);
        return 0;
    }
    if (fseek(f, 0, SEEK_END) != 0) {
        __flx_copy_cstr(strerror(errno), out);
        fclose(f);
        return 0;
    }
    long size = ftell(f);
    if (size < 0) {
        __flx_copy_cstr(strerror(errno), out);
        fclose(f);
        return 0;
    }
    rewind(f);
    char *buf = (char *)__flx_malloc_size(__flx_len_with_nul((long long)size));
    size_t got = fread(buf, 1, (size_t)size, f);
    if (got != (size_t)size || ferror(f)) {
        __flx_copy_cstr(strerror(errno), out);
        fclose(f);
        return 0;
    }
    fclose(f);
    if (memchr(buf, 0, got) != NULL) {
        __flx_copy_cstr("file contains NUL byte; use a List<I64> byte buffer", out);
        return 0;
    }
    buf[got] = 0;
    out->ptr = buf;
    out->len = (long long)got;
    return 1;
}
long long __flx_write_text(const char *p, long long n, const char *q, long long m, FlxStr *err) {
    char *path = __flx_path_copy(p, n);
    FILE *f = fopen(path, "wb");
    if (!f) {
        __flx_copy_cstr(strerror(errno), err);
        return 0;
    }
    size_t len = __flx_byte_len(m);
    size_t wrote = fwrite(q, 1, len, f);
    if (wrote != len || ferror(f)) {
        __flx_copy_cstr(strerror(errno), err);
        fclose(f);
        return 0;
    }
    if (fclose(f) != 0) {
        __flx_copy_cstr(strerror(errno), err);
        return 0;
    }
    err->ptr = "";
    err->len = 0;
    return 1;
}
long long __flx_append_text(const char *p, long long n, const char *q, long long m, FlxStr *err) {
    char *path = __flx_path_copy(p, n);
    FILE *f = fopen(path, "ab");
    if (!f) {
        __flx_copy_cstr(strerror(errno), err);
        return 0;
    }
    size_t len = __flx_byte_len(m);
    size_t wrote = fwrite(q, 1, len, f);
    if (wrote != len || ferror(f)) {
        __flx_copy_cstr(strerror(errno), err);
        fclose(f);
        return 0;
    }
    if (fclose(f) != 0) {
        __flx_copy_cstr(strerror(errno), err);
        return 0;
    }
    err->ptr = "";
    err->len = 0;
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
    // __flx_box, not raw malloc: heap exhaustion (quadratic ++ chains) must
    // die as "out of memory", not as a NULL-write SIGSEGV that the stack
    // guard would misreport as runaway recursion.
    long long out_len = __flx_checked_add_len(n1, n2);
    size_t len1 = __flx_byte_len(n1);
    size_t len2 = __flx_byte_len(n2);
    char *buf = (char *)__flx_malloc_size(__flx_len_with_nul(out_len));
    memcpy(buf, p1, len1);
    memcpy(buf + len1, p2, len2);
    buf[out_len] = 0;
    out->ptr = buf;
    out->len = out_len;
}
// Heap cell for a boxed ADT payload (a payload that doesn't fit the i64 slot
// by value: strings, records, non-enum ADTs, multi-field payloads). Boxes are
// immutable once written and reclaimed at process exit; region-based
// reclamation is the roadmap.
void *__flx_box(long long size) {
    return __flx_malloc_size(__flx_alloc_size(size));
}
// The growable list (List<T>): a header with an i64-slot element array.
// Elements use the SAME slot codec as ADT payloads — inline scalars by value,
// everything else a __flx_box address. Lists have reference semantics.
typedef struct { long long len; long long cap; long long *data; } FlxList;
static void __flx_list_check(FlxList *l) {
    if (l->len < 0 || l->cap < 0 || l->len > l->cap) {
        __flx_runtime_fail("invalid list header");
    }
}
void *__flx_list_new(void) {
    FlxList *l = (FlxList *)__flx_box((long long)sizeof(FlxList));
    l->len = 0;
    l->cap = 0;
    l->data = NULL;
    return l;
}
void __flx_list_push(void *lp, long long v) {
    FlxList *l = (FlxList *)lp;
    __flx_list_check(l);
    if (l->len == l->cap) {
        long long cap = __flx_grow_capacity(l->cap);
        l->data = (long long *)__flx_realloc_array(l->data, cap, sizeof(long long));
        l->cap = cap;
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
    __flx_list_check(l);
    __flx_bounds(i, l->len);
    return l->data[i];
}
void __flx_list_set(void *lp, long long i, long long v) {
    FlxList *l = (FlxList *)lp;
    __flx_list_check(l);
    __flx_bounds(i, l->len);
    l->data[i] = v;
}
long long __flx_list_len(void *lp) {
    FlxList *l = (FlxList *)lp;
    __flx_list_check(l);
    return l->len;
}
// Remove and return the LAST element's slot: 1 with *out filled, 0 when empty.
// The lowering assembles Some/None from the flag; the slot IS the payload.
long long __flx_list_pop(void *lp, long long *out) {
    FlxList *l = (FlxList *)lp;
    __flx_list_check(l);
    if (l->len == 0) {
        *out = 0;
        return 0;
    }
    l->len--;
    *out = l->data[l->len];
    return 1;
}
long long __flx_str_eq(const char *lp, long long ln, const char *rp, long long rn) {
    size_t llen = __flx_byte_len(ln);
    size_t rlen = __flx_byte_len(rn);
    if (llen != rlen) return 0;
    return memcmp(lp, rp, llen) == 0;
}
// ADT payload-slot equality with String awareness: when use_str, the slots
// are boxed FlxStr cells compared by CONTENT; otherwise raw slot bits. The
// flag keeps boxed dereferences off the non-string arms (a None slot is 0).
long long __flx_slot_str_eq(long long use_str, long long ls, long long rs) {
    if (!use_str) return ls == rs;
    if (ls == 0 || rs == 0) return ls == rs;
    FlxStr *a = (FlxStr *)ls;
    FlxStr *b = (FlxStr *)rs;
    return __flx_str_eq(a->ptr, a->len, b->ptr, b->len);
}
// The insertion-ordered map (Map<String, V>): an append-only entries array
// with tombstones. Set on a live key replaces in place (keeping its position),
// remove marks dead, re-insert appends — exactly a Python dict's observable
// order, which is what the interpreter uses. Lookup is a linear scan; a hash
// index is the roadmap. Values use the SAME i64 slot codec as list elements.
typedef struct { const char *key; long long keylen; long long slot; int alive; } FlxMapEntry;
typedef struct { long long len; long long cap; long long used; FlxMapEntry *entries; } FlxMap;
static void __flx_map_check(FlxMap *m) {
    if (m->len < 0 || m->cap < 0 || m->used < 0 || m->len > m->used || m->used > m->cap) {
        __flx_runtime_fail("invalid map header");
    }
}
void *__flx_map_new(void) {
    FlxMap *m = (FlxMap *)__flx_box((long long)sizeof(FlxMap));
    m->len = 0;
    m->cap = 0;
    m->used = 0;
    m->entries = NULL;
    return m;
}
static FlxMapEntry *__flx_map_find(FlxMap *m, const char *k, long long klen) {
    __flx_map_check(m);
    size_t keylen = __flx_byte_len(klen);
    for (long long i = 0; i < m->used; i++) {
        FlxMapEntry *e = &m->entries[i];
        if (e->alive && e->keylen == klen && memcmp(e->key, k, keylen) == 0) {
            return e;
        }
    }
    return NULL;
}
void __flx_map_set(void *mp, const char *k, long long klen, long long slot) {
    FlxMap *m = (FlxMap *)mp;
    FlxMapEntry *e = __flx_map_find(m, k, klen);
    if (e) {
        e->slot = slot;
        return;
    }
    if (m->used == m->cap) {
        long long cap = __flx_grow_capacity(m->cap);
        m->entries = (FlxMapEntry *)__flx_realloc_array(m->entries, cap, sizeof(FlxMapEntry));
        m->cap = cap;
    }
    size_t keylen = __flx_byte_len(klen);
    char *kcopy = (char *)__flx_malloc_size(__flx_checked_add_size(keylen, 1));
    memcpy(kcopy, k, keylen);
    kcopy[keylen] = 0;
    m->entries[m->used].key = kcopy;
    m->entries[m->used].keylen = klen;
    m->entries[m->used].slot = slot;
    m->entries[m->used].alive = 1;
    m->used++;
    m->len++;
}
long long __flx_map_get(void *mp, const char *k, long long klen, long long *out) {
    FlxMapEntry *e = __flx_map_find((FlxMap *)mp, k, klen);
    if (!e) {
        *out = 0;
        return 0;
    }
    *out = e->slot;
    return 1;
}
long long __flx_map_has(void *mp, const char *k, long long klen) {
    return __flx_map_find((FlxMap *)mp, k, klen) != NULL;
}
long long __flx_map_len(void *mp) {
    return ((FlxMap *)mp)->len;
}
void __flx_map_remove(void *mp, const char *k, long long klen) {
    FlxMap *m = (FlxMap *)mp;
    FlxMapEntry *e = __flx_map_find(m, k, klen);
    if (e) {
        e->alive = 0;
        m->len--;
    }
}
void *__flx_map_keys(void *mp) {
    FlxMap *m = (FlxMap *)mp;
    __flx_map_check(m);
    FlxList *out = (FlxList *)__flx_list_new();
    for (long long i = 0; i < m->used; i++) {
        if (!m->entries[i].alive) continue;
        FlxStr *s = (FlxStr *)__flx_box((long long)sizeof(FlxStr));
        s->ptr = m->entries[i].key;
        s->len = m->entries[i].keylen;
        __flx_list_push(out, (long long)s);
    }
    return out;
}
void *__flx_map_values(void *mp) {
    FlxMap *m = (FlxMap *)mp;
    __flx_map_check(m);
    FlxList *out = (FlxList *)__flx_list_new();
    for (long long i = 0; i < m->used; i++) {
        if (m->entries[i].alive) __flx_list_push(out, m->entries[i].slot);
    }
    return out;
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
static unsigned long long __flx_umax_bits(long long bits) {
    return bits >= 64 ? ULLONG_MAX : ((1ULL << bits) - 1ULL);
}
static long long __flx_smin_bits(long long bits) {
    return bits >= 64 ? LLONG_MIN : -(1LL << (bits - 1));
}
static long long __flx_smax_bits(long long bits) {
    return bits >= 64 ? LLONG_MAX : ((1LL << (bits - 1)) - 1LL);
}
static void __flx_int_convert_fail(void) {
    __flx_runtime_fail("integer conversion out of range");
}
long long __flx_int_to_int(
    long long value,
    long long src_bits,
    long long src_unsigned,
    long long dst_bits,
    long long dst_unsigned
) {
    if (src_unsigned) {
        unsigned long long raw = (unsigned long long)value;
        if (src_bits < 64) raw &= __flx_umax_bits(src_bits);
        if (dst_unsigned) {
            if (dst_bits < 64 && raw > __flx_umax_bits(dst_bits)) __flx_int_convert_fail();
            return (long long)raw;
        }
        unsigned long long max = (unsigned long long)__flx_smax_bits(dst_bits);
        if (raw > max) __flx_int_convert_fail();
        return (long long)raw;
    }
    if (dst_unsigned) {
        if (value < 0) __flx_int_convert_fail();
        unsigned long long raw = (unsigned long long)value;
        if (dst_bits < 64 && raw > __flx_umax_bits(dst_bits)) __flx_int_convert_fail();
        return (long long)raw;
    }
    if (value < __flx_smin_bits(dst_bits) || value > __flx_smax_bits(dst_bits)) {
        __flx_int_convert_fail();
    }
    return value;
}
long long __flx_f64_to_int(double x, long long dst_bits, long long dst_unsigned) {
    if (dst_unsigned) {
        long double upper =
            dst_bits >= 64 ? 18446744073709551616.0L : (long double)(1ULL << dst_bits);
        if (!((long double)x >= 0.0L && (long double)x < upper)) __flx_int_convert_fail();
        return (long long)(unsigned long long)x;
    }
    long double lower =
        dst_bits >= 64 ? -9223372036854775808.0L : (long double)__flx_smin_bits(dst_bits);
    long double upper =
        dst_bits >= 64 ? 9223372036854775808.0L : (long double)(1LL << (dst_bits - 1));
    if (!((long double)x >= lower && (long double)x < upper)) __flx_int_convert_fail();
    return (long long)x;
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
    size_t len = __flx_byte_len(count);
    char *buf = (char *)__flx_malloc_size(__flx_checked_add_size(len, 1));
    memcpy(buf, p + start, len);
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
    __flx_list_check(l);
    char *buf = (char *)__flx_malloc_size(__flx_len_with_nul(l->len));
    for (long long i = 0; i < l->len; i++) {
        __flx_byte_check(l->data[i]);
        buf[i] = (char)l->data[i];
    }
    buf[l->len] = 0;
    out->ptr = buf;
    out->len = l->len;
}
// Raw Bytes buffers: unlike String construction, byte 0 is valid.
static void __flx_binary_byte_check(long long b) {
    if (b < 0 || b > 255) {
        char msg[64];
        snprintf(msg, sizeof msg, "byte %lld is outside 0..255", b);
        __flx_runtime_fail(msg);
    }
}
void __flx_bytes_push(void *lp, long long b) {
    __flx_binary_byte_check(b);
    __flx_list_push(lp, b);
}
void __flx_bytes_to_hex(void *lp, FlxStr *out) {
    FlxList *l = (FlxList *)lp;
    __flx_list_check(l);
    long long out_len = __flx_checked_add_len(l->len, l->len);
    char *buf = (char *)__flx_malloc_size(__flx_len_with_nul(out_len));
    const char *digits = "0123456789abcdef";
    for (long long i = 0; i < l->len; i++) {
        long long b = l->data[i];
        __flx_binary_byte_check(b);
        buf[i * 2] = digits[(b >> 4) & 15];
        buf[i * 2 + 1] = digits[b & 15];
    }
    buf[out_len] = 0;
    out->ptr = buf;
    out->len = out_len;
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
    char *buf = (char *)__flx_malloc_size(__flx_len_with_nul((long long)need));
    snprintf(buf, (size_t)need + 1, "%.*f", (int)d, x);
    out->ptr = buf;
    out->len = need;
}
void __flx_i64_to_hex(long long n, FlxStr *out) {
    char *buf = (char *)__flx_box(17);
    out->len = (long long)sprintf(buf, "%llx", (unsigned long long)n);
    out->ptr = buf;
}
void __flx_i64_to_unsigned(long long n, FlxStr *out) {
    char *buf = (char *)__flx_box(32);
    out->len = (long long)sprintf(buf, "%llu", (unsigned long long)n);
    out->ptr = buf;
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
    "func.func private @__flx_error(!llvm.ptr, i64)\n"
    "func.func private @__flx_print(!llvm.ptr, i64)\n"
    "func.func private @__flx_read_line_opt(!llvm.ptr) -> i64\n"
    "func.func private @__flx_read_text(!llvm.ptr, i64, !llvm.ptr) -> i64\n"
    "func.func private @__flx_write_text(!llvm.ptr, i64, !llvm.ptr, i64, !llvm.ptr) -> i64\n"
    "func.func private @__flx_append_text(!llvm.ptr, i64, !llvm.ptr, i64, !llvm.ptr) -> i64\n"
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
    "func.func private @__flx_list_pop(!llvm.ptr, !llvm.ptr) -> i64\n"
    "func.func private @__flx_str_eq(!llvm.ptr, i64, !llvm.ptr, i64) -> i64\n"
    "func.func private @__flx_slot_str_eq(i64, i64, i64) -> i64\n"
    "func.func private @__flx_map_new() -> !llvm.ptr\n"
    "func.func private @__flx_map_set(!llvm.ptr, !llvm.ptr, i64, i64)\n"
    "func.func private @__flx_map_get(!llvm.ptr, !llvm.ptr, i64, !llvm.ptr) -> i64\n"
    "func.func private @__flx_map_has(!llvm.ptr, !llvm.ptr, i64) -> i64\n"
    "func.func private @__flx_map_len(!llvm.ptr) -> i64\n"
    "func.func private @__flx_map_remove(!llvm.ptr, !llvm.ptr, i64)\n"
    "func.func private @__flx_map_keys(!llvm.ptr) -> !llvm.ptr\n"
    "func.func private @__flx_map_values(!llvm.ptr) -> !llvm.ptr\n"
    "func.func private @__flx_byte_at(!llvm.ptr, i64, i64) -> i64\n"
    "func.func private @__flx_substr(!llvm.ptr, i64, i64, i64, !llvm.ptr)\n"
    "func.func private @__flx_from_byte(i64, !llvm.ptr)\n"
    "func.func private @__flx_from_bytes(!llvm.ptr, !llvm.ptr)\n"
    "func.func private @__flx_bytes_push(!llvm.ptr, i64)\n"
    "func.func private @__flx_bytes_to_hex(!llvm.ptr, !llvm.ptr)\n"
    "func.func private @__flx_parse_f64(!llvm.ptr) -> f64\n"
    "func.func private @__flx_f64_fixed(f64, i64, !llvm.ptr)\n"
    "func.func private @__flx_i64_to_hex(i64, !llvm.ptr)\n"
    "func.func private @__flx_i64_to_unsigned(i64, !llvm.ptr)\n"
    "func.func private @__flx_argv() -> !llvm.ptr\n"
    "func.func private @__flx_f64_to_str(f64, !llvm.ptr)\n"
    "func.func private @__flx_f64_to_i64(f64) -> i64\n"
    "func.func private @__flx_int_to_int(i64, i64, i64, i64, i64) -> i64\n"
    "func.func private @__flx_f64_to_int(f64, i64, i64) -> i64\n"
)
