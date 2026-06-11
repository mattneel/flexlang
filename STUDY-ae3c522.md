# Blind Usability Study v3 — flexlang @ ae3c522

*2026-06-15 · 12 source-blind participants · pinned wheel `flexlang-0.0.1-py3-none-any.whl` built from `ae3c522` (post M1–M4) · unblinded analyst synthesis · raw data: `/tmp/flexstudy-v3/study-raw.json` (ephemeral)*

## Methodology

Identical to [v2](STUDY-0.0.1.md) (owner-endorsed): participants run ONLY the pinned
wheel via `uvx` plus a copy of the shipped docs, never the repo — source-blindness is
mechanical. Each builds a realistic weekend project (word frequency, statistics, a
gradebook, a calculator REPL, Game of Life, a vending-machine state machine, an
order-parametric sorting kata, a CSV summarizer, FNV-1a + Caesar ciphers, mini-JSON,
a TODO CLI, a fib benchmark), records every wall in the standardized schema
(wall_id/phase/severity/expected/code/command/diagnostic/workaround/cost/status),
records strengths with evidence, and answers the keep-going/switch-back baseline.
The unblinded analyst dedupes walls into gaps, classifies each with the 8-tag
taxonomy, ranks by frequency × severity, and checks v2's gaps for fixes/regressions.
Two parser-shaped tasks (P8, P10) ran through a resource-capped wrapper after their
runaway scan loops twice took the host VM down — an ops note, not a language wall.

## Headline

v3 flips the adoption signal: 11/12 keep going (v2: 2/12), 12/12 tasks shipped, and zero backend-parity breaks across 64 walls — M1-M4 (Std.IO, whole ADTs, List<T>, F64/bitwise/function-values) all held under stress, including 50k-row and 100k-line differential runs that were bit-identical. The new top gap class is self-inflicted velocity debt: the compiler outgrew its docs (10/12 hit stale or missing documentation; strings.md still claims floats/split/hex/bitwise don't exist) and its diagnostics (absent features error as typos instead of saying "not yet, use Y"). The deepest genuine design flaw is small and surgical: Std.IO.read_line returning "" for both blank line and EOF silently drops data in 4/12 tasks with no in-language fix. The lone switch-back (P2, numerics) names an exact re-entry price — parse_float + float formatting — and the next adoption cliff is already visible in P1's 130-line dict workaround: the associative collection.

## Adoption

keep_going: 11 (P1, P3, P4, P5, P6, P7, P8, P9, P10, P11, P12) · switch_back: 1 (P2) · undecided: 0. Every keep-going verdict cites the same trust core — byte-identical backends, first-class tests, 23-28KB binaries, first-try correctness — and almost every one carries an explicit condition for bigger-than-weekend use: P1 leaves "the moment the task got data-heavy" (no dict/map), P3 would return to F# "for anything bigger than a weekend" (no lambdas, no float formatting), P4 leaves when he needs "tuples, catchable errors, or a REPL that survives hostile input", P9's "deal-breaker-in-waiting" is byte construction + silent escape mangling, P5 won't port a real game (interpreter 413x gap). P2's switch-back is conditional and priced: "I'd check back after a parse_float/format-control milestone; the foundation has earned that." Versus v2 (2 keep / 10 switch, all conditional on collections): the collections bet paid off exactly as v2 predicted.

## Outcomes

completed: 3 (P5 game-of-life, P7 sorting kata, P10 mini-JSON) · completed_with_workarounds: 9 (P1, P2, P3, P4, P6, P8, P9, P11, P12) · failed: 0. 64 walls total, mean 5.3/participant (v2 had 84+ with multiple program-restructuring blockers). Wall severity collapsed: 1 L (interpreter recursion ceiling), 17 M, 46 S; workaround cost was "trivial" for 36 of 64. No wall in v3 forced abandoning a task's natural data model — the v2 signature failure mode — and the only wall with no in-language workaround at all was read_line EOF conflation (p2-w4, p8-w2).

| P | Task | Outcome | Walls | Verdict |
|---|------|---------|-------|---------|
| P1 | Word Frequency | completed with workarounds | 8 | keep going |
| P2 | Statistics | completed with workarounds | 6 | switch back |
| P3 | Gradebook | completed with workarounds | 5 | keep going |
| P4 | Calculator | completed with workarounds | 7 | keep going |
| P5 | Game Of Life | completed | 5 | keep going |
| P6 | State Machine | completed with workarounds | 3 | keep going |
| P7 | Sorting Kata | completed | 4 | keep going |
| P8 | Csv Summarizer | completed with workarounds | 7 | keep going |
| P9 | Hash + Cipher | completed with workarounds | 4 | keep going |
| P10 | Mini-Json | completed | 6 | keep going |
| P11 | Todo Cli | completed with workarounds | 5 | keep going |
| P12 | Benchmark | completed with workarounds | 4 | keep going |

## What worked (praised with evidence)

- **Interpreter/native/binary parity — the headline promise, verified adversarially** — 12/12. All 12 participants independently diffed backends and found byte-identical output everywhere: P8's 50,000-row CSV with FFI atof float accumulation bit-identical; P2's 100k-line stats to the last float digit; P9 found even runtime panic text identical ('index 99 out of bounds (len 2)', same exit code); P6 diffed twice; P4: 'the only divergence I found all day was the documented interpreter stack guard'. v2's sole parity break (native ADT String payloads) is gone.
- **First-class test blocks (flx test / flx test --native, zero config)** — 11/12. P1-P5, P7-P12 all shipped 3-10 inline test blocks (60+ across the study, all green on both backends). P7: deliberate-failure output 'assert_eq failed: actual 4, expected 5' is 'exactly the actionable output a CI needs'; P4's precedence suite and P9's -30..30 round-trip sweep ran unchanged natively; string assert_eq via Std.Str (an M1 deliverable) is load-bearing in 8 tasks.
- **Diagnostics with exact spans that teach the language model** — 10/12. P3 on NAME003: 'it named the violated rule, the effect, and the exact fix'; P6 on MATCH001/MATCH002: 'names the exact constructor I forgot... this is Elm-quality and it is the reason I am here'; P10 on DER001: 'turned a potential debugging session into a 10-second decision'; P11 on MOD001's '(yet)': 'ended my persistence search in one shot'; P9 got the precise edit from TYPE003/EFFECT001 help lines.
- **flx build standalone binaries** — 9/12. P2, P4, P5, P7, P8, P9, P10, P11, P12 each produced a working 23-28KB ELF on the first attempt with no project file. P7: 'that is the Go deployment story'; P11 shipped a genuinely persistent CLI tool ('no Python in sight'); P12's binary reproduced --native benchmark numbers exactly.
- **First-contact correctness: non-trivial programs pass check and run right on the first attempt** — 8/12. P2's ~200-line stats program, P4's ~230-line parser (3 ADTs, 6 mutually recursive fns), P10's 250-line JSON parser, and P6's 86-line state machine all passed flx check and ran correctly on the literal first try, written blind from docs. P2: 'I never once fought the typechecker'; P4: 'for a 0.0.1 language learned from docs in an hour, that is remarkable.'
- **Recursive ADTs + exhaustive match + Option/Result + ? (the M2 cluster, now whole)** — 6/12. P4's Expr AST with multi-field payloads and nested constructor patterns 'compiled and ran exactly as adts.md promised'; P10's mutually recursive Json/Member through generic List payloads worked on first check; P6 got refining literal patterns and unreachable-arm detection; P2/P8 built all error paths on Option/Result; P4: eval() with ? 'reads like Rust.'
- **Docs 'Not yet (so you stop looking)' sections pre-empting walls** — 5/12. P3, P5, P7, P8, P11 all designed around documented absences (no sort, no xs[i]=v, no closures, no parse_float) before hitting a single compile error. P7: 'I never lost time to an undocumented behavior'; P5: 'three potential walls I routed around at design time.' The stale strings.md page (gap 1) is the one counterexample everyone also hit.
- **FFI as a documented, effect-checked escape hatch, identical on ctypes interpreter and clang-linked native** — 4/12. P1 read stdin to true EOF via getchar; P8 closed both his stdlib gaps (atof, write(2) stderr) with one-line externs that 'worked first try on both backends'; P11 built real file persistence from fopen/fputs/fclose with FILE* riding as I64; the effect system forced honest uses {} declarations throughout.
- **Effect annotations as guidance rather than friction** — 5/12. P1 and P12 wrote correct uses {} clauses from the std.md per-module effect tables and never saw an EFFECT001 all session; P5: 'only print_grid and main needed uses { Log }... honest and low-friction'; P11: 'no EFFECT001 whack-a-mole' across a 200-line program.
- **List<T> reference semantics + generics doing real work** — 4/12. P5's double-buffered Game-of-Life swap 'worked because list values are references — zero copies, exactly as lists.md described'; P1's List<List<I64>> parsed with no >> lexing trouble; P12's memo table mutated through call boundaries by design; P3's map/fold monomorphized over user records with zero annotations.
- **Pure function values (M4) powering real higher-order designs** — 3/12. P7's generic sort_by<T> took comparators through two call layers and monomorphized for I64 and String in one program; P12 built a bench harness over (I64) -> I64 values that LLVM devirtualized to ~4.3ns/call; P3 ran pipelines into fold/map. v2's gap 15 (function types don't parse) is emphatically closed.
- **uvx zero-install toolchain + flx doctor** — 3/12. P1, P4, P12: dozens of check/test/run/build invocations from a wheel with no clone, venv, or LLVM setup — 'including the native backend shelling out to LLVM 22 transparently' (P4); P2: flx doctor 'printed exactly which mlir-opt/mlir-translate/clang it resolved with versions.'

## The gap map (frequency × severity, analyst-classified)

### 1. The docs lag the compiler: stale 'Not yet' lists, contradictory chapters, and undocumented working syntax
`implemented_but_undocumented` · severity S · hit by 10 · walls: p1-w7, p1-w8, p2-w6, p4-w1, p4-w2, p5-w4, p7-w4, p8-w5, p8-w7, p9-w4, p10-w1, p10-w2, p10-w3, p11-w5, p12-w4

The single most-hit gap in the study, and pure velocity debt from M1-M4. Verified in source: docs/strings.md:75-79 still says floats, split, hex literals, and bitwise operators 'are not in the language yet' — all shipped in M3/M4 and documented in numerics.md/std.md/lists.md; ffi.md omits F64 from the ABI story though numerics.md documents doubles crossing it. Meanwhile working features are invisible: unary minus (docs' own examples write '0 - 1', causing 4 participants to write defensive arithmetic), else-less statement if (adts.md's lone example implies a dummy else is mandatory — P1, P8, P11 cargo-culted 'else { 0 }' across ~20 sites), else-if chains, '!', the escape set, and substr's third argument (source says 'count' at Std/Str.flx:29; the docs' one example is ambiguous, forcing P2, P4, P10 to write disambiguation probes — P10: 'had I guessed wrong, every parsed string would have been silently truncated'). P7 under-designed v1 because numerics.md:91's 'Generic functions can't be passed' reads as 'generics and fn values don't mix'. 15 walls, all S, all costing probe programs.

**Fix shape:** One documentation truth pass, no compiler changes: rewrite strings.md's Not-yet to the current truth; add a 'syntax you can rely on' page (unary minus, !, else-if, else-less if, escape set \n \t \r \" \\, substr(s, start, count), record-literal form); fix the ffi.md ABI table; sweep examples to use -7 (same fix v2 prescribed — it regressed). Then make it stay true: doc examples as CI doctests.

### 2. Absent features error as typos: the parser/checker doesn't say 'Flex doesn't have X yet — use Y'
`diagnostic_gap` · severity S · hit by 9 · walls: p1-w4, p1-w5, p3-w1, p4-w5, p5-w2, p7-w1, p7-w3, p8-w1, p10-w4, p11-w1, p12-w1

v2's most-praised diagnostic trait — TYPE021/TYPE022 honestly saying 'not supported yet' — did not scale to the M3/M4 surface. 'break' → NAME001 'unknown name' ('reads like a typo'd identifier, not loops-have-no-break' — P1, P4); lambda/fn literals → bare PAR001 (P3: 'unlike the language's usual helpful diagnostics'; P7); xs[i] = v → PAR001 'expected an expression, found =' with 'zero hint toward List.set' (P5, P7); Type { ... } record construction → NAME001/PAR001 with no pointer to the bare { ... } form, sending P8 and P11 grepping the MVP spec; match-arm commas → PAR001 that 'didn't say match arms take no commas' (P12); '.split on String' → TYPE010 calling a method a 'field' (P1); P10's malformed derive misparsed and errored 7 lines below the mistake. The counterexample proving the pattern works: NAME003's 'cannot be passed as a value (function types are pure)... help: call it directly' was singled out as teaching the language (P3).

**Fix shape:** A curated batch of ~8 special-case hints in parser/checker, in the existing NAME003/DER001 style: keyword 'break'/'continue' → 'Flex has no break yet; use return or a flag'; 'fn'/'fun' in expression position → 'no lambdas yet; pass a named function'; '=' after index expression → 'use List.set(xs, i, v)'; UpperIdent followed by '{' → 'record literals are bare { field = ... }'; ',' after match arm → 'arms are newline-separated'; method call on non-trait fn → name the trait/free-function distinction. Days of work, hits 9 participants.

### 3. Std.IO.read_line conflates blank line with EOF — silent data loss with no in-language fix
`design_gap` · severity M · hit by 4 · walls: p1-w2, p2-w4, p4-w3, p8-w2

The deepest genuine API design flaw in v3. Std/IO.flx:4-11 documents '"" at end of input', making a blank line indistinguishable from EOF. All four stdin tasks lost data silently: P1's wordfreq dropped everything after a paragraph break; P2's stats printed 'count 2' for a 4-number file; P4's REPL had to redefine its contract ('skip blank lines, stop at EOF is unimplementable: it either stops early or loops forever'); P8 couldn't even count the dropped row as failed. P8 nails the irony: 'this is exactly the in-band-signaling bug class Option exists to kill' — in a language whose own parse_int returns Option. No workaround exists inside the language (P2: 'none possible within the documented Std.IO API'); P1 escaped only by bypassing Std.IO entirely via extern getchar, which works solely because his data stayed bytes (see gap 4).

**Fix shape:** One stdlib function + two backend intrinsics: read_line() -> Option<String> (None at EOF, Some("") for blank), or additively read_line_opt() with the old form documented as a footgun. The interpreter side is sys.stdin.readline()'s ''-vs-'\n' distinction, already available; native runtime reads the same. Smallest M-severity fix in the study relative to harm.

### 4. No byte-to-String construction: chr()/from_byte/from_bytes don't exist
`stdlib_gap` · severity M · hit by 4 · walls: p1-w3, p9-w1 (and named as the blocking sub-cause inside p8-w2 and p11-w2)

byte_at deconstructs strings but nothing reconstructs them. P1, forced onto getchar by gap 3, could never rebuild words as Strings — his entire output path dropped to putchar with a hand-rolled digit printer, 'losing the entire ++/to_str/println ecosystem'. P9 (security tooling) built hex rendering and Caesar shifts via lookup-table strings indexed with char_at, and states the ceiling precisely: 'an XOR/RC4-style cipher emitting arbitrary bytes would be unwritable'. The gap also hardens gap 3 into a wall: P8 'can't write my own reader over extern getchar() because Flex has no way to construct a String from collected bytes', and P11's file read-back via fgets is impossible for the same reason. Std/Str.flx surface verified: nothing byte→string.

**Fix shape:** Std.Str.from_byte(b: I64) -> String and from_bytes(bs: List<I64>) -> String, pure Flex over one runtime intrinsic per backend (the native runtime already allocates strings; the interpreter is one bytes() call). Unlocks user-space readers, ciphers, binary emitters, and the fgets buffer pattern. Pairs with \xNN escapes (gap 16).

### 5. No numeric/text formatting: float precision control, padding — and to_str's 'shortest round-trip' claim is false
`specified_but_deferred` · severity M · hit by 4 · walls: p2-w3, p3-w2, p3-w5, p8-w6, p12-w3

numerics.md:98 honestly defers parse_float/format control, but four tasks were table-printing tasks: P3 hand-rolled round-half-up tenths math ('the kind of code nobody should write in 2026') plus a while-loop pad_right; P12's benchmark column mixed '5e-06', '0.000422', and '3772' and he rebuilt fixed-point ns formatting by hand; P2 wrapped to_str in floor-detection; P8 shipped 'mean: 2.3333333333333335' with no recourse. Separately, a truth bug shipped with M4: numerics.md:25/28 and the README claim to_str prints the 'shortest text that parses back exactly', but interp.py:71's %g precision loop prints '1e+01' for 10.0 — '100' is shorter and exact (P2). It's shortest-within-%g, not shortest.

**Fix shape:** Std.Fmt or Std.Str additions: to_str_fixed(x: F64, decimals: I64), pad_left/pad_right(s, width) — pure Flex over an snprintf-shaped intrinsic. Fix the to_str claim either way: switch to a true shortest algorithm (Python repr on the interpreter side already is one; native needs the matching cutover) or re-document as '%g-style shortest'. The doc-claim fix is one line and belongs in the next truth pass.

### 6. Composite equality and Show: lists and String-carrying ADTs can't be compared, derived, or rendered in test failures
`stdlib_gap` · severity M · hit by 4 · walls: p5-w5, p6-w3, p7-w2, p10-w5

Four tasks rebuilt equality or display by hand. P7 (TYPE019: 'assert_eq is not supported for List<I64>') wrote list_to_str and predicted 'every Flex program that touches lists will rewrite this exact helper'; P5 expanded one grid-row snapshot assert into 6-8 per-cell asserts; P10 hand-wrote ~60 lines of json_eq/arr_eq/obj_eq because derive(Eq) refuses String-carrying variants (deliberate check at macro/derive.py:99 — records with String fields work per std.md:44, ADT variants don't); P6 derived Show specifically so failing asserts would show states, but a failing ADT assert_eq prints only 'assertion failed' — he had to assert on .show() strings instead. This is the unfinished half of v2 gap 9 (assert_eq over trait Eq with Show diffs): strings and word-payload ADTs landed, composites didn't.

**Fix shape:** Three increments sharing machinery: (1) extend derive(Eq)/derive(Show) to String payloads and recursive/List payloads (strcmp and the existing show impls are already wired); (2) structural ==/assert_eq/Show for List<T> where T has Eq/Show; (3) route assert_eq failure rendering through Show when an impl exists — finishing the v2 prescription. P6's framing is exactly right: 'diagnostics gaps on a sound core.'

### 7. Interpreter recursion ceiling (2000 frames, no TCO) — the only behavioral backend divergence in the study
`backend_gap` · severity L · hit by 2 · walls: p4-w4, p10-w6

interp.py:37 pins _DEPTH_LIMIT = 2000 while native gets TCO plus the OS stack, so the same program on the same input printed '601' natively and died with 'stack overflow (recursion too deep)' interpreted (P4's recursive lexer at 600 +1 terms) — the study's sole same-input/different-output case, and the one L-severity wall. P4 paid a 'painful' full rewrite to iterative loops (only possible after discovering undocumented return), and since panics are uncatchable, 'one hostile line kills the entire REPL process under the interpreter'. P10 measured the envelope deliberately (depth 600 ok / 1000 dies interpreted; 1000 ok natively, graceful at 100000) and accepted it as documented. Recursive descent is the natural idiom this language's own ADT story teaches.

**Fix shape:** Self-tail-call elimination in the tree-walker (covers the lexer/parser accumulator shape that hit P4), or a configurable guard (--max-depth / env var) with the limit stated in cli.md as a named parity exception. Native's clean guard message (f87c459 shipped it) is the model — the interpreter just needs more headroom, not different semantics.

### 8. Interpreter throughput: 65x-18,000x slower than native on byte/cell loops
`backend_gap` · severity M · hit by 3 · walls: p1-w6, p5-w3, p9-w3 (ratios corroborated in P2, P8, P12 worked-well notes)

Three walls, three more measured corroborations: P1's 5,000-word corpus took 17s interpreted vs 0.47s native; P5's 64x64 Life burned 2m45s vs 402ms (413x — 'the default path is unusable for game-sim workloads'); P9 hashed at ~160KB/s vs ~500MB/s. P2 (65x), P8 (34x), P12 (geomean ~5,300x) measured the same thing without filing it as a wall. Nobody found wrong answers — parity held everywhere — and P12 even praised the interpreter as fast enough to prototype on. The cost is real but bounded: every affected participant's workaround was one flag.

**Fix shape:** Cheapest first: truth-telling — state the expected magnitude in cli.md/README and have flx run print a one-line '(tip: --native)' when wall time crosses a threshold. Then a hot-path pass on the tree-walker (byte_at/List.set/arithmetic nodes; P1's and P9's profiles are pure inner-loop dispatch). Auto-native-when-toolchain-present is a plausible later default but changes the no-toolchain pitch.

### 9. System surface: no stderr, no file IO, ms-only clock
`stdlib_gap` · severity M · hit by 3 · walls: p8-w4, p11-w2, p12-w2

Three different participants hit three sides of the same 'talk to the OS' thinness. P8 needed stderr so errors don't pollute piped stdout — Std.IO has no eprintln (his 6-line write(2) extern wrapper is effectively the patch). P11's todo CLI found Std.Fs honestly absent (MOD001 '(yet)'; README defers it on the allocation story) and built real persistence from fopen/fputs/fclose externs, but read-back required a stdin-redirect convention because fgets needs gap 4. P12 couldn't time sub-ms native work: Std/Time.flx (verified) is exactly unix_time() + monotonic_ms(), so he built inner-loop batching with argv-plumbed scale factors. All three workarounds shipped, all three are things a stdlib exists to own.

**Fix shape:** Three small, independent items: Std.IO.eprintln (libc write wrapper, uses { Log }); Std.Time.monotonic_ns (same intrinsic shape as monotonic_ms — v2 gap 14's prescription, half-landed); minimal Std.Fs read_to_string/write_string (the fopen/fputs path P11 proved works on both backends — from_bytes from gap 4 completes the read side). None requires the allocation story.

### 10. parse_int silently wraps on overflow, contradicting its Option contract
`design_gap` · severity M · hit by 2 · walls: p2-w2, p4-w7

Std/Str.flx:73-74's source comment declares 'values beyond I64 wrap (64-bit two's complement, like arithmetic)' — deliberate, but stated nowhere in user docs, which promise 'Some(-42); None on empty or non-digit input'. P2's stats program printed a mean wrong by 18 orders of magnitude with no warning (2^64+1 wraps positive past his sign guard) and he had to add a digit-count cap plus a regression test; P4's calculator printed 1864712049423024128 for an over-64-bit numeral and consciously accepted it as the language's integer model. A function that already returns Option<I64> answering 'unrepresentable' with a wrapped Some is in-band wrongness in a language whose pitch is no silent surprises.

**Fix shape:** Return None on overflow: a checked accumulate (if value > (MAX - d) / 10 { ok = false }) or P2's own 18-digit cap, ~6 lines in pure Flex in Std/Str.flx, plus one doc line. If wrap is kept as policy, it must move from source comment to strings.md/std.md — but both v3 victims expected None.

### 11. No parse_float — the read-half of F64 didn't ship with M4
`specified_but_deferred` · severity M · hit by 2 · walls: p2-w1, p8-w3

numerics.md:98 defers it honestly, but both float-input tasks paid heavily and dangerously: P2 spent ~60 lines hand-rolling a decimal parser that is not correctly rounded (his 100k-line mean drifted in the last 2 ulps vs strtod) and rejects exponent notation — 'I spent my weekend rebuilding strtod (badly) instead of doing statistics', the study's only switch-back driver. P8 went the other way — extern atof — and had to write a ~30-line byte validator because atof returns 0.0 on garbage and can't signal failure. F64 exists, libm exists, to_str exists; the language can compute and print floats it cannot safely read.

**Fix shape:** Std.Str.parse_float(s: String) -> Option<F64>, correctly rounded, via a runtime strtod-with-endptr intrinsic on both backends (ctypes strtod + native libc call); accept exponent notation. This plus gap 5's formatting is P2's stated re-entry condition verbatim.

### 12. No tuples, no multi-scrutinee match, and nested-pattern usefulness analysis missing
`specified_but_deferred` · severity M · hit by 2 · walls: p6-w1, p6-w2 (tuples also named in P4's final summary and adoption verdict)

P6 (the Elm refugee) wanted the canonical match (e, s) state-machine table; tuples don't parse (and appear nowhere in MVP.md — never specified), and the fallback pair-ADT match dies on MATCH001 because 'arms with literal or nested sub-patterns don't count toward coverage' (adts.md:128-129 documents the deferral; the help text states the rule precisely). He refused the catch-all that 'would defeat the whole point of exhaustiveness' and restructured into nested matches — safety preserved, 2D table shape lost. P4 invented single-variant PR ADTs for multi-returns (as did P10, who filed it as a worked-well!) and names tuples in his leave-condition. Severity M is P6's own rating; the workaround ceiling is real but the safety property survives.

**Fix shape:** Usefulness analysis for nested constructor patterns first (pure checker work — lower nested arms to decision rows before coverage counting); it makes pair-ADT 2D matching first-class and is the M2 match-ergonomics tail. Tuple types are a bigger spec decision and can trail; the single-variant-ADT idiom (P10) is good enough to document as the interim pattern.

### 13. List surface: no sort, pop, remove, slice
`specified_but_deferred` · severity M · hit by 2 · walls: p3-w4, p11-w3

lists.md:111-114 defers these on the allocation story. P3 wrote 16 lines of in-place selection sort for his report table (correct first try thanks to reference semantics, but 'a comparator-taking generic sort is impossible to write nicely anyway' pre-M4-fn-values — note P7's generic sort_by<T> now disproves the impossibility); P11 rebuilt the list to fake remove. The deferral rationale has aged out for these specific operations: sort_by(less) needs no new allocation primitives (P7 wrote one in user space on existing List.set/push), and remove/pop are length bookkeeping on the existing growable representation.

**Fix shape:** Std.List.sort_by(xs, less: (T, T) -> Bool) in pure Flex (P7's bubble-back insertion sort is a working draft), pop, remove(i); slice can wait for regions if it implies views. Function values shipping in M4 removed the original blocker — this is now stdlib authoring, not language work.

### 14. No lambdas, no closures; function values must be pure
`specified_but_deferred` · severity S · hit by 3 · walls: p3-w1, p3-w3, p7-w1, p11-w4

numerics.md:94/100 defers closures honestly, and every victim designed around it pre-emptively — but the tax is structural: P3 named a top-level function for every one-off fold/map step (F#-withdrawal is half his leave-condition); P7 calls it 'pre-generics Go again' and shipped anyway; P11 identifies the sharp edge precisely — without capture, 'higher-order style is effectively unusable for any predicate parameterized by runtime data' (his filter-by-id was unwritable, full stop). Related: function values are pure by fiat, so effectful iteration must be for-in (p3-w3 — softened by the study's best diagnostic, NAME003). All severity-S because workarounds were mechanical, but it caps the map/filter/fold investment M4 just made.

**Fix shape:** Staged: (1) non-capturing lambda literals (pure syntax sugar to a hidden top-level fn — kills p3-w1/p7-w1 at parser level); (2) capturing closures (needs the environment/allocation story — genuinely deferred); (3) consider effect-polymorphic HOFs (iter) later. Stage 1 is cheap and removes the most-probed absence; stage 2 is the real unlock for p11-w4.

### 15. No dict/map — the next data-layer cliff (low frequency, highest strategic weight)
`specified_but_deferred` · severity M · hit by 1 · walls: p1-w1

Only P1 hit it as a wall, but it is the v3 echo of v2's universal collections verdict: the README's Planned section still defers 'collections' on the allocation story, and the one task that was associative-shaped turned 'a Counter one-liner into ~130 lines of O(n*v) parallel-list bookkeeping' running 17s interpreted on 5,000 words. P1's keep-going is explicitly conditional: 'I'd switch back to Python the moment the task got data-heavy.' v2's lesson was that participants forgive absence-with-a-plan but adoption converges on the data layer; List<T> bought v3's 11/12 — Map buys the workloads (word counts, indexes, caches, JSON objects) that List can't.

**Fix shape:** One associative collection on both backends: Map<String, V> first if K-generality is the blocker (covers every observed use), with get -> Option<V>, set, len, keys iteration; hash-on-bytes reuses the existing string runtime. Same milestone shape as M3's List<T> — literal-less, function-surface-first, parity-tested.

### 16. Unknown string escapes are silently mangled (\x41 becomes 'x41')
`syntax_gap` · severity M · hit by 1 · walls: p9-w2

Verified at lexer.py:130: the escape map falls back to .get(esc, esc), silently dropping the backslash and keeping the rest literally — no error, no warning. P9 (security tooling) reverse-engineered the supported set (\n \t \\ \" — plus \r, which even he didn't find) via byte_at dumps because escapes are documented nowhere. His verdict isolates the harm class exactly: 'a literal that looks like raw bytes quietly becomes different bytes' — silent wrongness in the one language whose brand is never lying. One participant, but it's a named pre-condition of his adoption ('would have to be fixed before I trusted it with security tooling').

**Fix shape:** Three lines in the lexer: unknown escape → LEX error with the supported set in the help text. Document the set (gap 1's truth pass). Add \xNN as a real escape when from_byte (gap 4) lands, since they serve the same byte-construction need.

### 17. No xs[i] = v index assignment
`specified_but_deferred` · severity S · hit by 2 · walls: p5-w2, p7-w3

lists.md:111 defers it with the List.set alternative, and both victims (P5's Life cell writes, P7's insertion-sort swap) routed around it at design time — the residual cost is muscle-memory probes and 'clunky' three-statement swaps. Reads (xs[i], even nested g[r][c]) already work; only the write form is missing. The diagnostic half (bare PAR001 with no List.set hint) is covered by gap 2.

**Fix shape:** Pure parser sugar: lower `expr[i] = v` to List.set(expr, i, v) (nested g[r][c] = v falls out since g[r] is a reference read). No checker or backend changes; smallest syntax win available.

### 18. Loop control: no break/continue; `return` works everywhere but is documented nowhere (and needs block-wrapping in match arms)
`implemented_but_undocumented` · severity S · hit by 2 · walls: p1-w4, p4-w5, p4-w6

break/continue aren't even keywords (verified: absent from tokens.py), while `return` is a parsed keyword (tokens.py:101) that type-checks and runs correctly on both backends — yet appears in zero chapter docs, so both P1 and P4 discovered it by experimental probing and then built their real designs on it ('built the whole v2 rewrite on undocumented return' — P4). Bonus paper cut: return isn't an expression, so match arms need `None => { return Err(...) }` block-wrapping (p4-w6, PAR001). The misleading NAME001 for break is gap 2's batch.

**Fix shape:** Document return (one docs section: semantics, match-arm block form); accept `return` as a match-arm body without braces (parser one-liner); decide break/continue's fate and say so in the docs either way — both participants were fully served by return once they found it.

### 19. SIGPIPE: interpreter leaks a 60-line Python traceback where the native binary exits cleanly
`backend_gap` · severity S · hit by 1 · walls: p2-w5

`flx run stats.flx | head -1` dumps BrokenPipeError with flx/interp.py, flx/driver.py, flx/cli.py internals on stderr; the flx-built binary in the same pipeline is clean. Verified: no BrokenPipe/SIGPIPE handling exists anywhere in src/flx/. Stdout and exit codes still matched between backends so P2 filed it as cosmetic ('ignore the stderr noise'), but it's both a parity blemish on stderr behavior and the only place the study saw the Python machinery leak through the language's facade.

**Fix shape:** Catch BrokenPipeError at the cli/driver top level, close stdout, exit 141 (128+SIGPIPE) to match Unix convention and the native binary. ~5 lines in cli.py.

## Versus v2

**Confirmed fixed:** Confirmed fixed by v3 evidence (v2 rank → evidence): #1 collections (XL blocker, hit 5) → List<T> used by all 12 participants on both backends, incl. nested List<List<I64>> grids (P5, P1), generic List<Student>/List<Json> payloads (P3, P10), for-in, argv-as-list — the dict/map half remains (v3 gap 15). #2 recursive-ADT crashes (L blocker) → P4's recursive Expr AST and P10's mutually recursive Json/Member through List payloads compiled first-try and ran on both backends. #3 no floats (L blocker) → F64 + libm + FFI doubles delivered P2's bit-exact-vs-Python statistics, P8's 50k-row bit-identical sums, P3's grade math; parse_float/formatting are the residue (v3 gaps 5, 11). #4 native ADT payload parity (M, the only v2 parity break) → String-payload ADTs (P4's PErr(String), P10's JStr(String), P6's derived Show) ran natively everywhere; v3 found zero parity breaks. #5 string stdlib (hit 8) → byte_at/substr/char_at/split/parse_int powered three hand-rolled parsers (P4, P10, P8) with 'no missing primitive forced a detour anywhere in the parser' (P10); from_byte/pad are the residue. #6 no console IO (hit 7) → Std.IO print/println/read_line used universally; the EOF conflation is the residue (v3 gap 3). #8 multi-field payloads → Add(Expr, Expr), KV(String, Json), ES(Event, State) all worked. #9 assert_eq strings (hit 6) → string assert_eq with actual/expected diffs praised by P4, P6, P7, P9; composite types are the residue (v3 gap 6). #10 match ergonomics → nested constructor patterns (P4), literal patterns refining coverage (P6), block arm bodies (p4-w6 workaround used one). #11 argv → Env.argv() praised by P9, P11 ('shell quoting intact, no import needed'). #12 unit literal → P8's eprintln ends in bare `()`, no wall filed. #13 bitwise/hex (M) → P9's FNV-1a matched an independent Python reference bit-for-bit incl. wrapping multiply and masked shifts. #14 sub-second clock → monotonic_ms shipped and used (P12); ns residue in v3 gap 9. #15 function types (L) → generic sort_by<T> comparators (P7), bench harness fn params (P12), map/fold (P3). #16 generic inference → P3's map/fold inferred everything 'with zero annotations'. Net: 14 of 16 v2 gaps fully or substantially closed; the two left open (map half of #1; format-control corner of #5/#3) are v3 gaps 15 and 5.

**Regressions:** No behavioral regressions: zero parity breaks across 64 walls (v2 had one — native ADT payloads — now fixed); the interpreter's 2000-frame guard predates v2 and is documented. Two regressions of the truth-telling kind, both caused by M1-M4's own velocity: (1) DOCS ACCURACY — strings.md:75-79's 'Not yet' list was true at 84ef41e and is now false (floats, split, hex, bitwise all shipped in M3/M4); it became the single most-hit wall in v3 (7 participants: p1-w7, p2-w6, p4-w2, p8-w5, p9-w4, p10-w2, p12-w4), and the v2 report's prescribed example sweep for '0 - 7'-style negation never happened — 4 more participants designed around nonexistent limitations (p1-w8, p5-w4, p9-w4, p11-w5), the exact failure v2 gap 7 documented. (2) DIAGNOSTIC HONESTY COVERAGE — v2 participants singled out 'not supported yet' errors as trust-building; the M3/M4 surface shipped without equivalents, so v3's new absent-feature errors (break → NAME001 typo-style, lambdas → bare PAR001, xs[i]= → bare PAR001, Type{} → NAME001) regressed to the v2 era's '\"a\"+\"b\" with no hint' pattern that the v2 report explicitly warned about. (3) Minor overstated claim shipped with M4: numerics.md:25/README's 'shortest text that parses back exactly' for to_str(F64) is false at the %g cutover (10.0 → '1e+01'; interp.py:71). All three are days-not-weeks fixes — but they erode the one property every cohort praises most.

## The data-driven roadmap

### M5 — Tell the truth again — docs & diagnostics catch up to M1-M4

Rewrite strings.md's stale 'Not yet' (gap 1, hit 7 directly); 'syntax you can rely on' docs page: unary minus, !, else-if, else-less if, escape set, substr(s, start, count), bare record literals, return (gaps 1, 18); doc examples as CI doctests so drift can't recur; absent-feature diagnostic batch — break/lambda/xs[i]=/Type{}/match-comma/.method hints in the NAME003 style (gap 2, hit 9); LEX error on unknown escapes (gap 16); parse_int overflow → None + doc line (gap 10); fix the to_str 'shortest round-trip' claim (gap 5 sub-item); allow return as match-arm body (p4-w6); SIGPIPE clean exit in cli.py (gap 19).

*Rationale:* Ranks 1, 2, 10, 16, 18, 19 — over half the study's walls by frequency — are all S/M items fixable in days with zero language design risk. v3's data says the compiler outran its own documentation: 10 of 12 participants paid probe-program tax on features that already work, and the three silent-wrongness items here (escape mangling, parse_int wrap, false to_str claim) attack the exact property — 'the toolchain never lied to me once' (P1) — that every keep-going verdict is built on. This is v2-M1's playbook, which produced the cheapest wins of that cycle.

### M6 — Bytes in, bytes out — the IO correctness milestone

read_line() -> Option<String> (or read_line_opt) killing the EOF/blank conflation (gap 3, hit 4, silent data loss, no in-language workaround); Std.Str.from_byte/from_bytes (gap 4, hit 4 — unlocks user-space readers, ciphers, fgets buffers); \xNN escapes riding on from_byte (gap 16 completion); Std.IO.eprintln (gap 9 — P8's write(2) wrapper is the spec); Std.Time.monotonic_ns (gap 9, finishing v2 gap 14); minimal Std.Fs read_to_string/write_string over the fopen path P11 already proved on both backends (gap 9).

*Rationale:* Every stdin-reading task (4 of 4) silently dropped user data to the read_line design flaw — P8 names it 'exactly the in-band-signaling bug class Option exists to kill', in a language whose parse_int already returns Option. Byte construction is the named adoption pre-condition of the security persona (P9: 'real ciphers and binary emitters are unwritable') and the hidden blocker inside two other walls (p8-w2, p11-w2). None of these items needs the allocation story; together they make 'CLI tool that reads real input' — the study's dominant task shape — safe by default.

### M7 — Numbers people can show — close the float round-trip

Std.Str.parse_float -> Option<F64>, correctly rounded via strtod-with-endptr intrinsic, exponent notation accepted (gap 11, hit 2 but painful — P2's switch-back driver); to_str_fixed(x, decimals) / %.Nf-style formatting (gap 5, hit 4); pad_left/pad_right (gap 5 — P3, P12 both hand-rolled it); make to_str genuinely shortest-round-trip or finish re-documenting it (gap 5 truth item if not done in M5).

*Rationale:* The study's only switch-back (P2) priced re-entry exactly here: 'I'd check back after a parse_float/format-control milestone; the foundation has earned that.' Four participants hand-rolled formatters and two hand-rolled float parsers — one not correctly rounded (P2's last-2-ulp drift on 100k lines), one gated on atof-can't-fail (P8) — i.e., the workarounds are the rare kind that produce silently wrong numbers. M4 shipped F64 compute and print; this is the read-and-display half, small because the runtime hooks (libc, snprintf-shape) already cross both backends.

### M8 — The associative layer + finishing equality — the next data-layer bet

Map<String, V> (then Map<K, V>) with get -> Option<V>/set/len/key iteration on both backends (gap 15 — strategic #1 despite hit_by 1); Std.List.sort_by/pop/remove in pure Flex now that fn values exist (gap 13); derive(Eq)/derive(Show) for String-carrying and recursive ADTs + structural eq/Show for List<T> (gap 6); assert_eq failure rendering through Show (gap 6, completing v2 gap 9); non-capturing lambda literals as parser sugar (gap 14 stage 1); nested-pattern usefulness analysis (gap 12).

*Rationale:* v2's core lesson — adoption converges on the data layer — repeated in miniature: List<T> bought v3's 11/12, and the one associative-shaped task (P1) paid 130 lines and O(n·v) for a Counter one-liner, with a keep-going verdict explicitly conditioned on 'the moment the task got data-heavy'. Map is M3's proven playbook (function-surface-first, parity-tested) applied once more. The equality/Show items ride along because four participants hand-wrote eq/show helpers in test code, and sort/pop/lambdas convert M4's function-value investment from demo to daily driver.

## Per-participant adoption reasons

- **P1** (Python data person, curious about typed languages) — *keep going*: Keep going, for a weekend project specifically: every wall I hit had a findable workaround, the toolchain never lied to me once (interpreter and native were byte-identical on every run, including FFI and UTF-8), and tests/effects/uvx made iteration genuinely pleasant. But I'd switch back to Python the moment the task got data-heavy — the missing dict/map turned a Counter one-liner into ~130 lines of O(n*v) parallel-list bookkeeping, and stdin handling required dropping to raw libc.
- **P2** (numerical-computing engineer (Julia/Fortran background)) — *switch back*: The core is shockingly trustworthy for a 0.0.1 — bit-identical backends, first-class tests, a 24KB binary, and not one compile-error fight — but for numerical work the table stakes aren't there: I spent my weekend rebuilding strtod (badly — not correctly rounded, no exponents) around a silently-wrapping parse_int instead of doing statistics, and in Julia this whole task is ten correctly-rounded lines. I'd check back after a parse_float/format-control milestone; the foundation has earned that.
- **P3** (F# developer who loves records and pipelines) — *keep going*: Keep going — for a 0.0.1 this was shockingly smooth: my records-and-pipelines program compiled and printed the correct table on the first run, the tests were free, and the diagnostics taught me the effect rules instead of fighting me. The missing lambdas and float formatting are real F#-withdrawal pain and would send me back to F# for anything bigger than a weekend, but for a weekend project the core is trustworthy enough to stay.
- **P4** (compiler hobbyist (writes parsers for fun); judged Flex as a target for weekend language projects) — *keep going*: Keep going. The core I care about as a parser hobbyist — recursive ADTs, exhaustive match, Result + `?`, first-class tests, and an interpreter/native pair that actually agree — is sound and genuinely fun, and every wall I hit was workaroundable in minutes except the stdin EOF conflation; I'd switch back to Rust the moment I needed tuples, catchable errors, or a REPL that survives hostile input, but for a weekend language project this one earns the next weekend too.
- **P5** (game programmer, C++/Rust day job; cares about per-frame cost, double-buffered updates, shipping a binary, and tests that catch sim regressions.) — *keep going*: Keep going — for a weekend project this was the smoothest 0.x language I've touched: the whole Game of Life worked in essentially one pass, every wall was minutes-level, native parity is verifiably real, and run/test/build is a genuinely good loop. I'd keep --native on by default and I'm not porting a real game to it (413x interpreter gap, no slices, no collection equality), but as a weekend language it earned the next weekend.
- **P6** (Elm refugee who wants exhaustive matches everywhere) — *keep going*: Keep going. The one thing I left Elm-less ecosystems to keep — a compiler that names the exact constructor I forgot and refuses to ship until I handle it — works here, on a language that also compiles to a native binary with provably identical behavior; my whole task ran first-try from the docs alone. I want tuple/2D match usefulness analysis and Show-aware test failures, but those are diagnostics gaps on a sound core, not soundness gaps.
- **P7** (Go developer who values boring reliability) — *keep going*: Keep going. Everything worked the first time, the docs never lied to me, the interpreter and native backend agreed byte-for-byte, and one command gave me a real 24K binary — that is the boring reliability I moved to Go for; writing named comparators instead of lambdas is just pre-generics Go again, and I shipped a decade of software that way.
- **P8** (Rust developer, allergic to runtime surprises) — *keep going*: For a weekend project I'd keep going: the one promise I care most about — no interpreter/native divergence, checked bounds, checked effects, no UB — held even under a 50k-row float-summing stress test (bit-identical sums, 34x native speedup), and every wall I hit was stdlib breadth (parse_float, stderr, Option-returning read_line), not architecture. I'd still reach for Rust the moment the program needed real I/O surface or closures, but nothing here felt like quicksand.
- **P9** (security tooling author (loves bytes and bit twiddling); usual languages Python/Rust/Go) — *keep going*: Keep going: the bit-level semantics are exactly right (wrapping 64-bit multiply, masked shifts, hex literals, byte_at), interpreter/native parity held everywhere I probed including panic text, and tests-plus-24KB-binary is a better ship story than Python gives me. The deal-breaker-in-waiting is byte construction — no chr()/\\xNN means real ciphers and binary emitters are unwritable and the silent escape mangling (p9-w2) would have to be fixed before I trusted it with security tooling, but for a weekend project I'd stay.
- **P10** (backend developer (JSON for breakfast); daily drivers Python/Go, comfortable with F#-style ADTs) — *keep going*: Keep going — a recursive JSON parser with round-trip tests worked on the first check, ran identically interpreted and native, and compiled to a 28k binary with zero config; that's a better weekend than most young languages offer. The real costs were doc gaps (undocumented else-if/escapes, stale 'Not yet' lists, ambiguous substr) and hand-rolling 60 lines of equality that derive(Eq) should have written — annoyances, not blockers.
- **P11** (CLI tools author (argparse muscle memory); usual languages Python/Go) — *keep going*: Keep going — the core loop (check, run, test, build a 24KB binary) never lied to me once, interpreter/native parity held byte-for-byte, and the FFI was strong enough to fake the missing filesystem in twenty minutes; for a CLI tool that's already past my weekend bar. I'd miss argparse and string formatting hard (hand-rolled .eq() dispatch chains and ++ concatenation get old), but those are stdlib gaps in a 0.0.1, not design rot.
- **P12** (performance-curious tinkerer) — *keep going*: Keep going. A weekend language that hands me a real LLVM backend with a measured ~5,300x geomean speedup over its own (already decent) interpreter, byte-identical cross-backend behavior, 24KB standalone binaries, and pure function values clean enough to build a bench harness is genuinely fun for a performance tinkerer — the walls I hit (ms-only clock, no %.3f) are stdlib gaps I can work around, not design flaws; I'd want monotonic_ns and float formatting before doing serious benchmarking work in it, though.
