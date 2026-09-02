"use client";

import Link from "next/link";
import { useRun } from "@/components/shell/RunScope";
import { EmptyState } from "@/components/States";
import { HeadlineClaim } from "@/components/summary/HeadlineClaim";
import { SubjectAccounting } from "@/components/summary/SubjectAccounting";
import { Scorecard } from "@/components/summary/Scorecard";
import { InputTaxCredit } from "@/components/summary/InputTaxCredit";
import { analystVerdict } from "@/lib/analyst";
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
      <StandingLimits metrics={metrics} runId={run.run_id} />
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

/**
 * The caveats that are about the run as a whole rather than about one number,
 * and that this run's own data can confirm. Everything here is conditioned on
 * a wire value; none of it is standing boilerplate.
 */
function StandingLimits({
  metrics,
  runId,
}: {
  metrics: Metrics;
  runId: string;
}) {
  /**
   * THE SUMMARY AND THE ANALYST TAB MUST NOT DISAGREE ABOUT ONE RUN.
   *
   * This section used to assert "No model was called on this run" whenever
   * `llm_tokens_per_100` and `llm_cost_usd_per_100` were both zero — the exact
   * inference `lib/analyst.ts` documents at length as invalid. A run with a
   * live Gemini credential whose four attempts all returned 503 completes on
   * its deterministic result and bills nothing, and this page called that "the
   * absence of a call". It was an unconditional false statement about what the
   * system did, on the page a reviewer reads first, while the analyst tab on
   * the same run hedged correctly.
   *
   * So the state comes from the one discriminator now. Passing `null` for the
   * census is deliberate and is what keeps this page cheap: scanning the
   * exception list is the analyst tab's job, so the three positive states are
   * still decidable here from `Metrics` alone (an accepted hypothesis, a
   * non-zero rejection rate, or a billed call), and everything else lands on
   * `undetermined` — which is not "no model ran", it is "this page cannot
   * tell", and that is what it now says.
   */
  const verdict = analystVerdict(metrics, null);
  const cannotTell = verdict.state === "undetermined";

  return (
    <section aria-labelledby="limits-heading" className="space-y-6">
      <div className="space-y-2">
        <h2 id="limits-heading" className="text-base font-medium tracking-tight">
          What this run cannot tell you
        </h2>
        <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
          Limits of the run as a whole rather than of one number. Each is
          conditioned on what this run actually reports.
        </p>
      </div>

      <ul className="max-w-[72ch] space-y-4">
        <Limit title="The gap to 100% is one defect class, and it is not a long tail">
          Every unmatched resolvable subject is a split settlement: one
          settlement paid across two bank lines, which closes neither line on
          its own. Both narrations name the settlement, so the reference lookup
          hits and then declines — a settlement id proves identity, never
          arithmetic. Solving it needs a new tier that searches subsets of bank
          lines, and a subset search is exactly where a tie-breaker on an
          ambiguous set creeps back in. It was left unimplemented on purpose.
        </Limit>

        {cannotTell ? (
          <Limit title="This page cannot tell you whether a model was called">
            <code className="font-mono">llm_tokens_per_100</code> and{" "}
            <code className="font-mono">llm_cost_usd_per_100</code> are both
            zero, and zero is not proof of absence: a run whose analyst was
            switched off, a model that was called and proposed nothing, and a
            model that was called and whose provider failed all bill exactly
            this. <code className="font-mono">CreateRunRequest.use_llm</code> is
            a request field and{" "}
            <code className="font-mono">RunSummary</code> does not echo it back,
            so this contract cannot separate them here. The{" "}
            <Link
              href={`/runs/${encodeURIComponent(runId)}/analyst`}
              className="rounded-sm underline underline-offset-4 transition-colors duration-150 hover:text-foreground focus-visible:focus-ring"
            >
              analyst view
            </Link>{" "}
            reads the exception list and the run&apos;s own status for the rest
            of the evidence. Until then, read every LLM figure on this page as
            not established rather than as a measured zero.
          </Limit>
        ) : null}

        <Limit title="Five of the eight reason codes are not exercised by this generator">
          They are implemented and unit-tested, and this data does not produce
          them. A zero beside one of those codes on the exception list means
          not exercised here, not handled.
        </Limit>

        <Limit title="The scorecard is demonstrably blind to at least one real bug">
          A cardinality-filtered candidate pool — the precise defect the
          ambiguity trap exists to catch — leaves every metric at every scale
          byte-identical. The unit tests catch it; nothing on this page does.
        </Limit>
      </ul>

      <p className="text-xs text-muted-foreground">
        <Link
          href="/method"
          className="rounded-sm underline underline-offset-4 transition-colors duration-150 hover:text-foreground focus-visible:focus-ring"
        >
          The method page
        </Link>{" "}
        carries the pipeline, the tier ladder, the verifier&apos;s six checks and
        the full list of known limitations.
      </p>
    </section>
  );
}

/**
 * One standing limit: the claim visible, the argument folded.
 *
 * These five are the most valuable prose on the page and they were also the
 * heaviest — around 250 words in five stacked paragraphs, at the bottom of a
 * page a reviewer had already read 400 words of. Every title still reads as a
 * complete admission on its own, so the list scans as five limitations rather
 * than as an essay, and nothing is removed: the body is one click away and
 * stays in the DOM for ctrl-F and for print.
 */
function Limit({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <li className="border-l border-border pl-4">
      <details className="group">
        <summary className="cursor-pointer list-none text-xs font-medium underline decoration-dotted decoration-border underline-offset-4 focus-visible:focus-ring [&::-webkit-details-marker]:hidden">
          {title}
        </summary>
        <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
          {children}
        </p>
      </details>
    </li>
  );
}
