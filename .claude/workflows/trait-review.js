export const meta = {
  name: 'trait-review',
  description: 'Adversarial review of the Flex trait/impl + bounded-generics system',
  phases: [
    { title: 'Find', detail: 'probe dimensions with real compile/run programs' },
    { title: 'Verify', detail: 'independently reproduce and judge each finding' },
  ],
}

const PREAMBLE = `
You are auditing the trait/impl + bounded-generics feature of the Flex compiler.
Repo: /home/autark/src/flexlang  (cwd there). Run the compiler with:
  mise exec -- uv run flx <cmd> <file.flx>
where <cmd> is one of: parse | check | expand | emit-mlir | run | test.
  - 'check' type-checks (exit 0 ok). 'expand' shows macro/derive expansion.
  - 'run' compiles to native and runs main() -> I64 (process exit code = return value, mod 256).
  - 'test' compiles+runs test blocks (prints pass/fail; exit 0 if all pass).
The LLVM/MLIR toolchain IS installed (run/test work). Write probe programs to
/tmp with a UNIQUE prefix you pick (e.g. /tmp/dimX_*.flx) to avoid collisions.

LANGUAGE CHEATSHEET (functional, braces, F#/Elm-flavored):
  module Main
  type Point = { x: I64, y: I64 }                  // record
  type Shape = | Circle(I64) | Square(I64)         // ADT (single payload field max)
  trait Show = { fn show(self: Self) -> String }   // trait: signature-only methods
  impl Show for Point = { fn show(self: Point) -> String = { "P" ++ to_str(self.x) } }
  fn describe<T: Show>(v: T) -> String = { v.show() }   // bounded generic
  fn pick<T>(a: T, b: T) -> T = { a }                   // unconstrained generic
  let / mut, if/else are expressions, match { Ctor(x) -> ... }, ? on Result/Option.
  Strings: "a" ++ to_str(n). Effects: fn f() -> Unit uses { Log } = { Log.info("hi") }.
  Builtins assert/assert_eq/assert_ne/fail/panic only inside test "name" { ... }.
  Generics monomorphize: describe(p) -> a concrete function describe$Point that
  calls Show$Point$show. Method calls dispatch statically to the impl symbol.

HOW THE FEATURE IS BUILT (so you can target weak points):
  - check.py: generic fns kept out of the callable table; each call site infers the
    type substitution POSITIONALLY (a param written exactly 'T' binds T), checks the
    declared bounds against impls, records an instantiation + method_target.
  - specialize.py: fixpoint — clone each demanded template with TypeExprs substituted
    and FRESH node ids, append as a concrete fn, re-check until closed (MONO001 if not).
  - The body of a generic is only checked via its specializations (C++-template style),
    so bounds satisfaction is enforced at the CALL SITE; body method calls resolve
    against whatever impls the concrete type has.
  - backend: a call whose id is in method_targets calls flx_<symbol> directly
    (receiver as arg0 for p.m(), args as-is for generic g()).

YOUR JOB: find REAL defects — crashes/tracebacks, miscompiles (wrong runtime
output/exit code), unsound acceptance (a wrong program that compiles+runs), or
good programs wrongly rejected. For EACH finding give: a MINIMAL .flx program, the
exact command, the ACTUAL observed output/exit, the EXPECTED behavior, and why it's
a real defect (not a known/acceptable MVP limitation). Ground every claim in a real
run — paste the command and its output. Ignore: pure cosmetics, missing features
that simply produce a clean diagnostic, and the documented "bounds checked at call
site, body duck-typed at instantiation" design choice (that is NOT a bug by itself).
Prefer depth: actually try recursion, nesting, multiple type params, aggregates
through generics, ambiguous methods, effects, derive interplay, and odd identifiers.
`

const FINDING_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          program: { type: 'string', description: 'the minimal .flx source' },
          command: { type: 'string', description: 'exact flx command run' },
          observed: { type: 'string', description: 'actual output / exit code / traceback' },
          expected: { type: 'string' },
          severity: { type: 'string', enum: ['crash', 'miscompile', 'unsound', 'false-reject', 'minor'] },
          why: { type: 'string', description: 'why this is a real defect' },
        },
        required: ['title', 'program', 'command', 'observed', 'expected', 'severity', 'why'],
        additionalProperties: false,
      },
    },
  },
  required: ['findings'],
  additionalProperties: false,
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    reproduced: { type: 'boolean', description: 'did you re-run it and see the claimed behavior?' },
    isRealBug: { type: 'boolean' },
    severity: { type: 'string', enum: ['crash', 'miscompile', 'unsound', 'false-reject', 'minor', 'not-a-bug'] },
    evidence: { type: 'string', description: 'the command + actual output you observed' },
    rootCause: { type: 'string', description: 'best guess at file/mechanism, or empty' },
    notes: { type: 'string' },
  },
  required: ['reproduced', 'isRealBug', 'severity', 'evidence', 'rootCause', 'notes'],
  additionalProperties: false,
}

const DIMENSIONS = [
  {
    key: 'mono',
    prompt: `${PREAMBLE}
DIMENSION: monomorphization mechanics. Probe: generic recursion (fn that calls
itself), generic calling another generic, nested generic types (e.g. a generic over
Option<T> or a record holding T), multiple type params <A, B>, the same generic used
at 3+ distinct types, a generic whose type param appears in the RETURN only,
specializations whose mangled names could collide, generic returning a record/ADT,
and the MONO001 convergence guard (try to trip it; also confirm a legitimately
recursive-at-same-type call still converges). Hunt wrong output and crashes.`,
  },
  {
    key: 'dispatch',
    prompt: `${PREAMBLE}
DIMENSION: trait dispatch + bounds. Probe: multiple bounds <T: Show + Eq> and using
BOTH inside the body; a bound method that takes arguments and returns Self; two
different traits declaring a method of the SAME name (ambiguity / wrong pick); impl
for an ADT and for a primitive (I64); a method body that calls another method on
self; chained calls a.m().n(); field-vs-method name clashes; calling describe at a
type missing the impl. Look for wrong dispatch target (miscompile) and unsound
acceptance.`,
  },
  {
    key: 'effects',
    prompt: `${PREAMBLE}
DIMENSION: effects + control-flow interaction. Probe: a generic or trait method that
'uses { Log }' and is called from a context that does/doesn't declare the effect
(effect leak = unsound; spurious rejection = false-reject); effect propagation
through a generic call chain; generic functions used inside test blocks vs main; a
generic body using if/else, match, mut, and the ? operator; a trait method that
itself calls a generic. Verify effects are neither dropped nor spuriously required.`,
  },
  {
    key: 'codegen',
    prompt: `${PREAMBLE}
DIMENSION: backend codegen + soundness. Probe: aggregate (record/ADT) values passed
INTO and returned OUT OF generic/trait calls (struct ABI), equality (==, assert_eq)
on values produced by generics, derive(Eq, Show) combined with generic functions and
trait dispatch, a generic that constructs and returns an ADT then matches on it,
deeply nested calls, and identifiers/types with unusual but legal names. Focus on
miscompiles: the program compiles and runs but prints the WRONG value or exits with
the wrong code. Always assert the expected concrete value and report mismatches.`,
  },
]

phase('Find')
const found = await parallel(
  DIMENSIONS.map((d) => () =>
    agent(d.prompt, { label: `find:${d.key}`, phase: 'Find', schema: FINDING_SCHEMA }).then((r) => ({
      key: d.key,
      findings: (r && r.findings) || [],
    })),
  ),
)

const all = found
  .filter(Boolean)
  .flatMap((r) => r.findings.map((f) => ({ ...f, dimension: r.key })))
log(`collected ${all.length} candidate findings across ${DIMENSIONS.length} dimensions`)

phase('Verify')
const verified = await parallel(
  all.map((f) => () =>
    agent(
      `${PREAMBLE}
A prior auditor reported this finding. INDEPENDENTLY reproduce it: write the program,
run the exact command yourself, and observe the real behavior. Then judge whether it
is a genuine defect or a false alarm / known-acceptable limitation. Be skeptical —
default to not-a-bug unless you SEE the defect with your own run.

FINDING: ${f.title}
SEVERITY CLAIMED: ${f.severity}
PROGRAM:
${f.program}
COMMAND: ${f.command}
CLAIMED OBSERVED: ${f.observed}
CLAIMED EXPECTED: ${f.expected}
WHY (claimed): ${f.why}`,
      { label: `verify:${f.dimension}:${f.severity}`, phase: 'Verify', schema: VERDICT_SCHEMA },
    ).then((v) => ({ finding: f, verdict: v })),
  ),
)

const confirmed = verified
  .filter(Boolean)
  .filter((v) => v.verdict && v.verdict.reproduced && v.verdict.isRealBug && v.verdict.severity !== 'not-a-bug')

log(`confirmed ${confirmed.length} real defects of ${all.length} candidates`)
return {
  confirmedCount: confirmed.length,
  candidateCount: all.length,
  confirmed: confirmed.map((v) => ({
    title: v.finding.title,
    dimension: v.finding.dimension,
    severity: v.verdict.severity,
    program: v.finding.program,
    command: v.finding.command,
    evidence: v.verdict.evidence,
    rootCause: v.verdict.rootCause,
    notes: v.verdict.notes,
    expected: v.finding.expected,
  })),
}
