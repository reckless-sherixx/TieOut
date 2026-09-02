"use client";

import * as React from "react";
import { ChevronRightIcon } from "lucide-react";
import { DERIVATIONS, type MetricKey } from "@/lib/derivations";
import { cn } from "@/lib/utils";
import type { Metrics, RunSummary } from "@/lib/types";
import { DerivationPanel } from "@/components/summary/DerivationPanel";

/**
 * A set of metrics at density, each one row and each one click from its own
 * arithmetic.
 *
 * Extracted so the summary and the analyst view are literally the same table
 * rather than two tables that resemble each other. A metric that looks one way
 * on one screen and another way on the next teaches a reader that the
 * difference means something, and here it would not.
 *
 * `id` scopes the generated panel ids, because two of these can appear in one
 * document and `aria-controls` has to point somewhere unique.
 */
export function MetricDisclosure({
  id,
  rows,
  run,
  metrics,
  caption,
}: {
  id: string;
  rows: readonly MetricKey[];
  run: RunSummary;
  metrics: Metrics;
  caption: string;
}) {
  const [open, setOpen] = React.useState<MetricKey | null>(null);

  return (
    <div className="overflow-hidden rounded-xl border border-border">
      <table className="w-full border-collapse text-left">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-border bg-surface">
            <th
              scope="col"
              className="w-8 py-2.5 pl-4 text-2xs font-medium text-muted-foreground"
            >
              <span className="sr-only">Expand</span>
            </th>
            <th
              scope="col"
              className="w-48 py-2.5 pr-4 text-2xs font-medium text-muted-foreground"
            >
              Metric
            </th>
            <th
              scope="col"
              className="hidden py-2.5 pr-4 text-2xs font-medium text-muted-foreground sm:table-cell"
            >
              Counts
            </th>
            <th
              scope="col"
              className="w-32 py-2.5 pr-4 text-right text-2xs font-medium text-muted-foreground"
            >
              Value
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((key) => {
            const d = DERIVATIONS[key];
            const isOpen = open === key;
            const panelId = `${id}-derivation-${key}`;
            return (
              <React.Fragment key={key}>
                <tr
                  className={cn(
                    "border-b border-border transition-colors duration-150",
                    isOpen ? "bg-surface-selected" : "hover:bg-surface-hover",
                  )}
                >
                  <td className="py-0 pl-4">
                    <button
                      type="button"
                      aria-expanded={isOpen}
                      aria-controls={panelId}
                      onClick={() => setOpen((cur) => (cur === key ? null : key))}
                      className="flex size-6 items-center justify-center rounded-md text-muted-foreground transition-colors duration-150 hover:bg-surface-active hover:text-foreground focus-visible:focus-ring"
                    >
                      <ChevronRightIcon
                        aria-hidden
                        strokeWidth={2}
                        className={cn(
                          "size-3.5 transition-transform duration-150",
                          isOpen && "rotate-90",
                        )}
                      />
                      <span className="sr-only">
                        {isOpen ? "Hide" : "Show"} the derivation of {d.label}
                      </span>
                    </button>
                  </td>
                  <td className="py-2.5 pr-4 text-xs font-medium sm:whitespace-nowrap">
                    {d.label}
                  </td>
                  <td className="hidden py-2.5 pr-4 text-xs text-muted-foreground sm:table-cell">
                    {d.numerator}
                  </td>
                  <td className="tnum py-2.5 pr-4 text-right text-xs font-medium">
                    {d.format(metrics)}
                  </td>
                </tr>
                <tr
                  id={panelId}
                  className={cn("border-b border-border", !isOpen && "hidden")}
                >
                  <td colSpan={4} className="px-4 py-6 sm:px-6">
                    {isOpen ? (
                      <DerivationPanel
                        derivation={d}
                        run={run}
                        metrics={metrics}
                      />
                    ) : null}
                  </td>
                </tr>
              </React.Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
