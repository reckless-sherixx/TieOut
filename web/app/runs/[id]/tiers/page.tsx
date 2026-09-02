"use client";

import Link from "next/link";
import { useRun } from "@/components/shell/RunScope";
import { EmptyState } from "@/components/States";
import { ViewIntro } from "@/components/shell/ViewIntro";
import { TierLadder } from "@/components/tiers/TierLadder";

/**
 * The tier walk, and what it means.
 *
 * The counting unit here is the MATCH GROUP, from the engine's own tier
 * assignment — deliberately not the subject counts the summary reports, which
 * is why the two views live apart and each names its own denominator.
 *
 * Two registers, in the order a reviewer needs them: what each rung produced on
 * this run with the rule it applied one interaction away, and what the rungs
 * decompose onto — which is a measurement of one reference dataset and is
 * labelled as such everywhere it appears.
 */
export default function TiersPage() {
  const run = useRun();

  return (
    <div className="space-y-14">
      <ViewIntro
        title="Matches by tier"
        lede="Four deterministic rungs and one assisted, run in order. A subject leaves the pool the moment an earlier rung claims it."
      />

      {run.metrics ? (
        <>
          <section aria-labelledby="ladder-heading" className="space-y-6">
            <div className="space-y-2">
              <h2
                id="ladder-heading"
                className="text-base font-medium tracking-tight"
              >
                What each rung produced
              </h2>
              <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
                Open any row for the rule that rung applies, the confidence the
                engine stamped on it, and what makes a line fall through.
              </p>
            </div>

            <TierLadder
              tierCounts={run.metrics.tier_counts}
              tierConfidence={run.tier_confidence}
              matchCount={run.match_count}
            />
          </section>

        </>
      ) : (
        <EmptyState
          title={
            run.state === "failed"
              ? "This run failed before it produced a tier breakdown"
              : "No tier counts yet"
          }
          reason={
            run.state === "failed" ? (
              <>
                The run reached a terminal state without a scorecard, so there is
                no <code className="font-mono">tier_counts</code> to render — an
                absence, not five zeros. Five empty rungs would read as a run in
                which nothing matched anywhere, which is a different claim
                entirely.
              </>
            ) : (
              <>
                <code className="font-mono">RunSummary.metrics</code> is null
                until the run finishes and{" "}
                <code className="font-mono">tier_counts</code> lives on it. This
                view updates on its own: the summary is polled every 500&nbsp;ms
                until the run reaches a terminal state.
              </>
            )
          }
          action={
            <p className="text-2xs text-muted-foreground">
              The rules each rung applies do not depend on a run.{" "}
              <Link
                href="/method"
                className="rounded-sm underline underline-offset-4 transition-colors duration-150 hover:text-foreground focus-visible:focus-ring"
              >
                The method page
              </Link>{" "}
              carries the ladder in full.
            </p>
          }
        />
      )}
    </div>
  );
}
