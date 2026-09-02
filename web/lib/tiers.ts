/**
 * THE TIER LADDER, AS THE ENGINE ACTUALLY APPLIES IT.
 *
 * Five rungs — four deterministic and one assisted — run in order, with a
 * subject matched at an earlier rung removed from the pool before the next one
 * runs. This module is the copy for all five, and it is the same organising
 * rule as `lib/derivations.ts`: a count is never more than one interaction away
 * from what produced it. For a tier that means the requirement it enforces, the
 * defect classes that reach it, and what a zero on that row would mean.
 *
 * Sourced from ARCHITECTURE.md §5 (the ladder table, `core/matcher/tiers.py`)
 * and METRICS.md §11 (which defect class each rung resolves, measured).
 *
 * ONE HONEST SEPARATION, AND IT IS THE WHOLE REASON THIS FILE EXISTS.
 * `requires` is a property of the ENGINE: it is true of every run at every
 * scale, because it is the rule the code applies. `defects` is a MEASUREMENT
 * taken on one dataset — seed 42, deterministic-only, at three scales — and it
 * is not a property of the run being viewed. The UI labels the second as a
 * reference measurement and names its scale and seed; it never presents it as
 * something this run reported. The wire carries `tier_counts` and nothing that
 * attributes a match to a defect class, so any page that showed a live
 * decomposition would be inventing one.
 */
import { formatINR } from "./money";
import type { Metrics } from "./types";

export type TierKey = keyof Metrics["tier_counts"];

export const TIER_KEYS: readonly TierKey[] = ["T0", "T1", "T2", "T3", "LLM"];

export const DETERMINISTIC_TIERS: readonly TierKey[] = ["T0", "T1", "T2", "T3"];
export const ASSISTED_TIERS: readonly TierKey[] = ["LLM"];

/** One line, for a dense row. The full requirement is in `requires`. */
export type TierRequirement = {
  label: string;
  value: string;
  /** Why the rule is that and not something looser. */
  note?: string;
};

export type Tier = {
  key: TierKey;
  /** The rule, in one line, for the row itself. */
  rule: string;
  /** What this rung requires, field by field. */
  requires: TierRequirement[];
  /** What it takes to fall through to the next rung. */
  declines: string;
  /** Defect classes this rung resolves on the reference dataset. */
  defects: string;
  /** What a count of zero on this row would mean. Never "a bug". */
  zeroMeans: string;
};

/**
 * ±100 paise, as an integer, so the display value is formatted by the app's one
 * money formatter rather than typed out as a string with a rupee sign in it.
 */
export const TOLERANCE_PAISE = 100;
export const WINDOW_DAYS = 2;

/**
 * The tolerance, written the way every other amount in this app is written:
 * through the one money formatter, with the wire integer beside it. It is a
 * measurement in paise and it is rendered as rupees, and both halves are shown
 * because confusing the two is the single easiest mistake to make here.
 */
const TOLERANCE = `±${formatINR(TOLERANCE_PAISE)} (${TOLERANCE_PAISE} paise)`;

export const TIERS: Record<TierKey, Tier> = {
  T0: {
    key: "T0",
    rule: "Reference hit and exact arithmetic.",
    requires: [
      {
        label: "Reference",
        value: "A UTR, or a settlement id in the narration",
        note: "The only rung that needs the line to name its own settlement.",
      },
      {
        label: "Arithmetic",
        value: "Reconstructed net equals the bank credit exactly",
        note: "A settlement id proves identity. It says nothing about whether the sum closes, so the reference alone is never enough.",
      },
      { label: "Payment legs", value: "Any number" },
      { label: "Tolerance", value: "0 paise" },
      {
        label: "Date window",
        value: "None",
        note: "An explicit reference plus an exactly closing sum is not made more or less certain by a late posting.",
      },
    ],
    declines:
      "The reference resolves but the sum does not close. T0 writes the residual into the audit trail and lets the line fall through, rather than claiming it at confidence 1.00 with a net that is not the bank credit.",
    defects:
      "Every clean settlement, plus the six defect classes that damage the content of a settlement while leaving its reference intact and its sum exact: many-to-one batches, cross-period refunds, MDR plus GST rounding, the canonical copy of a duplicated leg, chargeback holds, and a settled leg whose order reference was recovered before matching.",
    zeroMeans:
      "No bank line in this run both named its settlement and closed to the paise. On a dataset with any clean settlements at all that would be surprising — this is the rung that carries the bulk of the work.",
  },

  T1: {
    key: "T1",
    rule: "Reconstruction closes exactly, from exactly one payment leg.",
    requires: [
      {
        label: "Reference",
        value: "Not required",
        note: "This rung exists for lines whose narration was stripped of the settlement id, the UTR and the entity.",
      },
      { label: "Arithmetic", value: "Reconstructed net equals the credit exactly" },
      {
        label: "Payment legs",
        value: "Exactly one",
        note: "Fee, tax, refund, chargeback, reserve and adjustment legs do not count. A settlement with one payment and two deduction legs is T1: it settles one order, and the deductions are the arithmetic rather than the batch.",
      },
      { label: "Tolerance", value: "0 paise" },
      { label: "Date window", value: `±${WINDOW_DAYS} days` },
    ],
    declines:
      "More than one unclaimed settlement satisfies the arithmetic and the window. More than one valid candidate means match nothing, and the candidate set goes into the audit trail.",
    defects:
      "Garbled narrations whose settlement has a single payment leg. Garbling is the only defect that removes the reference from a line that is still resolvable, so it is the only thing that ever reaches a reconstruction rung.",
    zeroMeans:
      "Nothing in this run lost its reference on a single-payment-leg settlement. A dataset with no garbled narrations, or one whose garbled lines all sit on multi-leg batches, reports zero here and is correct.",
  },

  T2: {
    key: "T2",
    rule: "Reconstruction closes exactly, from two or more payment legs.",
    requires: [
      { label: "Reference", value: "Not required" },
      { label: "Arithmetic", value: "Reconstructed net equals the credit exactly" },
      {
        label: "Payment legs",
        value: "Two or more",
        note: "Cardinality is the only thing separating this rung from T1. Method cannot separate them — an earlier draft defined T1 as reconstruction plus a date window, which is T2's method exactly, and T1 would have subsumed the rung the project exists for.",
      },
      { label: "Tolerance", value: "0 paise" },
      { label: "Date window", value: `±${WINDOW_DAYS} days` },
    ],
    declines:
      "The same ambiguity rule as T1. Candidacy is searched blind to cardinality and the ambiguity rule is applied to the whole candidate set afterwards — filtering the pool by leg count first would turn the T1/T2 split into a tie-breaker, and each settlement would look unique inside its own partition.",
    defects:
      "Garbled narrations whose settlement has two or more payment legs. The many-orders-to-one-credit case, with nothing in the narration to point at it.",
    zeroMeans:
      "No multi-leg batch in this run lost its reference. On a small dataset this is the ordinary result, not a rung that failed: a zero here is a measurement.",
  },

  T3: {
    key: "T3",
    rule: "The same reconstruction, accepted within tolerance.",
    requires: [
      { label: "Reference", value: "Not required" },
      {
        label: "Arithmetic",
        value: "Reconstructed net within tolerance of the credit",
        note: "The delta is recorded in the match evidence rather than discarded, so a tolerance match never looks like an exact one.",
      },
      {
        label: "Payment legs",
        value: "Any number",
        note: "Deliberately cardinality-agnostic. Restricting it to T2's cardinality would strand a single-payment-leg settlement carrying a rounding break, which is exactly the shape this rung exists for.",
      },
      {
        label: "Tolerance",
        value: TOLERANCE,
        note: "The loosest amount window in the system. The verifier is held to exactly the same one.",
      },
      { label: "Date window", value: `±${WINDOW_DAYS} days` },
    ],
    declines:
      "The residual is larger than the tolerance, or more than one candidate closes within it.",
    defects:
      "Rounding breaks. T0's reference lookup hits, T0 declines on the residual, T1 and T2 decline on a tolerance of zero, and this rung takes the line at confidence 0.80 with the delta on the record.",
    zeroMeans:
      "No line in this run needed the tolerance. Every match closed to the paise or did not close at all.",
  },

  LLM: {
    key: "LLM",
    rule: "The analyst proposed a resolution and the verifier accepted it.",
    requires: [
      {
        label: "Proposal",
        value: "A hypothesis naming one bank line and a set of legs",
        note: "The analyst only ever sees the residue the deterministic rungs declined.",
      },
      {
        label: "Verifier",
        value: "Six checks over five frozen labels, all of which must hold",
        note: "existence, exclusivity, causality, arithmetic, coherence and uniqueness, in that order, returning on the first failure. Coherence reports under the existence label because the five spellings are shared with the exception contract and frozen.",
      },
      {
        label: "Subject tie",
        value: "The hypothesis's subject must be the bank line it proposes against",
        note: "A seventh gate, owned by the accept loop rather than the verifier: the verifier cannot know a subject's type, so it never checks this. Without it a hypothesis could credit a resolution to a subject the data does not determine.",
      },
      {
        label: "Amount tolerance",
        value: TOLERANCE,
        note: "T3's tolerance, deliberately. A model proposal does not get a wider window than deterministic code would have taken.",
      },
      {
        label: "Money",
        value: "Recomputed, never taken from the model",
        note: "Every money field on an accepted match is recomputed by the matcher's own reconstruction from the legs, and the net is the bank credit. The hypothesis's own confidence is recorded in evidence and read by no check.",
      },
    ],
    declines:
      "Any one of the six checks fails, or the subject tie does not hold. The proposal, the free-text reason and the machine-readable failed check all survive onto the exception, which is where a rejection becomes visible rather than being discarded.",
    defects:
      "Whatever the deterministic rungs left that a single complete settlement can still explain. On the seeded datasets that set is empty, and the correct outcome is rejection rather than assistance.",
    zeroMeans:
      "Either no model was called on this run, or one was called and nothing it proposed survived the verifier. Those are different results and the analyst view separates them; a zero here alone does not.",
  },
};

/* ------------------------------------------------------------------ *
 * The reference decomposition
 * ------------------------------------------------------------------ */

export type DefectClass = {
  name: string;
  /** What the class tests. */
  tests: string;
  /**
   * The rungs that resolve it. Empty when nothing does — and two of the ten
   * classes are genuinely empty here, one of them deliberately.
   */
  resolvedBy: readonly TierKey[];
  /** How a class that reaches two rungs divides between them. */
  split?: string;
  /** What happens to it, when that needs more than the rung name. */
  outcome: string | null;
  /** Injected instances at 5,000 records, seed 42. */
  instances: number;
  /**
   * Bank-line subjects those instances produce, when it is not one each. Two
   * classes span two lines per instance, which is why 50 injected defects
   * become 100 exceptions.
   */
  subjects?: number;
};

/**
 * The ten defect classes, measured on seed 42 at 5,000 records, deterministic
 * only. Reproduced from METRICS.md §11.
 *
 * This is a REFERENCE MEASUREMENT of one dataset and not a property of the run
 * being viewed. Nothing on the wire attributes a match to a defect class, so
 * this table can never be recomputed live and is always labelled with the seed
 * and the scale it was taken at.
 */
export const REFERENCE_SEED = 42;
export const REFERENCE_RECORDS = 5000;

/**
 * `tier_counts` from the reference run, so the decomposition below can be
 * checked against it in the UI rather than asserted in prose. If a figure here
 * is ever edited out of agreement with DEFECT_CLASSES, the page says so.
 */
export const REFERENCE_TIER_COUNTS: Record<TierKey, number> = {
  T0: 1327,
  T1: 17,
  T2: 83,
  T3: 50,
  LLM: 0,
};

/** Matches the reference run produced, and the subjects it accounted for. */
export const REFERENCE_MATCHES = 1477;
export const REFERENCE_EXCEPTIONS = 300;
export const REFERENCE_BANK_LINES = 1677;

export const DEFECT_CLASSES: readonly DefectClass[] = [
  {
    name: "many_to_one_batch",
    tests: "The dominant shape: N orders paid as one bank credit.",
    resolvedBy: ["T0"],
    outcome: null,
    instances: 100,
  },
  {
    name: "cross_period_refund",
    tests: "A refund from the previous cycle netted into this one.",
    resolvedBy: ["T0"],
    outcome: null,
    instances: 44,
  },
  {
    name: "fee_plus_gst",
    tests: "MDR at 2.36%, then 18% GST on the MDR, with integer rounding at each step.",
    resolvedBy: ["T0"],
    outcome: null,
    instances: 50,
  },
  {
    name: "garbled_narration",
    tests: "Entity, settlement reference and UTR all stripped from the line.",
    resolvedBy: ["T1", "T2"],
    split: "T1 17, T2 83 — the split is payment-leg cardinality and nothing else.",
    outcome: null,
    instances: 100,
  },
  {
    name: "duplicate_psp_txn",
    tests: "The same economic event recorded twice.",
    resolvedBy: ["T0"],
    outcome: "The canonical copy matches; the duplicate is itemised as DUPLICATE_PSP_TXN, outside every rate's denominator.",
    instances: 100,
  },
  {
    name: "rounding_break",
    tests: "A ₹0.50 delta — the boundary between a tolerance match and an exception.",
    resolvedBy: ["T3"],
    outcome: null,
    instances: 50,
  },
  {
    name: "chargeback_hold",
    tests: "A deduction referencing no order in the register.",
    resolvedBy: ["T0"],
    outcome: null,
    instances: 50,
  },
  {
    name: "missing_order_ref",
    tests: "A settled payment leg with a null order id.",
    resolvedBy: ["T0"],
    outcome: "The order is recovered from the batch before matching, so the line reaches T0 intact.",
    instances: 50,
  },
  {
    name: "split_settlement",
    tests: "One settlement paid across two bank lines, closing neither on its own.",
    resolvedBy: [],
    outcome:
      "AMOUNT_MISMATCH. Both narrations name the settlement, so the reference lookup hits and then declines on the residual — which is exactly the other half's credit. This class is the entire distance between the match rate and 1.0, and closing it needs a new rung that can search subsets of bank lines.",
    instances: 50,
    subjects: 100,
  },
  {
    name: "ambiguous_unresolvable",
    tests: "Two settlements with an identical net on an identical date.",
    resolvedBy: [],
    outcome:
      "AMBIGUOUS_MULTI_CANDIDATE, by design. The data does not determine which settlement funded which line, so matching either would make iteration order the tie-breaker. Declining it is what the trap-capture rate measures.",
    instances: 50,
    subjects: 100,
  },
];
