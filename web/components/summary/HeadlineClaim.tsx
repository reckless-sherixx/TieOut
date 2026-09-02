"use client";

import * as React from "react";
import { CheckIcon, TriangleAlertIcon } from "lucide-react";
import { DERIVATIONS, type MetricKey } from "@/lib/derivations";
import { formatRate } from "@/lib/money";
import { cn } from "@/lib/utils";
import type { Metrics, RunSummary } from "@/lib/types";
import { DerivationPanel } from "@/components/summary/DerivationPanel";
import { Caveat } from "@/components/ui/Caveat";
import { trapShape } from "@/lib/metric-shape";

/**
 * The run's result, as the one claim it is entitled to make.
 *
 * Not four stat tiles. METRICS.md §6 is unambiguous that each of these
 * numbers is worthless alone — an engine that matches nothing scores a
 * perfect false-match rate AND a perfect trap-capture rate — and that only
 * the combination is a claim. Four equal tiles say the opposite: they say
 * here are four independent numbers, take whichever you like. A sentence
 * cannot be quoted a quarter at a time.
 *
 * Each number in it opens its own derivation directly underneath, so the
 * reader never leaves the sentence to find out what a number counts.
 *
 * The four are the auto-match rate, recall on resolvable, the false-match
 * rate and the trap-capture rate. They appear in the prose below rather than
 * in a list, because the order they are read in is the argument.
 *
 * The two paragraphs that used to follow this sentence are now folded. They
 * are not gone — they are one interaction away, and still in the DOM — but a
 * reader arriving at a run wants the claim, not the defence of the claim.
 */
export function HeadlineClaim({
  run,
  metrics,
}: {
  run: RunSummary;
  metrics: Metrics;
}) {
  const [open, setOpen] = React.useState<MetricKey | null>(null);
  const panelId = "claim-derivation";

  // A checkable invariant, not a coincidence: the two rates are
  // numerator-different and coincide exactly while no false match exists.
  const holds =
    Math.abs(metrics.auto_match_rate - metrics.recall_on_resolvable) < 1e-9;

  // `100.0%` of 2 and `100.0%` of 100 are the same string. The denominator is
  // what tells them apart, and it belongs beside the figure, not in a footnote.
  const trap = trapShape(metrics);

  const number = (key: MetricKey, trailing?: string) => (
    <ClaimNumber
      key={key}
      metricKey={key}
      value={DERIVATIONS[key].format(metrics)}
      trailing={trailing}
      open={open === key}
      panelId={panelId}
      onToggle={() => setOpen((cur) => (cur === key ? null : key))}
    />
  );

  return (
    <section aria-labelledby="claim-heading">
      <h2 id="claim-heading" className="sr-only">
        What this run measured
      </h2>

      {/* The measure is set in rem: `ch` would resolve against this element's
          own 18px, and the sentence would run wider than it reads. */}
      <p className="max-w-[44rem] text-lg leading-[1.75] text-balance">
        Of the subjects this dataset determines, the engine resolved{" "}
        {number("auto_match_rate", ",")} and {number("recall_on_resolvable")} of
        them correctly. It asserted {number("false_match_rate")} wrong matches,
        and declined {number("trap_capture_rate")} of the subjects the dataset
        does <strong className="font-semibold">not</strong> determine
        {trap.denominator ? (
          <span className="text-muted-foreground">
            {" "}
            <span className="tnum text-sm">({trap.denominator})</span>
          </span>
        ) : null}
        .
      </p>

      <Caveat summary="Why all four, or none">
        <p>
          None of those four numbers is a result on its own. An engine that
          matches nothing scores a false-match rate of 0.0% and a trap-capture
          rate of 100% — that is measured, not hypothetical. Quote all four or
          none. Every one of them opens its own arithmetic.
        </p>
        {trap.caveat ? <p>{trap.caveat}</p> : null}
      </Caveat>

      <Invariant holds={holds} metrics={metrics} />

      <div
        id={panelId}
        className={cn(
          "mt-6 overflow-hidden rounded-xl border border-border bg-card",
          open === null && "hidden",
        )}
      >
        {open !== null ? (
          <>
            <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 border-b border-border bg-surface px-6 py-3.5">
              <h3 className="text-sm font-medium tracking-tight">
                {DERIVATIONS[open].label}
              </h3>
              <p className="tnum font-mono text-xs">
                {DERIVATIONS[open].format(metrics)}
              </p>
            </div>
            <div className="px-6 py-6">
              <DerivationPanel
                derivation={DERIVATIONS[open]}
                run={run}
                metrics={metrics}
              />
            </div>
          </>
        ) : null}
      </div>
    </section>
  );
}

/**
 * A number in the sentence that opens its own derivation.
 *
 * A dotted rule under it is the affordance; it goes solid on hover and focus
 * and the number takes the selected ground while its panel is open, so at a
 * glance you can see which of the four you are reading about.
 */
function ClaimNumber({
  metricKey,
  value,
  trailing,
  open,
  panelId,
  onToggle,
}: {
  metricKey: MetricKey;
  value: string;
  /** Punctuation that must never wrap away from the number it follows. */
  trailing?: string;
  open: boolean;
  panelId: string;
  onToggle: () => void;
}) {
  return (
    <span className="whitespace-nowrap">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={panelId}
        className={cn(
          "tnum -mx-1 rounded-md px-1 font-medium underline decoration-dotted decoration-1 underline-offset-[6px]",
          "transition-colors duration-150",
          "hover:decoration-solid focus-visible:focus-ring focus-visible:decoration-solid",
          open
            ? "bg-surface-selected decoration-solid"
            : "hover:bg-surface-hover active:bg-surface-active",
        )}
      >
        {value}
        <span className="sr-only">
          {" "}
          — {open ? "hide" : "show"} the derivation of{" "}
          {DERIVATIONS[metricKey].label}
        </span>
      </button>
      {trailing}
    </span>
  );
}

/**
 * The one relationship between these numbers that is checkable from the wire
 * alone, checked live rather than asserted in prose.
 */
function Invariant({ holds, metrics }: { holds: boolean; metrics: Metrics }) {
  const Icon = holds ? CheckIcon : TriangleAlertIcon;
  return (
    <p
      className={cn(
        "mt-4 flex max-w-[72ch] items-start gap-2 text-xs leading-relaxed",
        holds ? "text-muted-foreground" : "text-error-fg",
      )}
    >
      <Icon
        aria-hidden
        className={cn("mt-0.5 size-3.5 shrink-0", holds && "text-matched")}
        strokeWidth={2}
      />
      <span>
        {holds ? (
          <>
            <span className="font-medium text-foreground">
              Invariant holds.
            </span>{" "}
            Auto-match and recall on resolvable are equal (
            <span className="tnum font-mono">
              {formatRate(metrics.auto_match_rate, 4)}
            </span>
            ).
            <Caveat summary="What that equality proves" className="mt-1.5">
              <p>
                The two rates are numerator-different — matched, versus matched
                correctly — so they coincide only while the false-match rate is
                0. Checked live against the wire on every render rather than
                asserted in prose, which means a run that broke it would say so
                here instead of reading like a run that did not.
              </p>
            </Caveat>
          </>
        ) : (
          <>
            <span className="font-medium">Invariant broken.</span> The
            auto-match rate (
            <span className="tnum font-mono">
              {formatRate(metrics.auto_match_rate, 4)}
            </span>
            ) and recall on resolvable (
            <span className="tnum font-mono">
              {formatRate(metrics.recall_on_resolvable, 4)}
            </span>
            ) have diverged, which means this run produced at least one match
            that disagrees with ground truth. Check the false-match rate and
            the exception list before reading anything else on this page.
          </>
        )}
      </span>
    </p>
  );
}
