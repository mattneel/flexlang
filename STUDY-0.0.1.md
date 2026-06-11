# Flex 0.0.1 Blind Usability Study

**Surface:** flexlang 0.0.1 @ 84ef41e (pinned wheel) · **Participants:** 12 source-blind (docs+errors only; no compiler source) · **Analyst:** unblinded (spec+source+probes)

## Headline

Flex 0.0.1's toolchain is astonishingly trustworthy for its age — all 12 tasks shipped working, tested programs, with byte-identical interpreter/native/binary output everywhere code compiled — but every single task finished "with workarounds" because the language is missing its data layer: no usable collections, recursive ADTs crash the checker, no floats, and a stdlib so thin that printing requires reading the FFI chapter. 10 of 12 participants would switch back today, yet nearly every verdict ends with "I'd happily return the day collections land" — the walls are breadth, not architecture.

## Task outcomes

- **numerics** — completed_with_workarounds, 9 walls, verdict: switch_back
- **wordfreq** — completed_with_workarounds, 6 walls, verdict: switch_back
- **wc** — completed_with_workarounds, 8 walls, verdict: keep_going
- **calc** — completed_with_workarounds, 5 walls, verdict: keep_going
- **life** — completed_with_workarounds, 7 walls, verdict: switch_back
- **json** — completed_with_workarounds, 10 walls, verdict: switch_back
- **pipeline** — completed_with_workarounds, 8 walls, verdict: switch_back
- **geometry** — completed_with_workarounds, 5 walls, verdict: switch_back
- **sorting** — completed_with_workarounds, 9 walls, verdict: switch_back
- **bench** — completed_with_workarounds, 4 walls, verdict: switch_back
- **bits** — completed_with_workarounds, 9 walls, verdict: switch_back
- **interactive** — completed_with_workarounds, 13 walls, verdict: switch_back

numerics: completed_with_workarounds — no floats/sqrt/FFI-doubles forced a scale-10^4 fixed-point system with hand-written Newton isqrt. | wordfreq: completed_with_workarounds — recursive ADTs crash (PAR003) and no collections degenerated counting into O(n^2) libc strtok rescanning. | wc: completed_with_workarounds (keep_going) — no Std.Fs/argv/int-printing; shipped via libc fopen/fgetc and putchar digit emission. | calc: completed_with_workarounds (keep_going) — no character access; synthesized char-at from libc strndup + prefix equality. | life: completed_with_workarounds — no arrays/runtime-for; encoded the grid as I64 bitmask rows with div/mod bit arithmetic. | json: completed_with_workarounds — recursive types impossible and List write-only, so fixed-capacity records; union half is interpreter-only (native ADT String payload). | pipeline: completed_with_workarounds — native backend rejects String/record ADT payloads, forcing a parallel payload-free rewrite of the whole program. | geometry: completed_with_workarounds — no floats anywhere; micro-unit fixed point with manual overflow budgeting. | sorting: completed_with_workarounds — no collections, recursive types, or function-typed parameters; hardcoded a six-field record and bubble network. | bench: completed_with_workarounds — whole-second clock + no floats turned a 10-line benchmark into tick-alignment and chunk calibration. | bits: completed_with_workarounds — no bitwise ops/hex literals/byte access; XOR emulated with div/mod and bytes hardcoded out-of-band. | interactive: completed_with_workarounds — no print/read_line; fully interactive game built on hand-declared libc getchar/puts/putchar.

## Gap map (ranked by frequency × severity)

### 1. No usable collections at runtime (arrays/lists/maps; List literals are write-only; `for` is comptime-only)

`specified_but_deferred` · **blocker** · hit by 5 task(s) · size XL

MVP.md:1040 explicitly defers arrays/slices ('MVP can start with simple arrays/slices later') and std.md says collections 'wait on the allocation story' — but the half-shipped surface is a trap: [1,2,3] literals type-check as List<I64> (even as record fields) yet have no indexing, no len, no iteration; xs[0]/s[0] silently parses as a juxtaposed list literal (verified: runs and yields the wrong value with no error); and `for x in xs` — shown as valid pure code in the spec's own section 4.5 — fails with TYPE021 comptime-only, while comptime `for` can't accumulate (CT001 bans mut). Five tasks restructured their entire program around it (bitmask rows, fixed-capacity records with named slots, six-field record + bubble network), and nearly every switch-back verdict names collections as THE reason to leave.

**Suggested approach:** Ship one growable, indexable collection (Array<T> or List<T>) with literal construction, index/len/push, runtime for-in on both backends, and make the existing dead surface either work or error at the literal/index site. This is the allocation-story milestone; everything else (argv, split, sort comparators) queues behind it.

<sub>walls: life_01, life_02, life_03, json_04, json_05, json_06, sorting_09, bits_05, bits_07, calc_01, calc_03</sub>

### 2. Recursive and mutually recursive ADTs crash the checker

`specified_but_missing` · **blocker** · hit by 3 task(s) · size L

'Simple ADTs' are MVP must-support and ADTs in an ML-family language imply self-reference, but any use of a recursive type (cons list, tree, Nat) raises a Python RecursionError that driver.py:90 reports as a location-free 'error[PAR003]: input is too deeply nested' (verified: the declaration alone passes flx check; first use crashes). Mutual recursion via a record fails with TYPE001 unknown-type in one order and the self-contradictory 'field rest has type Counts, expected Counts' in the other (verified). Three tasks lost their natural data model entirely — there is no workaround, only redesign into fixed-capacity records or string rescanning.

**Suggested approach:** Two-pass type declaration resolution (declare all type names, then resolve bodies) plus cycle-aware type equality/display so self-reference can't infinitely recurse; interp side needs boxing for recursive payloads, native needs the heap story. Land the honest diagnostic (with span) first if full support trails.

<sub>walls: wordfreq_03, wordfreq_04, json_02, json_03, sorting_01, sorting_03, sorting_04</sub>

### 3. No floating point anywhere: no literals, no F64/F32 type, no FFI doubles, no sqrt

`design_gap` · **blocker** · hit by 3 task(s) · size L

The MVP type list (MVP.md:1359) is I64/U64/Bool/String/Unit — floats appear nowhere, not even in the can-defer lists (F32 only shows up in a future data-layout example), so the spec genuinely never considered numeric work. Worse, the usual escape hatch is closed: the FFI marshals only I64/I32/String/Unit, so libm's sqrt cannot be declared correctly, and a mis-typed `extern fn sqrt(x: I64) -> I64` is accepted and silently returns garbage. Three tasks (statistics, geometry, benchmarking) each independently invented fixed-point arithmetic with manual scale tracking, hand-written Newton isqrt, and bespoke decimal formatters.

**Suggested approach:** Add F64 with literals and arithmetic to both backends, F64 across the FFI (unlocking all of libm for free), and a minimal Std.Math float surface (sqrt, pi). Until then, at minimum fix the '12.5 → expected a member name' diagnostic to say floats are unsupported.

<sub>walls: numerics_01, numerics_02, numerics_03, numerics_04, geometry_01, geometry_02, geometry_03, bench_01</sub>

### 4. Native backend rejects ADT payloads carrying String or records

`backend_gap` · **blocker** · hit by 3 task(s) · size M

backend/mlir.py:457 raises 'ADT payload of type X is not supported yet' for any non-I64 payload — verified: a one-variant Msg(String) type checks clean, runs on the interpreter, and dies at native backend time with no source span. This breaks the language's headline pitch: a typed error carrying context in a Result — exactly what the spec's Result chapter teaches — runs interpreted but cannot ship natively. The pipeline participant maintained a ~200-line parallel payload-free rewrite; json's union half is interpreter-only. It is also the ONLY parity break found across 12 tasks, against otherwise byte-identical backends.

**Suggested approach:** Extend native ADT lowering to box/inline String and record payloads (records and strings already lower natively, so this is plumbing, not research). Until fixed, report it at flx check time with a span instead of at run/build time.

<sub>walls: wc_05, json_08, pipeline_08</sub>

### 5. String processing stdlib: no char/byte access, no split, no substring, no parse_int, no formatting/padding

`stdlib_gap` · **painful** · hit by 8 task(s) · size M

Std.Str is exactly length/is_empty/eq/ne/cmp (verified in src/flx/std/Std/Str.flx). Eight of twelve tasks needed more: char_at (calc built it from libc strndup + prefix equality, O(n^2)), split (wordfreq used strtok/strstr), byte access (a hard blocker for bits — its hash cannot inspect its own input, bytes were precomputed in Python), parse_int (pipeline hand-rolled strspn/atoll with an overflow guard), fixed-precision/zero-padded formatting (numerics, geometry, bench each wrote a formatter), JSON escaping (json shipped knowingly-incorrect output), and char literals (wc compared raw ASCII codes). Everything was expressible via FFI — the language core is fine — but each task paid 15-40 lines of libc plumbing for one-call operations.

**Suggested approach:** Expand Std.Str in pure Flex over the existing extern mechanism (the std.md pattern already works): char_at/byte_at, substring, starts_with, parse_int -> Result, pad/repeat, and char literals as I64 byte values. Most of this needs no allocation story; split-returning-a-list waits on collections.

<sub>walls: wordfreq_01, calc_02, json_09, bits_04, pipeline_01, numerics_07, geometry_04, wc_07</sub>

### 6. No console IO: print/println/read_line do not exist; integer printing requires recursion over putchar

`stdlib_gap` · **painful** · hit by 7 task(s) · size S

Seven tasks hit NAME001 on print/println as their first wall; the working paths — extern fn puts (shown only in the FFI chapter) and Log.info (discovered by reading examples/traits.flx) — are documented nowhere as 'this is how you print'. stdin is worse: read_line doesn't exist, fgets is unusable (no writable buffers), scanf is banned (variadic), so the interactive task parsed integers char-by-char from getchar in a 30-line function, and there is no way to print without a trailing newline (no inline prompts). Every Flex program currently opens with a block of hand-copied libc declarations before it can say hello.

**Suggested approach:** Ship Std.IO in pure Flex over the externs that already work: print/println (Show-polymorphic or String + to_str), eprintln, read_line over getchar, all uses { Process }. This is days of work with the std mechanism already in place, and it removes the single most universal first-contact wall.

<sub>walls: numerics_06, wc_02, life_04, json_10, sorting_07, bits_09, interactive_01, interactive_02, interactive_12</sub>

### 7. Working features participants could not discover: ++, to_str, unary minus, Log.info, working flx build

`implemented_but_undocumented` · **painful** · hit by 6 task(s) · size S

The most fixable gap in the study: ++ concatenation and to_str exist and work on both backends, but neither appears in std.md, and three participants (wc, life, interactive) concluded string building was impossible — and shipped degraded programs emitting ASCII via putchar — because '"a" + "b"' errors with 'expected I64' and no hint. Unary minus works, but every shipped example writes '0 - 7', so three participants designed around a limitation that does not exist. to_str was found only by grepping examples/traits.flx. docs/cli.md still marks flx build as 'stub' though it produces working 17-22KB binaries (verified), teaching two participants to distrust the docs. Each item is real, verified, and costs a docs sweep to fix.

**Suggested approach:** One 'Hello world and strings' book page covering print routes, ++, to_str, escape sequences, unary minus, and %; sweep examples to use -7; correct the cli.md status table; add 'help: use ++ for string concatenation' to the String + TYPE003 error.

<sub>walls: wc_04, wc_08, life_05, interactive_03, calc_04, calc_05, numerics_09, wordfreq_06, interactive_13</sub>

### 8. ADT variants cannot carry multi-field payloads (TYPE022)

`design_gap` · **painful** · hit by 4 task(s) · size M

Cons(String, List), NotNumeric(String, String) — table stakes in every ML-family language — fail with 'variant has a multi-field payload, which is not supported yet'. The spec only ever shows single-payload variants, so this was never designed. The wrap-in-a-record workaround is mechanical but compounds with the two ADT walls above: the wrapper record route is exactly what dies on recursive types (rank 2) and on the native backend (rank 4), so four tasks paid for this three times over.

**Suggested approach:** Allow tuple-style multi-field payloads in parser, checker, and both backends (internally: an anonymous record payload, reusing existing record lowering). Best landed together with the rank-2/rank-4 ADT work since it is the same representation decision.

<sub>walls: wordfreq_02, json_01, pipeline_02, sorting_02</sub>

### 9. assert_eq refuses Strings and anything containing a String (TYPE019)

`specified_but_missing` · **papercut** · hit by 6 task(s) · size S

The spec's section 5.2 defines assert_eq unrestricted over MVP types (String included) and promises actual/expected diffs, and std.md headlines that Std.Str gives every String real equality — yet assert_eq on Strings, or on Result<I64, E> where E merely contains a String, errors with TYPE019. Six tasks rewrote assertions as assert(a.eq(b)), losing exactly the actual-vs-expected diff that matters when a formatter is off by one character. The deliberate check sits at sema/check.py:1311; the trait Eq machinery it should dispatch through already exists.

**Suggested approach:** Route assert_eq through the trait Eq impl (strcmp is already wired for String on both backends) and render both sides via Show in failure output. Small, high-leverage, and it makes the spec's own 5.2 example suite true.

<sub>walls: numerics_05, json_07, pipeline_06, geometry_05, bench_04, bits_08</sub>

### 10. Match ergonomics: no nested constructor patterns, no literal patterns, no qualified patterns, no statement-block arm bodies

`syntax_gap` · **painful** · hit by 3 task(s) · size M

Four independent restrictions narrowed match to a fraction of its ML role: Err(NotNumeric(i)) needs two-level matching (MATCH004 at least suggests the fix); `=> {` parses as a record literal producing the baffling "expected 'with'" so every multi-statement arm becomes a named top-level helper function; construction uses InputError.Eof but patterns require bare Eof; and integer literal patterns don't parse at all. The interactive task restructured its whole game loop around integer sentinels because arms can't mutate state; pipeline and sorting scattered straight-line logic across helper functions.

**Suggested approach:** Parser/checker work, no backend changes: accept block expressions after =>, accept qualified and literal patterns, and lower nested constructor patterns to the two-level form the compiler already suggests manually.

<sub>walls: pipeline_03, pipeline_04, sorting_06, interactive_04, interactive_05, interactive_06, interactive_07</sub>

### 11. Process-input surface: no argv, Env cannot distinguish unset from empty, no Std.Random despite Random being a spec effect

`design_gap` · **painful** · hit by 3 task(s) · size M

main() is always () -> I64 in the spec — program arguments were never considered, and flx run rejects extra args at the CLI layer, so the wc tool took its filename from an environment variable in all three execution modes. Env's get_or conflates unset with empty (documented as C-like in std.md, but it costs error-message precision for config validation). Random is in the spec's effect list (MVP.md:840) yet no Std.Random exists; the guessing game seeded from unix_time (extern rand() does work).

**Suggested approach:** An arg-count/arg(i) accessor pair needs no collections and unblocks real CLI tools immediately (full argv-as-list waits on rank 1); add Env.get -> Result/Option alongside get_or; wrap libc rand in Std.Random to honor the spec's effect list.

<sub>walls: wc_06, pipeline_07, interactive_11</sub>

### 12. No unit literal and rigid statement-position if/else typing

`syntax_gap` · **papercut** · hit by 3 task(s) · size S

() is not an expression (PAR001) even though the spec's own test-lowering pseudocode writes Ok(()); an empty block parses as a record literal; and a statement-position if/else with branches of different discarded types is TYPE008 (with the span on the else-if keyword). The discovered idiom — a block ending in a dead `let` evaluates to Unit — appears in no documentation; three participants found it by guessing, and the interactive task wrapped every puts call in a say() helper just to make branches line up.

**Suggested approach:** Parse () as the Unit literal; consider coercing statement-position if/else branches to Unit when the value is discarded. Document the Unit story either way.

<sub>walls: wc_03, sorting_05, interactive_08, interactive_09</sub>

### 13. No bitwise operators, hex literals, or hex formatting

`design_gap` · **painful** · hit by 2 task(s) · size M

^ & | << >> are LEX001 unexpected-character and 0xFF lexes as '0' followed by unknown name 'xFF' — the spec never mentions bit manipulation, hostile territory for a language that says 'Ship like C'. The FNV-1a task emulated XOR one bit at a time with division/modulo plus a manual overflow proof to replace a hardware AND, and Life's bitboard rows paid an O(col) loop per bit access. Severity was task-blocking for bits, papercut for life.

**Suggested approach:** Add the standard operator set and 0x/0b literals to lexer, checker, and both backends — small, self-contained, and exactly the kind of integer-shaped work the language is otherwise already good at. Fix the 0xFF diagnostic regardless.

<sub>walls: bits_01, bits_02, bits_03, bits_06, life_06</sub>

### 14. No sub-second clock; struct-pointer FFI closes the workaround

`stdlib_gap` · **blocker** · hit by 1 task(s) · size S

Std.Time is unix_time() in whole seconds, so directly wall-clock-timing anything under a second is impossible; clock_gettime/gettimeofday are unreachable because struct pointers don't cross the FFI (FFI002 — honestly documented as 'Not yet' in ffi.md). The bench task synthesized precision from tick alignment plus clock()-calibrated chunks — 40 lines of measurement theory and a mandatory 5-second run for what Instant::now() does in one line.

**Suggested approach:** Add monotonic_ms()/monotonic_us() to Std.Time — implementable today as a tiny C-free wrapper over scalar-returning libc calls (or a shipped helper), no struct FFI needed.

<sub>walls: bench_02, bench_03</sub>

### 15. Function types do not parse, so no comparators or higher-order functions

`specified_but_missing` · **painful** · hit by 1 task(s) · size L

MVP.md:1364 lists 'function types' in the MVP type system, but `fn(I64) -> I64` as a parameter type is PAR001 'expected a type, found fn'. The sort task hardcoded its ordering into five compare-swap functions; any reuse means duplicating the network. One task hit it, but it caps the 'write like F#' promise generally.

**Suggested approach:** Parse function types and support function references as arguments (monomorphized, no closures needed initially) — enough for comparators and map/filter once collections exist.

<sub>walls: sorting_08</sub>

### 16. Generic type parameters not inferred from Result arguments (BOUND003)

`specified_but_deferred` · **painful** · hit by 1 task(s) · size M

report<T>(r: Result<T, ConfigError>) cannot infer T from its argument; MVP.md section 3.2 explicitly defers generic type inference, and BOUND003 says so plainly. The pipeline task duplicated identical reporter functions per concrete Ok type — annoying but honest staging.

**Suggested approach:** Extend inference to unify type parameters appearing inside generic argument types (Result<T,E> covers the dominant case); low urgency relative to the blockers above.

<sub>walls: pipeline_05</sub>

## Strengths (independently replicated)

- **First-class inline test blocks (flx test, assert/assert_eq, auto-discovery)** (praised by 12/12): Every participant wrote 2-6 test blocks next to their code with zero harness setup and got clean 'N passed, 0 failed' output; several called it 'better than pytest ceremony'. All 40+ test blocks across the study passed on both backends.
- **Backend parity: interpreter vs --native vs flx build binaries** (praised by 12/12): Byte-identical output across flx run, --native, and standalone 17-22KB binaries in every task where code compiled natively — the sole parity break in 12 tasks was the known ADT-payload backend gap. The bench task ran the same file 1500x faster natively with the identical output format.
- **Diagnostic quality: error codes, file:line:col spans, caret excerpts** (praised by 12/12): Participants repeatedly noted the probe-and-fix loop was fast because nearly every error pointed at the exact token; TYPE021/TYPE022 honestly saying 'not supported yet' was singled out as trust-building. (Exceptions catalogued in diagnostics notes.)
- **Effect system ergonomics** (praised by 11/12): Typically one 'uses { Process }' on main was the entire ceremony; effects propagated through call graphs without false positives, EFFECT001 came with an actionable help line, and pipeline noted signatures 'genuinely document which helpers touch the filesystem vs stdout'.
- **Imperative core: mut, while, if-as-expression, else-if chains, %, &&/||** (praised by 10/12): Newton isqrt loops, Life's neighbor counting, the calc parser, and the guessing-game loop all compiled and ran correctly on the first attempt — multiple reports used the phrase 'first try'.
- **FFI via extern fn — one line, no toolchain, identical on both backends** (praised by 9/12): Hand-declared libc externs (puts, getchar, fopen, strtok, clock, strndup...) worked first-try through ctypes interpretation AND clang-linked native code, including round-tripping FILE* through I64; it is the load-bearing workaround for almost every stdlib gap.
- **String building: ++ concatenation, to_str, Std.Str trait-dispatched eq/cmp** (praised by 7/12): Whole JSON documents, hex renderers, and output lines composed with ++/to_str with 'zero ceremony' — among the participants who discovered these features existed (three did not; see gap rank 7).
- **Records: nested construction, dot access, multi-field functional update, derive(Show)** (praised by 6/12): calc's Parsed multi-value returns, sorting's '{ s with u0 = s.u1, u1 = s.u0 }' compare-swaps, and pipeline's cfg.show() all worked exactly as documented first time.
- **flx doctor** (praised by 6/12): One command confirmed the mlir-opt/mlir-translate/clang (LLVM 22) toolchain before trying --native; every participant who used it reported zero native-run surprises afterward.
- **ADTs + match + Result + ? propagation as the failure model** (praised by 4/12): pipeline's three-variant ConfigError with ?-chained load_config and wc's Result-based open_file 'behaved correctly on the first complete draft' — the design pitch works, on the interpreter.
- **Pipe operator** (praised by 3/12): bits' eight-stage '2166136261 |> step(102) |> ...' hash chain and sorting's 's |> pass |> pass' read exactly as intended with first-argument insertion working per spec.

## Adoption signal

keep going: **2** · switch back: **10** · undecided: **0**

Switch-back reasons are remarkably uniform and conditional, not terminal: no collections/recursive types (named in 7 of 10 switch-back verdicts), no floats (3), stdlib too thin to print/parse/format without libc golf (5), and the native backend rejecting the language's own headline Result-with-payload design (1). Every single switch-back verdict volunteers that they would return — 'the day collections land', 'once floats and a real Std.Time land', 'after ADT String payloads land natively'. The two keep-going verdicts (wc, calc) came from tasks that fit the current sweet spot: integer-and-FFI-shaped work with a fast edit-check-run loop. Universal across all 12: trust in the toolchain itself — tests, parity, diagnostics, doctor 'never once misbehaved' — which is the inverse of most 0.0.1 languages, where the surface is broad and the foundations are rotten.

## Diagnostics worth fixing regardless of features

- PAR003 'input is too deeply nested' is a bare Python RecursionError catch (driver.py:90) with no file/line — it fires when a recursive TYPE is used (the declaration alone passes flx check, verified), names the parser while flx parse succeeds, and cost two participants long blind bisections. Worth a span + honest 'recursive types are not supported yet' message even before the feature lands.
- TYPE003 'field rest has type Counts, expected Counts' compares an unresolved forward-reference placeholder against the real type using identical names — reads as a compiler self-contradiction and made two participants hunt for nonexistent bugs in their own code.
- Float literal 12.5 → PAR001 'expected a member name, found 5' and 0xFF → NAME001 'unknown name xFF': both should say the literal form is unsupported; three participants burned probe cycles deducing that floats/hex simply don't exist.
- '"a" + "b"' → TYPE003 'left operand of + has type String, expected I64' with no hint that ++ exists — directly caused three participants to ship putchar-based output believing string concatenation was impossible. Add 'help: use ++ to concatenate strings'.
- s[0]/xs[0] silently parses as two statements (binding the whole value, then a dead list literal) in let position — verified to run and produce wrong values with no warning — yet is a hard PAR001 in argument position. Either support indexing or reject the juxtaposition everywhere.
- '=> {' in match arms parses as a record literal, producing "expected 'with'" — no hint that arm bodies cannot be blocks; cost one participant a bisection session and forced game-loop restructuring in another.
- Reserved keywords (notably 'target', reserved for build.flx) produce 'expected a parameter name, found target' without saying the word is reserved — hit twice, both read it as a parser bug.
- flx run prints nothing on success and communicates main's result only via exit code — two participants initially read a successful run as a tool crash; a one-line '(exit N)' or documented convention would fix it.
- Native-backend rejections ('ADT payload of type X is not supported yet') surface at run/build time with no source span after flx check passed clean — the check/backend capability mismatch should be reported at check time.
- import Std.Fs → MOD001 'cannot find imported module /your/project/Std/Fs.flx' points into the user's project instead of saying the stdlib has no Fs module yet.
- A mis-typed extern (sqrt declared over I64) is accepted and silently returns garbage — consistent with the documented trust-declaration model, but a known-signature lint for common libc/libm symbols would catch the footgun the numerics participant only found via exit codes.
- TYPE008 'if branches have mismatched types' anchors its span on the else-if keyword rather than the offending branch tail, slowing diagnosis in long chains.

## Suggested roadmap

Milestone 1 — Truth-telling and table stakes (days-to-weeks, all S items): fix docs and small surfaces that hit 6-7 tasks each at trivial cost. Ship Std.IO (print/println/read_line — gap 6, hit by 7), the strings-and-printing book page plus example sweep for ++/to_str/unary-minus/flx-build status (gap 7, hit by 6), assert_eq over trait Eq with Show diffs (gap 9, hit by 6), the unit literal (gap 12), Std.Time monotonic_ms (gap 14), and the diagnostics batch (PAR003 span, ++ hint, floats/hex 'unsupported' wording, reserved-word message, check-time backend errors, flx run exit-code line). Rationale: roughly half of all walls in the study are S-sized polish; this milestone converts the 'pleasant up to the edge' experience into one where the edge is visible before you fall off it. Milestone 2 — Make ADTs whole (the same representation decision three ways): recursive/mutual type resolution (gap 2, blocker x3), multi-field variant payloads (gap 8, painful x4), native String/record ADT payloads (gap 4, blocker x3 and the only parity break found), plus match ergonomics (gap 10, painful x3) while the pattern code is open. Rationale: 7 distinct tasks hit this cluster; finishing it makes the spec's own Result-with-context pitch shippable natively, which is the credibility of 'Ship like C'. Milestone 3 — The data layer: one growable indexable collection with runtime for-in on both backends (gap 1 — the blocker hit by 5 tasks and the single most-cited switch-back reason), the Std.Str expansion (gap 5, hit by 8 — char_at/parse_int/pad can even land early since they need no allocator), and argv via arg(i)/arg_count then argv-as-list (gap 11). Rationale: this is the explicit re-entry condition in 7 of 10 switch-back verdicts; it converts every study task from 'workaround' to 'natural'. Milestone 4 — Numerics and abstraction: F64 with literals, arithmetic, and FFI doubles unlocking libm (gap 3, blocker x3 — placed after collections because it dominates fewer verdicts and is a larger isolated lift), bitwise ops and hex literals (gap 13), then function-typed parameters (gap 15) and Result-argument generic inference (gap 16) so collections gain map/filter/sort-by. Rationale: closes the remaining domain blockers in descending hitBy order once the universally-cited gaps are gone.
