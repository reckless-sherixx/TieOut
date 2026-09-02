"use client";

import { type MetricKey } from "@/lib/derivations";
import { formatINR } from "@/lib/money";
import type { Metrics, RunSummary } from "@/lib/types";
import { MetricDisclosure } from "@/components/metrics/MetricDisclosure";

/**
 * The three rupee figures, which are the reason this is a finance product.
 *
 * Every other number on this page is a ratio. These are money — integer paise
 * on the wire, Indian grouping on screen, through the app's one formatter — and
 * they are what makes the match rate cost something: GST is computed only from
 * settlements the engine CLOSED, so a settlement it cannot close moves rupees
 * out of `itc_substantiated_paise` and into `itc_at_risk_paise`. The headline
 * is not only "87.6% of lines reconcile"; it is also "₹39,330 of credit is
 * substantiated and ₹12,067 is not".
 *
 * Same treatment as every other metric on this console: one row each, each row
 * one interaction from its own definition, numerator over denominator, the wire
 * values that feed it and what it does not prove. Not three stat tiles — the
 * at-risk figure and the variance are the same disagreement asked two different
 * ways, and a tile grid would present them as three independent results.
 */
const ROWS: readonly MetricKey[] = [
  "itc_substantiated_paise",
  "itc_at_risk_paise",
  "itc_variance_paise",
];

export function InputTaxCredit({
  run,
  metrics,
}: {
  run: RunSummary;
  metrics: Metrics;
}) {
  // Arithmetic on this page, and labelled as such below rather than presented
  // as a wire field. Per period the engine asserts
  // `substantiated + at_risk == max(computed_gst, invoiced_gst)`; the two
  // operands of that max are not on this contract, so the sum is shown and the
  // invariant is named, not checked.
  const considered = metrics.itc_substantiated_paise + metrics.itc_at_risk_paise;

  return (
    <section aria-labelledby="itc-heading" className="space-y-6">
      <div className="space-y-2">
        <h2 id="itc-heading" className="text-base font-medium tracking-tight">
          Input tax credit, in rupees
        </h2>
        <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
          GST the run can stand behind, GST it cannot, and the signed gap
          against the PSP&apos;s own tax invoices.
        </p>
        
      </div>

      <MetricDisclosure
        id="itc"
        rows={ROWS}
        run={run}
        metrics={metrics}
        caption="Input tax credit figures, each expandable to its derivation"
      />

      <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
        Substantiated and at risk sum to{" "}
        <span className="money font-medium text-foreground">
          {formatINR(considered)}
        </span>
        {" "}— every rupee of GST this run considered.
      </p>

    </section>
  );
}
