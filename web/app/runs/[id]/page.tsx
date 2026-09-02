"use client";

import Link from "next/link";
import { useRun } from "@/components/shell/RunScope";
import { EmptyState } from "@/components/States";
import { HeadlineClaim } from "@/components/summary/HeadlineClaim";
import { SubjectAccounting } from "@/components/summary/SubjectAccounting";
import { Scorecard } from "@/components/summary/Scorecard";
import { InputTaxCredit } from "@/components/summary/InputTaxCredit";
import { isFromUploads, isTerminal } from "@/lib/labels";
import type { Metrics, RunSummary } from "@/lib/types";

/**
 * The run summary.
 *
 * Three registers, in the order a reviewer actually needs them: the claim the
 * run is entitled to make, the accounting that makes it checkable, and the
 * remaining numbers at density. Every figure on the page opens its own
 * derivation in place.
 */
export default function RunSummaryPage() {
  const run = useRun();

  if (!run.metrics) return <NoMetricsYet run={run} />;
  const metrics: Metrics = run.metrics;

  return (
    <div className="space-y-14">
      <HeadlineClaim run={run} metrics={metrics} />
      <SubjectAccounting run={run} metrics={metrics} />
      <Scorecard run={run} metrics={metrics} />
      <InputTaxCredit run={run} metrics={metrics} />
    </div>
  );
}

/**
 * `RunSummary.metrics` is null in THREE different situations, and they are
 * three different things to tell a reader. Rendering 0% in any of them would
 * be indistinguishable from a measured zero, which is the one thing this
 * console must never do — but rendering the same sentence in all three would
 * be nearly as bad, because two of them are normal and one is a failure.
 *
 * * **Still executing.** Not scored yet; the page updates on its own.
 * * **Failed.** Terminal without a scorecard, and that is the failure.
 * * **Reconciled uploaded files.** Terminal, successful, and permanently
 *   unscored: accuracy is measured against ground truth and nobody knows the
 *   right answer to a reconciliation of a merchant's own exports. The run's
 *   findings are real and are on the other five views; what does not exist is
 *   a grade. Presenting this as "no metrics yet" would promise a number that
 *   is never coming.
 */
function NoMetricsYet({ run }: { run: RunSummary }) {
  const failed = run.state === "failed";
  const uploaded = isFromUploads(run) && run.state === "completed";

  if (uploaded) {
    return (
      <div className="space-y-6">
        <EmptyState
          title="This run has no scorecard, and it never will"
          reason={
            <>
              It reconciled files you uploaded. Every rate on this page is
              measured against ground truth — the generator&apos;s own record of
              which order settled into which batch and landed on which bank
              line — and no such record exists for your own exports. So{" "}
              <code className="font-mono">RunSummary.metrics</code> is null,
              this page shows no match rate, and it does not show 0%: a rate
              computed against the run&apos;s own output would be a number
              grading itself.
            </>
          }
        />
        <div className="max-w-[72ch] space-y-3">
          <h2 className="text-sm font-medium tracking-tight">
            What this run does tell you
          </h2>
          <p className="text-xs leading-relaxed text-muted-foreground">
            Everything except how well it did.{" "}
            <Link
              href={`/runs/${run.run_id}/settlements`}
              className="text-brand underline-offset-4 hover:underline focus-visible:focus-ring"
            >
              Settlements
            </Link>{" "}
            carries every batch it reconstructed and the bank credit each one
            closed against — including the narration of why a credit came in
            lower than the dashboard said.{" "}
            <Link
              href={`/runs/${run.run_id}/exceptions`}
              className="text-brand underline-offset-4 hover:underline focus-visible:focus-ring"
            >
              Exceptions
            </Link>{" "}
            carries the{" "}
            <span className="tnum">
              {run.exception_count.toLocaleString("en-IN")}
            </span>{" "}
            subjects it could not close and the reason for each, with the full
            audit trail behind them. Those are findings about your books, and
            they do not depend on anyone knowing the answer in advance.
          </p>
          <p className="text-xs leading-relaxed text-muted-foreground">
            The measured accuracy of this engine is published against the
            generated datasets on the{" "}
            <Link
              href="/method"
              className="text-brand underline-offset-4 hover:underline focus-visible:focus-ring"
            >
              Method
            </Link>{" "}
            page, where ground truth exists and the number means something.
          </p>
        </div>
      </div>
    );
  }

  return (
    <EmptyState
      title={failed ? "This run failed before it produced metrics" : "No metrics yet"}
      reason={
        failed ? (
          <>
            The run reached a terminal state without a scorecard, so there is
            nothing here to read — not a zero, an absence. The exception list
            and the audit trail for whatever it did complete are still on the
            other views.
          </>
        ) : (
          <>
            This run is still executing.{" "}
            <code className="font-mono">RunSummary.metrics</code> is null until
            it finishes, and this page will not show 0% in the meantime: a
            measured zero and an absent number are different claims. It updates
            on its own — the summary is polled every 500&nbsp;ms until the run
            reaches a terminal state.
          </>
        )
      }
      action={
        isTerminal(run.state) ? null : (
          <p className="text-2xs text-muted-foreground">
            Polling <code className="font-mono">GET /api/runs/{run.run_id}</code>
          </p>
        )
      }
    />
  );
}
