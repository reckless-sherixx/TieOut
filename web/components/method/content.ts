/**
 * The content of /method, kept out of the page so the page is layout.
 *
 * Everything here restates the repository's own ARCHITECTURE.md and
 * METRICS.md rather than paraphrasing them loosely — including the parts that
 * are unflattering, which are the parts worth having.
 */

export const SECTIONS = [
  { id: "shape", label: "Why not a VLOOKUP" },
  { id: "pipeline", label: "The pipeline" },
  { id: "ladder", label: "The tier ladder" },
  { id: "ambiguity", label: "The ambiguity rule" },
  { id: "verifier", label: "The verifier" },
  { id: "defects", label: "The ten defects" },
  { id: "limitation", label: "Known limitation" },
  { id: "caveats", label: "What it does not prove" },
] as const;

export const PIPELINE = [
  {
    name: "Ingest",
    module: "core/ingest/",
    does: "Three CSVs become typed records. Parse, coerce, reject a bad row.",
    mayNot: "arithmetic, or matching.",
  },
  {
    name: "Canonicalize",
    module: "core/canonicalize/",
    does: "A narration becomes a settlement id, a bank reference and an entity. Duplicate legs are suppressed and missing order references are recovered from the batch.",
    mayNot: "decide a match. It derives views and records findings.",
  },
  {
    name: "Match",
    module: "core/matcher/tiers.py",
    does: "T0 through T3, in order. A tier may claim a settlement for a bank line.",
    mayNot: "guess under ambiguity; read the ground-truth file.",
  },
  {
    name: "Residue",
    module: "core/matcher/engine.py",
    does: "Every subject not matched, typed, with a machine-readable reason code. The partition invariant holds here: every subject is matched or excepted, exactly once.",
    mayNot: "leave a subject in neither set, or in both.",
  },
  {
    name: "Analyst",
    module: "core/llm/",
    does: "Proposes hypotheses over the residue, given the exceptions and their candidate settlements — with every money figure in the prompt already computed for it.",
    mayNot: "compute money; see the whole batch; see the ground truth; accept anything.",
  },
  {
    name: "Verifier",
    module: "core/llm/verifier.py",
    does: "Six checks, all of which must hold. Rejecting is the only thing it does.",
    mayNot: "read the hypothesis's own confidence; widen a deterministic rule.",
  },
  {
    name: "Report",
    module: "scorer/, core/store/",
    does: "Metrics against ground truth, and the audit trail. The only package that grades.",
    mayNot: "be imported by the matcher. The dependency runs the other way.",
  },
] as const;

export const TIERS = [
  {
    tier: "T0",
    rule: "Reference hit and exact arithmetic",
    cardinality: "any",
    tolerance: "0",
    window: "none",
    confidence: "1.00",
  },
  {
    tier: "T1",
    rule: "Reconstruction closes exactly",
    cardinality: "exactly one",
    tolerance: "0",
    window: "±2 days",
    confidence: "0.95",
  },
  {
    tier: "T2",
    rule: "Reconstruction closes exactly",
    cardinality: "two or more",
    tolerance: "0",
    window: "±2 days",
    confidence: "0.99",
  },
  {
    tier: "T3",
    rule: "Reconstruction closes within tolerance",
    cardinality: "any",
    tolerance: "±₹1",
    window: "±2 days",
    confidence: "0.80",
  },
  {
    tier: "LLM",
    rule: "Proposed by the analyst, accepted by all six verifier checks",
    cardinality: "one settlement, complete",
    tolerance: "±₹1",
    window: "no lower bound",
    confidence: "0.70",
  },
] as const;

export const VERIFIER_CHECKS_DETAIL = [
  {
    name: "existence",
    rejects:
      "a null or unknown bank line id; a bank line with no credit; a credit that is zero or negative; an empty proposal; unknown transaction ids; a transaction id proposed more than once.",
    why: "it runs first so every later check can index the context without a key error, and that guarantee only holds if the bank line is validated here too. Three of its clauses were added after demonstration rather than in anticipation — a debit-only line read as a zero target is closed exactly by an empty proposal; a zero or negative credit is closed by a settlement whose legs cancel; and a repeated id passes every membership test trivially and is then counted again by the reconstruction, so naming one leg three times triples the net.",
  },
  {
    name: "exclusivity",
    rejects: "a proposal naming a transaction already claimed by an accepted match.",
    why: "one PSP leg funds one bank line. Without it, one settlement could be spent twice. The accept loop keeps the claimed set current between verifications rather than verifying a batch against one frozen context.",
  },
  {
    name: "causality",
    rejects:
      "a leg that never settled at all, and a leg whose settlement date falls after the bank line's date.",
    why: "money cannot arrive in the bank before the settlement that produced it, and money that never settled cannot have funded a credit either. Treating an unsettled leg as 'not late' waves through precisely the leg that provably did not fund the line. One asymmetry, stated because it is real: this bounds lateness only, where the deterministic tiers apply a symmetric two-day window — so a settlement that settled thirty days early passes here and would not have passed there.",
  },
  {
    name: "arithmetic",
    rejects:
      "a proposal whose reconstructed net differs from the bank credit by more than ₹1.",
    why: "this is the re-check the whole claim rests on. It calls the matcher's own reconstruction function, imported rather than re-implemented, so the two can never disagree. The tolerance is the loosest deterministic tier's: a model's proposal does not get a wider window than deterministic code would have taken.",
  },
  {
    name: "coherence",
    rejects:
      "a proposal that is not exactly one settlement — legs spanning two settlements, legs carrying no settlement id, a settlement not in the ingested data, or an incomplete leg set of a real one.",
    why: "it exists because of a demonstrated exploit, and it reports under the existence label because the five check names are a frozen contract. See below.",
  },
  {
    name: "uniqueness",
    rejects:
      "a proposal where more than one unclaimed settlement closes the same bank line within the same ₹1.",
    why: "it also exists because of a demonstrated exploit, and it is the same ambiguity rule the deterministic tiers obey. See below.",
  },
] as const;

export const DEFECT_CLASSES = [
  {
    name: "many_to_one_batch",
    tests: "the dominant shape: N orders arriving as one bank credit",
    resolvedBy: "T0",
    unresolved: false,
  },
  {
    name: "cross_period_refund",
    tests: "a refund from the previous cycle netted into this one",
    resolvedBy: "T0",
    unresolved: false,
  },
  {
    name: "fee_plus_gst",
    tests: "MDR at 2.36% plus 18% GST on the MDR, with integer rounding",
    resolvedBy: "T0",
    unresolved: false,
  },
  {
    name: "garbled_narration",
    tests: "entity, settlement reference and bank reference all stripped",
    resolvedBy: "T1 and T2",
    unresolved: false,
  },
  {
    name: "duplicate_psp_txn",
    tests: "the same economic event recorded twice",
    resolvedBy: "T0, the copy excepted",
    unresolved: false,
  },
  {
    name: "rounding_break",
    tests: "a ₹0.50 delta — the boundary between the tolerant tier and an exception",
    resolvedBy: "T3",
    unresolved: false,
  },
  {
    name: "chargeback_hold",
    tests: "a deduction referencing no order in the register",
    resolvedBy: "T0",
    unresolved: false,
  },
  {
    name: "split_settlement",
    tests: "one settlement paid across two bank lines",
    resolvedBy: "nothing — reported as an amount mismatch",
    unresolved: true,
  },
  {
    name: "missing_order_ref",
    tests: "a settled payment leg carrying no order id",
    resolvedBy: "T0, order recovered first",
    unresolved: false,
  },
  {
    name: "ambiguous_unresolvable",
    tests: "two settlements, identical amount, identical date",
    resolvedBy: "nothing, by design",
    unresolved: true,
  },
] as const;

export const LIMITATIONS = [
  {
    title: "Split settlements are not solved",
    detail:
      "They are 100% of the gap between the auto-match rate and 1.0 at every scale — verified as set equality in both directions, with no remainder and no long tail. Deliberate; it needs a new tier with the ambiguity rule designed in from the start.",
  },
  {
    title:
      "The trap-capture rate and precision are both perfect for an engine that matches nothing",
    detail:
      "Confirmed against real ground truth, not hypothesised. Neither is meaningful without the auto-match rate and recall beside it.",
  },
  {
    title: "No live model call has ever been made from this repository",
    detail:
      "Everything on the analyst path is built, wired and tested end to end against a stubbed client. Every LLM figure is labelled as such and the live table is empty.",
  },
  {
    title: "Five of the eight reason codes score zero on this generator",
    detail:
      "They are implemented and unit-tested; this data does not produce them. Read those zeros as 'not exercised here', not as 'handled'.",
  },
  {
    title: "Throughput degrades superlinearly and is machine-dependent",
    detail:
      "Per-record cost rises about 3.5× for 10× the records, because the candidate search scans every unclaimed settlement for every open bank line with no index. Above roughly 50,000 records it would need bucketing by reconstructed net. The accuracy numbers are exact; this one is not.",
  },
  {
    title: "The boundary proof is structural, not adversarial",
    detail:
      "A static check asserts that no module under the matcher imports or names the ground-truth file. It cannot catch a dynamic import or a runtime-assembled path, and its own header says so. It is a good-faith guard against accidental coupling, not a sandbox.",
  },
  {
    title: "The scorecard is demonstrably blind to at least one real bug",
    detail:
      "A cardinality-filtered candidate pool — the exact defect the ambiguity trap exists to catch — leaves every metric at every scale byte-identical, including the tier breakdown. Only a single-subject unit test catches it. The two-subject test that most looks like the trap passes on the broken implementation, for the same reason the metrics do.",
  },
  {
    title: "Two fixes rated critical moved no metric at any scale",
    detail:
      "The collisions they handle do not occur in this generator's data. Their only evidence is sixteen permutation-invariant unit tests that construct those collisions by hand.",
  },
  {
    title: "The verifier had five holes past a green suite of 361 tests",
    detail:
      "Each was found by writing the exploit rather than by reading the code, and the fix for the first hole contained the fifth. The argument that found them predicts a sixth.",
  },
  {
    title: "All data is synthetic and self-generated",
    detail:
      "There is no live payment-processor integration, the currency is INR only, and no production concern — authentication, multi-tenancy, retention — is addressed.",
  },
] as const;
