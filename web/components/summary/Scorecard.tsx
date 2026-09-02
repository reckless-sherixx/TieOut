"use client";

import { type MetricKey } from "@/lib/derivations";
import type { Metrics, RunSummary } from "@/lib/types";
import { MetricDisclosure } from "@/components/metrics/MetricDisclosure";

/**
 * The rest of the scorecard.
 *
 * Everything the headline claim does not carry, at the density Operate mode
 * wants: one row each, values right-aligned in tabular figures, and each row
 * one click from the same derivation the headline numbers get. Nothing here
 * is a smaller class of number — it is the same treatment in a denser
 * container.
 *
 * The table itself now lives in `MetricDisclosure`, because the analyst view
 * needs exactly this and two tables that merely resemble each other would
 * imply a difference that is not there.
 */
const ROWS: readonly MetricKey[] = [
  "assisted_match_rate",
  "precision",
  "exception_rate",
  "llm_rejection_rate",
  "throughput_records_per_sec",
  "llm_cost_usd_per_100",
  "llm_tokens_per_100",
];

export function Scorecard({
  run,
  metrics,
}: {
  run: RunSummary;
  metrics: Metrics;
}) {
  return (
    <section aria-labelledby="scorecard-heading" className="space-y-6">
      <div className="space-y-2">
        <h2
          id="scorecard-heading"
          className="text-base font-medium tracking-tight"
        >
          The rest of the scorecard
        </h2>
        <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
          Open any row for its definition, its numerator over its denominator,
          the values from this run that feed it, and what it does not prove.
        </p>
      </div>

      <MetricDisclosure
        id="scorecard"
        rows={ROWS}
        run={run}
        metrics={metrics}
        caption="Secondary metrics, each expandable to its derivation"
      />
    </section>
  );
}
