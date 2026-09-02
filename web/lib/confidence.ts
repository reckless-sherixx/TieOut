/**
 * The only place a confidence becomes words.
 *
 * `lib/tiers.ts` used to carry a hardcoded copy of these numbers, and the LLM
 * row rendered the word **"verified"** where the engine stamps **0.70** — a
 * reassurance substituted for a measurement, in the one slot a reviewer reads
 * to find the model's own uncertainty. Measured 2026-09-02 on a live run:
 *
 *     tier   engine stamps   the table said
 *     T0     1.0             "1.00"
 *     T2     0.99            "0.99"
 *     T3     0.8             "0.80"
 *     LLM    0.7             "verified"   <-- the defect
 *
 * Nothing bound the two together, so nothing caught the drift. Everything here
 * is derived from `RunSummary.tier_confidence`, which the API computes from the
 * run's own matches. This module asserts no figure of its own.
 */

/** One tier's confidence on one run, straight off the wire. */
export type TierConfidence = {
  confidence_observed: number | null;
  confidence_conflict: boolean;
};

export type ConfidenceLabel = {
  /** Fixed to two places, or an em dash when there is nothing to show. */
  value: string;
  /** What the number means on THIS rung, in one sentence. */
  meaning: string;
  /** False whenever `value` is not a figure this run produced. */
  measured: boolean;
};

/**
 * Why 1.00 is not a model score, and why saying so matters.
 *
 * T0 fires only when the bank line NAMES the settlement and the reconstruction
 * closes to the exact paise. Both holding leaves no residual uncertainty for a
 * probability to express, so 1.00 is an identity rather than a confidence. Read
 * as a model's self-assessment — which is what the bare column heading invited —
 * 1.00 across 83% of matches is the signature of an overfitted model, and that
 * is exactly how it was read. The number was always honest; the framing was not.
 */
const MEANING: Record<string, string> = {
  "1.00":
    "The reference resolved and the reconstruction closes to the exact paise. No model ran on this rung — this is an identity, not a probability, and there is no residual uncertainty for it to express.",
  "0.99":
    "The reconstruction closes exactly from two or more legs, with no reference corroborating it. The withheld point is the chance that a different leg set closes to the same net.",
  "0.95":
    "The reconstruction closes exactly from a single leg, inside the date window, with exactly one candidate.",
  "0.80":
    "Accepted inside the ±₹1 tolerance. The residual is recorded on the match.",
  "0.70":
    "Proposed by the analyst and accepted by all six verifier checks. This is the engine's figure — the model's own self-assessment is never read by any check.",
};

export function confidenceLabel(
  tier: TierConfidence | undefined,
): ConfidenceLabel {
  if (!tier || (tier.confidence_observed === null && !tier.confidence_conflict)) {
    return {
      value: "—",
      meaning:
        "No match from this rung on this run, so there is nothing to report. That is a result, not a gap.",
      measured: false,
    };
  }

  if (tier.confidence_conflict) {
    return {
      value: "—",
      meaning:
        "This rung stamped more than one confidence on this run. A single figure would be true of some of its matches and false of the rest, so none is shown.",
      measured: false,
    };
  }

  const fixed = (tier.confidence_observed as number).toFixed(2);
  return {
    value: fixed,
    meaning:
      MEANING[fixed] ??
      "Stamped by the engine on every match from this rung on this run.",
    measured: true,
  };
}
