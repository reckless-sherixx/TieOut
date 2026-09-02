"use client";

import { CircleSlashIcon, CheckIcon, ShieldIcon, ClockIcon } from "lucide-react";
import {
  analystVerdict,
  ANALYST_STATE_EXPLANATION,
  type AnalystState,
} from "@/lib/analyst";
import type { HypothesisCensus } from "@/lib/census";
import type { Metrics } from "@/lib/types";

/**
 * Which of the analyst outcomes this run is in, and the wire facts that decide.
 *
 * A sentence, then the evidence, then what the evidence does not settle. Not a
 * status tile: the difference between these outcomes is an argument made of
 * four fields, and a coloured badge reading "0" would be the exact failure this
 * view exists to avoid.
 *
 * The icon is the only ornament and it carries one bit — whether the outcome is
 * an absence, a refusal, or an acceptance — at the same stroke weight as every
 * other icon in the console.
 */
const STATE_ICON: Record<AnalystState, typeof CheckIcon> = {
  accepted: CheckIcon,
  "all-rejected": ShieldIcon,
  "called-silently": CircleSlashIcon,
  "no-evidence": CircleSlashIcon,
  undetermined: ClockIcon,
};

export function AnalystVerdict({
  metrics,
  census,
}: {
  metrics: Metrics;
  census: HypothesisCensus | null;
}) {
  const verdict = analystVerdict(metrics, census);
  const Icon = STATE_ICON[verdict.state];

  return (
    <section aria-labelledby="verdict-heading" className="space-y-6">
      <div className="space-y-3">
        <h2
          id="verdict-heading"
          className="flex items-start gap-2.5 text-base font-medium tracking-tight"
        >
          <Icon
            aria-hidden
            strokeWidth={2}
            className="mt-1 size-4 shrink-0 text-muted-foreground"
          />
          <span>{verdict.title}</span>
        </h2>
        <p className="max-w-[72ch] pl-6.5 text-xs leading-relaxed">
          {ANALYST_STATE_EXPLANATION[verdict.state]}
        </p>
      </div>

      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full min-w-[42rem] border-collapse text-left">
          <caption className="sr-only">
            The wire values this reading is based on, and what each one rules in
            or out
          </caption>
          <thead>
            <tr className="border-b border-border bg-surface">
              <th
                scope="col"
                className="w-64 py-2.5 pr-4 pl-4 text-2xs font-medium text-muted-foreground"
              >
                Field
              </th>
              <th
                scope="col"
                className="w-32 py-2.5 pr-4 text-right text-2xs font-medium text-muted-foreground"
              >
                This run
              </th>
              <th
                scope="col"
                className="py-2.5 pr-4 text-2xs font-medium text-muted-foreground"
              >
                What it settles
              </th>
            </tr>
          </thead>
          <tbody>
            {verdict.evidence.map((fact) => (
              <tr
                key={fact.label}
                className="border-b border-border last:border-b-0"
              >
                <td className="py-2.5 pr-4 pl-4 align-top font-mono text-2xs break-words">
                  {fact.label}
                </td>
                <td className="tnum py-2.5 pr-4 text-right align-top text-xs font-medium">
                  {fact.value}
                </td>
                <td className="py-2.5 pr-4 align-top text-xs leading-relaxed text-muted-foreground">
                  {fact.implication}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
        One thing this contract cannot settle:{" "}
        <code className="font-mono">use_llm</code> is a field on the request that
        starts a run and <code className="font-mono">RunSummary</code> does not
        echo it back, so no view can prove the analyst was switched off — only
        that nothing here is evidence it was switched on.{" "}
        <code className="font-mono">RunStatus.stage</code> does carry a
        free-text account of what happened to the analyst — including a missing
        credential and a provider that refused the call, neither of which is a
        count and neither of which any typed field here can see. It is
        reproduced verbatim in the next section and is never parsed: no verdict
        above is computed from it, because recovering a number from prose is the
        same mistake as recovering{" "}
        <code className="font-mono">failed_check</code> from{" "}
        <code className="font-mono">verifier_reason</code>.
      </p>
    </section>
  );
}
