/**
 * WHAT EACH NUMBER ACTUALLY COUNTS.
 *
 * The organising rule of this console is that no metric is ever more than one
 * interaction away from its own derivation: the definition, the numerator, the
 * denominator, the values on the wire that feed it, and the thing it does not
 * prove. All five live beside the number, never on a page of their own, because
 * a caveat a reader has to go and find has not been told to them.
 *
 * Definitions below are the project's own, from METRICS.md §3, which copies
 * them verbatim out of `scorer/metrics.py`'s module docstring so they cannot
 * drift from the code that computes them.
 *
 * ONE HONEST LIMIT, STATED HERE ONCE AND SURFACED IN THE UI. `Metrics` on the
 * wire carries rates, not the integers they were computed from -- there is no
 * `resolvable_subject_count` field in `api/openapi.yaml`. So `inputs` below
 * lists what the contract DOES carry and what each figure constrains; it never
 * back-computes a denominator by dividing one wire float by another and
 * presenting the result as a count. A derived integer that looks measured is
 * worse than an absent one.
 */
import type { Metrics, RunSummary } from "./types";
import { formatINR, formatRate } from "./money";

const int = (n: number) => n.toLocaleString("en-IN");

export type MetricKey =
  | "auto_match_rate"
  | "assisted_match_rate"
  | "recall_on_resolvable"
  | "false_match_rate"
  | "precision"
  | "trap_capture_rate"
  | "exception_rate"
  | "llm_rejection_rate"
  | "throughput_records_per_sec"
  | "llm_cost_usd_per_100"
  | "llm_tokens_per_100"
  | "itc_substantiated_paise"
  | "itc_at_risk_paise"
  | "itc_variance_paise";

export type DerivationInput = {
  label: string;
  value: string;
  note?: string;
};

export type Derivation = {
  key: MetricKey;
  label: string;
  /** How the value renders. */
  format: (m: Metrics) => string;
  /** One sentence: what the number counts. */
  definition: string;
  numerator: string;
  denominator: string;
  /** Values on the wire that feed or constrain it. */
  inputs: (run: RunSummary, m: Metrics) => DerivationInput[];
  /**
   * What this number does not prove. Non-null wherever METRICS.md records a
   * case of the metric reading green while failing to establish its claim --
   * which it does four separate times.
   */
  caveat: string | null;
};

/** The subject of every rate on this scorecard is the bank line. */
export const SUBJECT_NOTE =
  "The subject is the bank line. Linkages and unresolvable ids are both keyed on bank_line_id, so that is the unit every rate counts. PSP-side exceptions are diagnostics and sit outside every denominator here; folding them in would let one bank-line failure be counted twice.";

const deterministic = (m: Metrics) =>
  m.tier_counts.T0 + m.tier_counts.T1 + m.tier_counts.T2 + m.tier_counts.T3;

const tierInput = (m: Metrics): DerivationInput => ({
  label: "tier_counts, deterministic",
  value: `T0 ${int(m.tier_counts.T0)} + T1 ${int(m.tier_counts.T1)} + T2 ${int(
    m.tier_counts.T2,
  )} + T3 ${int(m.tier_counts.T3)} = ${int(deterministic(m))}`,
  note: "match groups produced by the deterministic ladder, from the engine's own tier assignment",
});

const rateOnlyNote: DerivationInput = {
  label: "on the wire",
  value: "the rate, not its two integers",
  note: "Metrics carries no resolvable-subject count, so the denominator is named above but cannot be shown as a figure from this run.",
};

export const DERIVATIONS: Record<MetricKey, Derivation> = {
  auto_match_rate: {
    key: "auto_match_rate",
    label: "Auto-match rate",
    format: (m) => formatRate(m.auto_match_rate),
    definition:
      "The share of subjects the data determines that the deterministic tiers resolved, with no model involved.",
    numerator:
      "subjects matched by T0–T3, intersected with the truth-resolvable set",
    denominator: "truth-resolvable subjects",
    inputs: (run, m) => [
      tierInput(m),
      {
        label: "match_count",
        value: int(run.match_count),
        note: "all matches this run produced, deterministic and assisted together",
      },
      rateOnlyNote,
    ],
    caveat:
      "A cardinality-filtered candidate pool — the exact defect the ambiguity trap exists to catch — leaves this number byte-identical at every scale. It moved no digit when the bug was injected deliberately. Read it knowing the scorecard is demonstrably blind to at least one real correctness bug; the unit tests are what catch that one.",
  },

  assisted_match_rate: {
    key: "assisted_match_rate",
    label: "Assisted match rate",
    format: (m) => formatRate(m.assisted_match_rate),
    definition:
      "The share of subjects the data determines that were resolved by an LLM hypothesis the verifier accepted.",
    numerator:
      "subjects matched via an accepted LLM hypothesis, intersected with the truth-resolvable set",
    denominator: "truth-resolvable subjects",
    inputs: (_run, m) => [
      {
        label: "tier_counts.LLM",
        value: int(m.tier_counts.LLM),
        note: "accepted hypotheses that became matches. 0 is a result, not a missing key.",
      },
      {
        label: "llm_rejection_rate",
        value: formatRate(m.llm_rejection_rate),
        note: "the other half of the same story — read the two together",
      },
      rateOnlyNote,
    ],
    caveat:
      "This is only meaningful next to the rejection rate. A verifier that accepts nothing drives this to 0 and the rejection rate to 1; a verifier that accepts everything does the reverse. Neither number is a result on its own.",
  },

  recall_on_resolvable: {
    key: "recall_on_resolvable",
    label: "Recall on resolvable",
    format: (m) => formatRate(m.recall_on_resolvable, 2),
    definition:
      "The share of subjects the data determines that were matched CORRECTLY — checked against the recorded linkage, not merely matched.",
    numerator:
      "matches that agree with truth, intersected with the truth-resolvable set",
    denominator: "truth-resolvable subjects",
    inputs: (_run, m) => [
      {
        label: "auto_match_rate",
        value: formatRate(m.auto_match_rate, 4),
        note: "the same denominator, a different numerator: matched, versus matched correctly",
      },
      {
        label: "false_match_rate",
        value: formatRate(m.false_match_rate, 4),
        note: "the two rates above coincide exactly when this is 0",
      },
      rateOnlyNote,
    ],
    caveat:
      "Its agreement with the auto-match rate is a checkable invariant, not a coincidence: the two are numerator-different and coincide only while no false match exists. A divergence is the signal that the engine has produced a wrong match — check that before anything else.",
  },

  false_match_rate: {
    key: "false_match_rate",
    label: "False-match rate",
    format: (m) => formatRate(m.false_match_rate),
    definition:
      "The share of the matches this run asserted that disagree with the recorded linkage. A wrong match is worse than no match: an unmatched line is an exception a human works through, a wrongly matched one is two bad ledger entries nobody is looking for.",
    numerator: "matches disagreeing with the recorded linkage",
    denominator: "all matches produced",
    inputs: (run, m) => [
      {
        label: "match_count",
        value: int(run.match_count),
        note: "the denominator, and this one IS on the wire",
      },
      tierInput(m),
      {
        label: "precision",
        value: formatRate(m.precision, 4),
        note: "derived, not measured separately: 1 − false_match_rate",
      },
    ],
    caveat:
      "With no matches at all the denominator is empty, this evaluates to 0.0, and precision reads 1.0. An engine that matches nothing scores perfectly on both. It means something only beside the auto-match rate and recall.",
  },

  precision: {
    key: "precision",
    label: "Precision",
    format: (m) => formatRate(m.precision, 2),
    definition:
      "Of everything this run matched, the share that is right. It is not measured independently.",
    numerator: "derived, not a ratio",
    denominator: "1 − false_match_rate",
    inputs: (run, m) => [
      {
        label: "false_match_rate",
        value: formatRate(m.false_match_rate, 4),
      },
      {
        label: "match_count",
        value: int(run.match_count),
        note: "the population this is over",
      },
    ],
    caveat:
      "Trivially 1.0 for an engine that asserts nothing, for the same reason the false-match rate is trivially 0.0 there. It is a necessary condition, never an achievement.",
  },

  trap_capture_rate: {
    key: "trap_capture_rate",
    label: "Trap capture rate",
    format: (m) => formatRate(m.trap_capture_rate),
    definition:
      "The share of subjects that are unresolvable BY CONSTRUCTION — two settlements with an identical net on an identical date, where either assignment satisfies the arithmetic — that the engine correctly left unmatched.",
    numerator: "unresolvable subjects correctly left unmatched",
    denominator: "unresolvable subjects",
    inputs: (run, m) => [
      {
        label: "exception_count",
        value: int(run.exception_count),
        note: "traps are a subset of these: they are supposed to end up here",
      },
      {
        label: "auto_match_rate",
        value: formatRate(m.auto_match_rate, 4),
        note: "quote this beside it or not at all",
      },
      rateOnlyNote,
    ],
    caveat:
      "An engine that matches nothing scores 1.0 here — it leaves the traps alone by leaving everything alone. Confirmed, not hypothesised. And uniquely among these rates, an EMPTY denominator evaluates to 1.0 rather than 0.0, because declining a trap that does not exist is not a failure. Both facts make this a necessary condition and not an achievement.",
  },

  exception_rate: {
    key: "exception_rate",
    label: "Exception rate",
    format: (m) => formatRate(m.exception_rate),
    definition:
      "The share of all subjects this run did not match. Its denominator comes from truth, never from the result being graded.",
    numerator: "truth subjects the run did not match",
    denominator: "all truth subjects",
    inputs: (run) => [
      {
        label: "exception_count",
        value: int(run.exception_count),
      },
      {
        label: "match_count",
        value: int(run.match_count),
      },
      {
        label: "accounted subjects",
        value: int(run.match_count + run.exception_count),
        note: "every subject is matched or excepted, exactly once, never both and never neither",
      },
    ],
    caveat:
      "An earlier denominator was the count of subjects the ENGINE accounted for, so a subject the engine dropped from both sets left the denominator with it and the rate improved. An engine could raise its score by losing work. The fix is invisible in the headline by construction — the two denominators coincide whenever the engine accounts for everything — and mutation tests are what assert it.",
  },

  llm_rejection_rate: {
    key: "llm_rejection_rate",
    label: "LLM rejection rate",
    format: (m) => formatRate(m.llm_rejection_rate),
    definition:
      "The share of hypotheses the analyst proposed that the verifier refused. A rejection is the guardrail firing, not a failure.",
    numerator: "hypotheses rejected by the verifier",
    denominator: "hypotheses proposed",
    inputs: (_run, m) => [
      {
        label: "tier_counts.LLM",
        value: int(m.tier_counts.LLM),
        note: "hypotheses that survived all six checks and became matches",
      },
      {
        label: "assisted_match_rate",
        value: formatRate(m.assisted_match_rate, 4),
        note: "the number this one is worthless without",
      },
      {
        label: "llm_tokens_per_100",
        value: int(m.llm_tokens_per_100),
        note: "Positive proves a model was called. Zero does not disprove it -- a call that failed at the provider bills nothing -- so read 0.0 above as 'not established', not as 'nothing rejected'.",
      },
    ],
    caveat:
      "A verifier that rejects everything scores 1.0. On the seeded fixtures the correct outcome IS total rejection: the residue splits into ambiguity traps, which no engine may resolve, and split settlements, which no single-settlement hypothesis can express. A live run that accepted something in that residue would be the thing to investigate.",
  },

  throughput_records_per_sec: {
    key: "throughput_records_per_sec",
    label: "Throughput",
    format: (m) => `${int(Math.round(m.throughput_records_per_sec))} rec/s`,
    definition:
      "Records per second through the matching stage. Ingest and scoring are excluded; LLM latency is excluded by definition, so the same engine cannot report a different speed because a flag that does not touch it was set.",
    numerator: "record_count",
    denominator: "wall-clock seconds, excluding LLM latency",
    inputs: (run, m) => [
      {
        label: "record_count",
        value: int(run.record_count),
      },
      {
        label: "implied elapsed",
        value:
          m.throughput_records_per_sec > 0
            ? `${(run.record_count / m.throughput_records_per_sec).toFixed(3)} s`
            : "—",
        note: "the denominator, recovered from the two figures above — arithmetic on this page, not a measurement of its own",
      },
    ],
    caveat:
      "The only number here that will not reproduce on another machine, and the spread is large. It also degrades superlinearly: the candidate search scans every unclaimed settlement for every open bank line, once per tier, with no index. Treat it as an order of magnitude.",
  },

  llm_cost_usd_per_100: {
    key: "llm_cost_usd_per_100",
    label: "LLM cost",
    format: (m) => `$${m.llm_cost_usd_per_100.toFixed(4)}`,
    definition:
      "US dollars billed per 100 records. A dollar figure, not a paise field — it is the one money value in this product that is not integer paise.",
    numerator: "llm_cost_usd × 100",
    denominator: "record_count",
    inputs: (run, m) => [
      { label: "record_count", value: int(run.record_count) },
      {
        label: "llm_tokens_per_100",
        value: int(m.llm_tokens_per_100),
        note: "zero tokens and zero cost together mean nothing was billed. They do not establish that nothing was called: a provider failure and a switched-off analyst bill the same amount.",
      },
    ],
    caveat:
      "Zero here is not a cheap model, and it is not proof of no model either. A run whose analyst was never switched on, a call the provider refused with a 503, and a stubbed client that proposed ten hypotheses through the full pipeline all bill exactly nothing. The analyst view is where the three are separated; on this page a zero means not established.",
  },

  llm_tokens_per_100: {
    key: "llm_tokens_per_100",
    label: "LLM tokens",
    format: (m) => int(m.llm_tokens_per_100),
    definition: "Tokens consumed per 100 records, as an integer.",
    numerator: "llm_tokens × 100",
    denominator: "record_count",
    inputs: (run, m) => [
      { label: "record_count", value: int(run.record_count) },
      {
        label: "llm_cost_usd_per_100",
        value: `$${m.llm_cost_usd_per_100.toFixed(4)}`,
      },
    ],
    caveat:
      "Zero is the absence of a bill, which is weaker than the absence of a call. It is what a switched-off analyst reports, and equally what a call that failed at the provider reports -- the run completes on its deterministic result and no tokens are charged. Whether a model ran is settled on the analyst view, not here.",
  },

  /* ------------------------------------------------------------------ *
   * Input tax credit -- the three rupee figures (spec §6)
   * ------------------------------------------------------------------ *
   *
   * THESE ARE MONEY AND ARE RENDERED THROUGH `formatINR`, which means integer
   * paise in and Indian grouping out. They are the only figures on this
   * scorecard whose unit is rupees rather than a ratio, and they are the reason
   * the project's headline is not only "87.6% of lines reconcile": a settlement
   * the run fails to close moves rupees out of `itc_substantiated_paise` and
   * into `itc_at_risk_paise`, which is what turns the match rate into a money
   * figure.
   *
   * `itc_variance_paise` MAY BE NEGATIVE and the sign is rendered. It is a net
   * position, not an exposure, which is exactly why the contract carries it
   * separately from the at-risk magnitude -- an under-invoiced period and an
   * unmatched one push it in opposite directions and partly cancel.
   */

  itc_substantiated_paise: {
    key: "itc_substantiated_paise",
    label: "ITC substantiated",
    format: (m) => formatINR(m.itc_substantiated_paise),
    definition:
      "Input tax credit this run can stand behind: GST both computed from a settlement the engine MATCHED and covered by a row of the PSP's own tax invoices. Claimable and evidenced, in that order -- either half alone is not enough.",
    numerator: "GST on matched settlements, capped at what the period was invoiced",
    denominator: "not a ratio — integer paise",
    inputs: (run, m) => [
      {
        label: "itc_at_risk_paise",
        value: formatINR(m.itc_at_risk_paise),
        note: "the other side of the same GST. Substantiated plus at-risk accounts for every rupee of GST the run considered.",
      },
      {
        label: "substantiated + at_risk",
        value: formatINR(m.itc_substantiated_paise + m.itc_at_risk_paise),
        note: "arithmetic on this page, not a wire field: it equals max(computed GST, invoiced GST) per period, which is the invariant tests/itc/test_reconcile.py asserts.",
      },
      {
        label: "match_count",
        value: int(run.match_count),
        note: "the coupling — GST is computed only from settlements the engine closed, so this figure moves with the match rate rather than beside it",
      },
    ],
    caveat:
      "A dataset carrying no psp_gst_invoice.csv reports zero here, and that is a valid dataset rather than a failure to substantiate anything. Zero is also what a run that matched nothing would report, for the same reason the false-match rate is trivially 0.0 there — read it beside the at-risk figure and the match count, never alone.",
  },

  itc_at_risk_paise: {
    key: "itc_at_risk_paise",
    label: "ITC at risk",
    format: (m) => formatINR(m.itc_at_risk_paise),
    definition:
      "Input tax credit that is not claimable as evidenced: GST computed but never invoiced, or invoiced but not backed by a settlement the engine could close. A magnitude, so the two causes add rather than cancel.",
    numerator: "uninvoiced GST + GST on settlements the run left open",
    denominator: "not a ratio — integer paise",
    inputs: (run, m) => [
      {
        label: "itc_substantiated_paise",
        value: formatINR(m.itc_substantiated_paise),
      },
      {
        label: "exception_count",
        value: int(run.exception_count),
        note: "the mechanism: a settlement the run could not close carries its GST into this figure, so an exception has a rupee cost and this is where it lands",
      },
      {
        label: "itc_variance_paise",
        value: formatINR(m.itc_variance_paise),
        note: "the same disagreement kept signed instead of taken as a magnitude — the two are different questions and neither substitutes for the other",
      },
    ],
    caveat:
      "This is exposure, not loss. Part of it is a matcher residue that a closed settlement would move straight back into the substantiated column, and part is a genuine invoice defect on the PSP's side; this number does not separate them. The per-period breakdown is what does, and it is not on this contract.",
  },

  itc_variance_paise: {
    key: "itc_variance_paise",
    label: "ITC variance",
    format: (m) => formatINR(m.itc_variance_paise),
    definition:
      "The signed total disagreement between the GST this run computed and the GST the PSP invoiced. Negative means the invoices claim more than the run can compute; positive means the run computed more than was invoiced.",
    numerator: "computed GST − invoiced GST, summed over periods with the sign kept",
    denominator: "not a ratio — integer paise",
    inputs: (_run, m) => [
      {
        label: "itc_at_risk_paise",
        value: formatINR(m.itc_at_risk_paise),
        note: "the same periods as magnitudes. This figure is smaller because opposite-signed periods cancel here and do not cancel there.",
      },
      {
        label: "itc_substantiated_paise",
        value: formatINR(m.itc_substantiated_paise),
      },
    ],
    caveat:
      "A variance near zero does not mean the periods agree — it means they agree ON NET. An under-invoiced month and a month of unclosed settlements push in opposite directions and can cancel to almost nothing while both defects are live. That is precisely why the at-risk figure exists beside it, and why neither is quotable alone.",
  },
};
