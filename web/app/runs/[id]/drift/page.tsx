"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ScaleIcon } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useResource } from "@/lib/hooks";
import { isTerminal } from "@/lib/labels";
import { splitMoves } from "@/lib/drift";
import type { DriftReport } from "@/lib/types";
import { useRun } from "@/components/shell/RunScope";
import { ViewIntro } from "@/components/shell/ViewIntro";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/States";
import { BaselinePicker } from "@/components/drift/BaselinePicker";
import { MetricMoves } from "@/components/drift/MetricMoves";
import { ReasonCodeMoves } from "@/components/drift/ReasonCodeMoves";

/**
 * WHAT CHANGED SINCE LAST TIME.
 *
 * A finance controller's question is rarely "what is the match rate" — it is
 * "what changed, and why". A match rate that falls from 98% to 91% overnight
 * because a new deduction type appeared is the finding; the 91% on its own is
 * not. Every other view of a run reports on one batch. This is the one that
 * compares.
 *
 * A REAL ROUTE, NOT A PANEL. `/runs/{id}/drift?against={id}` names one specific
 * comparison, and a reviewer needs to hand someone exactly that — the same
 * argument that made the other six views routes rather than tab state.
 *
 * THREE ANSWERS, ALL NORMAL, ALL RENDERED AS ANSWERS.
 *
 *   200 — the report. Material moves are separated from the rest by the flag
 *         the API computed, and nothing here recomputes it.
 *   404 — either the baseline id is unknown, or no `against` was given and
 *         this run has no earlier completed run on its dataset. The second is
 *         the ordinary case for the first run of a session and is not an
 *         error: it is an empty state that says pick a baseline.
 *   409 — both runs exist and the PAIR is the problem. That is the most
 *         informative answer this endpoint gives and it is rendered as prose
 *         with the API's own explanation, not as a failed request.
 *
 * The API's `detail` is reproduced verbatim in each case. It names both runs
 * and the specific state that makes the comparison meaningless, and rewriting
 * it in the client would be the console asserting a reason it did not compute.
 */
export default function DriftPage() {
  return (
    <div className="space-y-14">
      <ViewIntro
        title="Drift"
        lede="This run set against a baseline: which metrics moved, which of those moves cleared a threshold, and which reason codes appeared or changed count. Detection is deterministic and lives in the engine against named constants — the console renders the flags it is sent and applies none of its own."
      />

      {/* useSearchParams renders its subtree on the client. The boundary keeps
          the run header, the view rail and this heading out of that. */}
      <React.Suspense
        fallback={
          <LoadingBlock
            label="Reading the baseline from the URL"
            lines={3}
            className="max-w-xl"
          />
        }
      >
        <DriftView />
      </React.Suspense>
    </div>
  );
}

function DriftView() {
  const run = useRun();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  const againstParam = params.get("against");
  const against = againstParam === null || againstParam === "" ? null : againstParam;

  const setAgainst = React.useCallback(
    (next: string | null) => {
      const search = new URLSearchParams(params.toString());
      if (next === null) search.delete("against");
      else search.set("against", next);
      const query = search.toString();
      // `replace`, not `push`: the baseline is view state, and one history
      // entry per dropdown change turns the back button into an undo stack.
      // The URL still carries it, so the comparison is still shareable.
      router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [params, pathname, router],
  );

  const terminal = isTerminal(run.state);

  const { data, error, loading, refresh } = useResource<DriftReport>(
    `drift:${run.run_id}:${run.state}:${against ?? "default"}`,
    (signal) => api.getRunDrift(run.run_id, against, { signal }),
    terminal,
  );

  return (
    <div className="space-y-12">
      <BaselinePicker run={run} against={against} onChange={setAgainst} />

      {!terminal ? (
        <EmptyState
          title="This run has not finished"
          reason={
            <>
              A comparison needs a scorecard on both sides, and{" "}
              <code className="font-mono">RunSummary.metrics</code> is null
              until a run reaches a terminal state. Nothing is requested until
              then — a drift report against a half-finished run would be a
              comparison against numbers that are still moving. The run header
              above is polled every 500&nbsp;ms.
            </>
          }
        />
      ) : loading ? (
        <LoadingBlock
          label="Comparing this run against its baseline"
          lines={4}
          className="max-w-xl"
        />
      ) : error ? (
        <DriftFailure
          runId={run.run_id}
          against={against}
          error={error}
          onRetry={refresh}
          onClearBaseline={() => setAgainst(null)}
        />
      ) : data === null ? null : (
        <Report report={data} />
      )}
    </div>
  );
}

/**
 * The two refusals, and the failure that is actually a failure.
 *
 * A 409 IS NOT AN ERROR TOAST. Both runs exist, both are readable, and the API
 * has computed something worth knowing: that comparing them would produce
 * numbers with no meaning. It gets a heading that says so and the API's own
 * sentence explaining which property of the pair is the problem. Wrapping it in
 * "the request failed, try again" would be telling the reader to retry
 * something that cannot succeed.
 *
 * A 404 with no `against` is the ordinary state of the first run on a dataset.
 * It is an empty state with a way forward, not a failure.
 */
function DriftFailure({
  runId,
  against,
  error,
  onRetry,
  onClearBaseline,
}: {
  runId: string;
  against: string | null;
  error: Error;
  onRetry: () => void;
  onClearBaseline: () => void;
}) {
  const status = error instanceof ApiError ? error.status : null;
  const detail = error instanceof ApiError ? error.detail : null;

  if (status === 409) {
    return (
      <section
        aria-labelledby="not-comparable-heading"
        className="max-w-[72ch] space-y-4 rounded-xl border border-border bg-surface px-6 py-5"
      >
        <div className="flex items-start gap-3">
          <ScaleIcon
            aria-hidden
            className="mt-0.5 size-4 shrink-0 text-excepted"
            strokeWidth={2}
          />
          <div className="space-y-2">
            <h3
              id="not-comparable-heading"
              className="text-sm font-medium tracking-tight"
            >
              These two runs are not comparable, and here is why
            </h3>
            <p className="text-xs leading-relaxed text-foreground">
              {detail ??
                "The API refused the pair without a reason it could put into words, which is itself worth reporting."}
            </p>
          </div>
        </div>

        <p className="text-xs leading-relaxed text-muted-foreground">
          Both runs exist and both are readable on their own pages. What is
          wrong is the pair. Every rate in{" "}
          <code className="font-mono">Metrics</code> is computed over a
          denominator drawn from the run&apos;s own subjects and every{" "}
          <code className="font-mono">itc_*_paise</code> figure is a sum over
          its own settlements, so two runs of different sizes share no scale on
          either — a delta between them would be a number with no meaning, and
          the engine refuses rather than producing one. A run that completed
          without a scorecard is refused for the same class of reason.
        </p>

        <p className="text-xs leading-relaxed text-muted-foreground">
          Pick a baseline of the same size above, or{" "}
          <button
            type="button"
            onClick={onClearBaseline}
            className="rounded-sm underline underline-offset-4 transition-colors duration-150 hover:text-foreground focus-visible:focus-ring"
          >
            let the API choose the previous completed run on this dataset
          </button>
          . Retrying this pair will produce the same answer, so there is no
          retry offered for it.
        </p>
      </section>
    );
  }

  if (status === 404) {
    return (
      <EmptyState
        title={
          against === null
            ? "There is no earlier run on this dataset to compare with"
            : `No run with id ${against}`
        }
        reason={
          against === null ? (
            <>
              With no baseline named, the API compares against the immediately
              previous <em>completed</em> run on the same dataset — and this run
              is the first. That is the ordinary state of a freshly generated
              dataset rather than a failure: start a second run on it, or choose
              any other run above and the comparison will be made against that
              one instead. The API&apos;s own words:{" "}
              <span className="font-mono text-2xs break-words">
                {detail ?? "no earlier completed run on this dataset"}
              </span>
            </>
          ) : (
            <>
              The baseline named in the URL does not exist. Choose a run from
              the list above — every id there came from{" "}
              <code className="font-mono">GET /api/runs</code>, so any of them
              can be compared against.{" "}
              <span className="font-mono text-2xs break-words">
                {detail ?? ""}
              </span>
            </>
          )
        }
      />
    );
  }

  return (
    <ErrorState
      title="The drift report did not load"
      error={error}
      recovery={
        <>
          <code className="font-mono">GET /api/runs/{runId}/drift</code> failed
          before the API could answer. This is a failed request rather than a
          refused comparison — the two are different, and a refusal would say so
          in words. Retry below; the run&apos;s own pages loaded from a
          different endpoint and are unaffected.
        </>
      }
      onRetry={onRetry}
    />
  );
}

function Report({ report }: { report: DriftReport }) {
  const { material } = splitMoves(report.moves);
  const appeared = report.reason_code_moves.filter((m) => m.appeared);

  return (
    <div className="space-y-14">
      <section aria-labelledby="drift-summary-heading" className="space-y-3">
        <h2
          id="drift-summary-heading"
          className="text-base font-medium tracking-tight"
        >
          {material.length === 0 && report.reason_code_moves.length === 0
            ? "Nothing moved materially between these two runs"
            : "What moved"}
        </h2>
        <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
          Comparing{" "}
          <Link
            href={`/runs/${encodeURIComponent(report.current_run_id)}`}
            className="rounded-sm font-mono underline underline-offset-4 transition-colors duration-150 hover:text-foreground focus-visible:focus-ring"
          >
            {report.current_run_id}
          </Link>{" "}
          against{" "}
          <Link
            href={`/runs/${encodeURIComponent(report.baseline_run_id)}`}
            className="rounded-sm font-mono underline underline-offset-4 transition-colors duration-150 hover:text-foreground focus-visible:focus-ring"
          >
            {report.baseline_run_id}
          </Link>
          . {material.length === 0 ? "No metric" : `${material.length} metric${material.length === 1 ? "" : "s"}`}{" "}
          cleared a threshold, and{" "}
          {report.reason_code_moves.length === 0
            ? "no reason code changed count"
            : `${report.reason_code_moves.length} reason code${report.reason_code_moves.length === 1 ? "" : "s"} changed count`}
          {appeared.length > 0
            ? `, ${appeared.length} of them absent from the baseline entirely`
            : ""}
          .
        </p>
      </section>

      <section aria-labelledby="drift-moves-heading" className="space-y-6">
        <div className="space-y-2">
          <h2
            id="drift-moves-heading"
            className="text-base font-medium tracking-tight"
          >
            Every metric, before and after
          </h2>
          <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
            One row per numeric field of the contract, whether it moved or not —
            a metric that held steady is a result, and an omitted row would be a
            silence. The grouping is the API&apos;s{" "}
            <code className="font-mono">material</code> flag.
          </p>
        </div>

        <MetricMoves moves={report.moves} />
      </section>

      <section aria-labelledby="drift-codes-heading" className="space-y-6">
        <div className="space-y-2">
          <h2
            id="drift-codes-heading"
            className="text-base font-medium tracking-tight"
          >
            Reason codes that changed count
          </h2>
          <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
            The half of a drift report that names the cause rather than the
            symptom. A code absent from the baseline and present now is the
            shape of a new deduction type turning up overnight, and it is the
            example this whole comparison exists for.
          </p>
        </div>

        <ReasonCodeMoves moves={report.reason_code_moves} />
      </section>

      <Narrative narrative={report.narrative} />
    </div>
  );
}

/**
 * `narrative` is prose an LLM may write over facts it did not compute, and it
 * is null when no model ran. This endpoint runs no model, so it is always null.
 *
 * THE VIEW DOES NOT FILL THE GAP. Writing a summary sentence here and putting
 * it where the narrative goes would be exactly the thing the contract separates
 * `narrative` from `material` to prevent: language that reads as though it came
 * from the detector when it came from the renderer. So the absence is stated as
 * an absence, and every finding above stays the engine's own arithmetic.
 */
function Narrative({ narrative }: { narrative: string | null }) {
  return (
    <section aria-labelledby="drift-narrative-heading" className="space-y-3">
      <h2
        id="drift-narrative-heading"
        className="text-base font-medium tracking-tight"
      >
        {narrative === null ? "No prose was written over this" : "The narrative"}
      </h2>
      {narrative === null ? (
        <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
          <code className="font-mono">DriftReport.narrative</code> is null. The
          drift endpoint runs no model, and detection never needed one — it is a
          pure function of two <code className="font-mono">Metrics</code> objects
          and two reason-code censuses, against named thresholds. Nothing is
          written here in its place: a summary composed by this page would read
          as the detector&apos;s and would not be, which is the one confusion
          the split between{" "}
          <code className="font-mono">narrative</code> and{" "}
          <code className="font-mono">material</code> exists to prevent.
        </p>
      ) : (
        <>
          <p className="max-w-[72ch] text-sm leading-relaxed">{narrative}</p>
          <p className="max-w-[72ch] text-2xs leading-relaxed text-muted-foreground">
            Written by a model over facts it did not compute, and never an input
            to any <code className="font-mono">material</code> flag above. Read
            the table, not this, for what changed.
          </p>
        </>
      )}
    </section>
  );
}
