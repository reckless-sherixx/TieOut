import type { Metrics } from "./types";

/**
 * A figure and everything a reader needs in order not to misread it.
 *
 * The console's four headline numbers were rendered as bare percentages, and
 * one of them — `trap_capture_rate` — divides by the count of deliberately
 * unresolvable subjects in the dataset. That count is **2** at 50 records and
 * **10** at 500. `100.0%` of 2 and `100.0%` of 100 are the same string, so a
 * reader has no way to tell a strong result from a trivial one, and the
 * reasonable reaction to an unexplained 100% is to distrust it.
 */
export type MetricShape = {
  label: string;
  value: string;
  /** The counts behind the rate. Null when the rate has no natural pair. */
  denominator: string | null;
  /** Shown inline and small — never hidden behind a hover. */
  caveat: string | null;
};

const DASH = "—";

function pct(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

/**
 * `trap_capture_rate`, with the denominator it divided by.
 *
 * Deliberately NOT smoothed: a small denominator is reported as small rather
 * than rounded into confidence, because the honest reading of "2 of 2" is
 * different from the honest reading of "100 of 100" and the console's job is
 * to let a reviewer tell them apart.
 */
export function trapShape(metrics: Metrics | null | undefined): MetricShape {
  const label = "Trap capture rate";

  if (!metrics || metrics.trap_capture_rate == null) {
    return { label, value: DASH, denominator: null, caveat: null };
  }

  const rate = metrics.trap_capture_rate;
  const value = pct(rate);
  const total = metrics.total_traps;

  // A run stored before `total_traps` existed. "Not recorded" is not "zero".
  if (total == null) {
    return { label, value, denominator: null, caveat: null };
  }

  if (total === 0) {
    return {
      label,
      value,
      denominator: "no traps in this dataset",
      caveat:
        "Declining a trap that does not exist is not an achievement. This reads 100% by definition rather than by measurement.",
    };
  }

  return {
    label,
    value,
    denominator: `${Math.round(rate * total)} of ${total} traps`,
    caveat:
      total < 20
        ? "A small denominator. This is every deliberately unresolvable subject in the dataset, not a sample of them — but it is still a handful, and the larger datasets are the stronger evidence."
        : null,
  };
}

/**
 * The other three headline figures. They divide by subject counts in the
 * hundreds or thousands, so their denominators are informative rather than
 * load-bearing — but they are shown for the same reason: a rate whose
 * denominator is invisible cannot be checked.
 */
export function rateShape(
  label: string,
  rate: number | null | undefined,
  denominator: string | null = null,
  caveat: string | null = null,
): MetricShape {
  if (rate == null) return { label, value: DASH, denominator: null, caveat: null };
  return { label, value: pct(rate), denominator, caveat };
}
