/**
 * DRIFT, AS SOMETHING TO READ.
 *
 * `GET /api/runs/{id}/drift` answers "what changed since last time" — the
 * question a finance controller actually asks. A match rate that falls from 98%
 * to 91% overnight because a new deduction type appeared is the finding; the
 * 91% on its own is not.
 *
 * THREE RULES THIS MODULE OBEYS AND THE VIEW INHERITS.
 *
 * 1. **`material` is the API's word, never this module's.** Detection is
 *    deterministic and lives in `core/drift/compare.py` against named
 *    thresholds. Nothing here recomputes it, second-guesses it, or hides a move
 *    the API flagged. The thresholds are DESCRIBED below so a reader can see
 *    what a flag means — describing is not deciding.
 *
 * 2. **`narrative` may be null and the UI must not fill the gap.** The endpoint
 *    runs no model, so it always answers `narrative: null`. A view that wrote
 *    its own summary prose into that slot would be doing precisely what the
 *    contract separates `narrative` from `material` to forbid: presenting
 *    written-over-the-facts language as though it came from the detector.
 *
 * 3. **`MetricMove.metric` is a plain `string`, not the union of known keys.**
 *    A run recorded before a metric existed must still be comparable. So every
 *    lookup here degrades to the wire name rather than throwing or dropping the
 *    row — an unrecognised metric renders as itself, which is more information
 *    than an omitted row.
 *
 * WHY `before`/`after`/`delta` ARE `number` EVEN FOR THE PAISE FIELDS. A
 * `MetricMove` is a comparison record, not a money field: it carries rates, a
 * throughput, a dollar cost and an integer-paise count in the same three slots.
 * The contract says so and says why — every paise figure this system produces
 * is far below 2**53, so a double holds it exactly. Money stays an integer
 * everywhere it is money.
 */
import { formatINR, formatRate } from "./money";
import type { MetricMove } from "./types";

const int = (n: number) => n.toLocaleString("en-IN");

/**
 * How a metric's three numbers should be read.
 *
 * `paise` is the reason this exists: rendering `itc_variance_paise` as `-119429`
 * beside an auto-match rate of `0.8757` invites the reader to compare two
 * numbers that are not in the same units, and the rupee figures are the half of
 * this report a controller came for.
 */
export type MetricUnit = "rate" | "paise" | "usd" | "count" | "throughput";

/**
 * The materiality rule the API applies to each metric, from
 * `core/drift/compare.py`. Named constants, quoted here so the UI can say what
 * a flag means rather than leaving `material: true` as an unexplained badge.
 */
export type MaterialityRule = "rate" | "exact" | "magnitude" | "never";

export const RATE_MATERIAL_DELTA = 0.01;
export const MAGNITUDE_MATERIAL_RATIO = 0.05;

export const MATERIALITY_RULE_NOTE: Record<MaterialityRule, string> = {
  rate: "Bounded 0.0–1.0, so an absolute threshold means the same thing at every scale: a move of 0.01 or more is material. At 500 records that is about 1.6 subjects.",
  exact:
    "This metric has a documented correct value — 0.0 for the false-match rate, 1.0 for precision and trap capture — so ANY change at all is material. A false-match rate moving from 0.0 to 0.004 is four wrong matches appearing where there were none, and it would hide under the 0.01 rate threshold.",
  magnitude:
    "An unbounded magnitude, held to a relative ratio of 5% rather than an absolute delta: 0.01 absolute would fire on one paise and on a single token. A zero baseline has no ratio, so any appearance is material.",
  never:
    "Reported with its before, after and delta, and never flagged material. Wall-clock throughput on shared hardware is the one figure in this project that does not reproduce on another machine, and one run against one run is not the benchmark method that would support calling a change a finding.",
};

type MetricProfile = {
  label: string;
  unit: MetricUnit;
  rule: MaterialityRule;
  /** What the metric counts, in one clause. */
  note: string;
};

/**
 * Every numeric field of `Metrics`, in that contract's declaration order —
 * which is the order the API emits moves in, and the order they render in.
 */
export const METRIC_PROFILES: Record<string, MetricProfile> = {
  auto_match_rate: {
    label: "Auto-match rate",
    unit: "rate",
    rule: "rate",
    note: "Resolvable subjects the deterministic tiers closed, with no model involved.",
  },
  assisted_match_rate: {
    label: "Assisted match rate",
    unit: "rate",
    rule: "rate",
    note: "Resolvable subjects closed by an LLM hypothesis the verifier accepted.",
  },
  exception_rate: {
    label: "Exception rate",
    unit: "rate",
    rule: "rate",
    note: "Subjects the run did not match, over a denominator drawn from truth.",
  },
  false_match_rate: {
    label: "False-match rate",
    unit: "rate",
    rule: "exact",
    note: "Matches that disagree with the recorded linkage. Its correct value is 0.0.",
  },
  precision: {
    label: "Precision",
    unit: "rate",
    rule: "exact",
    note: "1 − false-match rate. Its correct value is 1.0, and it reports the same event twice on purpose.",
  },
  recall_on_resolvable: {
    label: "Recall on resolvable",
    unit: "rate",
    rule: "rate",
    note: "Resolvable subjects matched CORRECTLY, checked against the linkage.",
  },
  trap_capture_rate: {
    label: "Trap capture rate",
    unit: "rate",
    rule: "exact",
    note: "Unresolvable-by-construction subjects the engine correctly declined. Its correct value is 1.0.",
  },
  llm_rejection_rate: {
    label: "LLM rejection rate",
    unit: "rate",
    rule: "rate",
    note: "Hypotheses the verifier refused, over hypotheses proposed.",
  },
  throughput_records_per_sec: {
    label: "Throughput",
    unit: "throughput",
    rule: "never",
    note: "Records per second through the matching stage, excluding LLM latency.",
  },
  llm_cost_usd_per_100: {
    label: "LLM cost",
    unit: "usd",
    rule: "magnitude",
    note: "US dollars per 100 records. The one money figure here that is not integer paise.",
  },
  llm_tokens_per_100: {
    label: "LLM tokens",
    unit: "count",
    rule: "magnitude",
    note: "Tokens consumed per 100 records.",
  },
  itc_substantiated_paise: {
    label: "ITC substantiated",
    unit: "paise",
    rule: "magnitude",
    note: "Input tax credit both computed from a matched settlement and covered by a PSP invoice.",
  },
  itc_at_risk_paise: {
    label: "ITC at risk",
    unit: "paise",
    rule: "magnitude",
    note: "GST computed but not invoiced, or invoiced but not substantiated by a settlement the engine closed.",
  },
  itc_variance_paise: {
    label: "ITC variance",
    unit: "paise",
    rule: "magnitude",
    note: "Signed net disagreement between computed and invoiced GST. May be negative.",
  },
};

/**
 * The profile for a metric name off the wire.
 *
 * An unknown name is a metric this build has never heard of — a run recorded
 * against a newer contract, which the API deliberately allows. It renders under
 * its own wire name with no unit assumed, rather than being dropped.
 */
export function profileOf(metric: string): MetricProfile {
  return (
    METRIC_PROFILES[metric] ?? {
      label: metric,
      unit: "count",
      rule: "rate",
      note: "This build does not carry a description for this metric. It is rendered under the name the API sent.",
    }
  );
}

/** One of a move's three numbers, in the metric's own units. */
export function formatMetricValue(unit: MetricUnit, value: number): string {
  switch (unit) {
    case "rate":
      return formatRate(value, 2);
    case "paise":
      // Money, so Indian grouping and two decimal places, through the app's one
      // money formatter. `Math.round` is defensive only: these are integers on
      // the wire and a `number` slot cannot promise that at the type level.
      return formatINR(Math.round(value));
    case "usd":
      return `$${value.toFixed(4)}`;
    case "throughput":
      return `${int(Math.round(value))} rec/s`;
    case "count":
      return int(Math.round(value));
  }
}

/**
 * A move's delta, always signed — including the `+`.
 *
 * A delta is a direction as much as a magnitude, and `327179` and `+327179`
 * read differently at a glance in a column that also holds negatives.
 *
 * THE MINUS IS THE ASCII HYPHEN, NOT U+2212, and that is not typographic
 * carelessness. `formatINR` writes a negative rupee figure as `-₹1,194.29`, and
 * the drift table puts a baseline value and a delta in adjacent columns of the
 * same row — a typographic minus here and a hyphen one column over is two
 * different minus signs on one line. There is one money formatter in this app;
 * this follows its sign.
 */
export function formatMetricDelta(unit: MetricUnit, delta: number): string {
  if (delta === 0) return formatMetricValue(unit, 0);
  const body = formatMetricValue(unit, Math.abs(delta));
  return `${delta > 0 ? "+" : "-"}${body}`;
}

/**
 * `after / before − 1`, or null when the baseline is zero.
 *
 * Shown only for the magnitude metrics, because that is the only family whose
 * threshold is a ratio — putting a percentage change beside a rate that already
 * IS a percentage would be two different percentages in one cell. Null when
 * `before` is 0: there is no ratio to a zero baseline, which is exactly why the
 * API treats any appearance from zero as material.
 */
export function relativeMove(move: MetricMove): number | null {
  if (move.before === 0) return null;
  return move.after / move.before - 1;
}

export type DriftSplit = {
  material: MetricMove[];
  unchanged: MetricMove[];
};

/**
 * The moves the API flagged, and the rest.
 *
 * `material` is read straight off the wire and never recomputed. The order
 * within each group is the order the API sent, which is the declaration order
 * of `Metrics` — accuracy rates first, then throughput and cost, then the rupee
 * figures.
 */
export function splitMoves(moves: readonly MetricMove[]): DriftSplit {
  const material: MetricMove[] = [];
  const unchanged: MetricMove[] = [];
  for (const move of moves) (move.material ? material : unchanged).push(move);
  return { material, unchanged };
}
