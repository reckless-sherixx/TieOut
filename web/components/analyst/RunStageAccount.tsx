"use client";

import { api } from "@/lib/api";
import { useResource } from "@/lib/hooks";
import { RUN_STATE_LABEL } from "@/lib/labels";
import type { RunState, RunStatus } from "@/lib/types";
import { ErrorState, Skeleton } from "@/components/States";

/**
 * THE RUN'S OWN ACCOUNT OF WHAT HAPPENED TO THE ANALYST, VERBATIM.
 *
 * `lib/analyst.ts` decides which of five states a run is in, and it decides it
 * from typed fields only — `tier_counts.LLM`, `llm_rejection_rate`, the token
 * and cost figures, and hypotheses counted by reading rows. That rule is right
 * and it is not relaxed here: nothing on this page parses the string below, and
 * no verdict is computed from it.
 *
 * But three genuinely different runs land in the same state under those fields:
 *
 *   use_llm: false                     -> nothing was asked of a model
 *   use_llm: true, no credential set   -> a misconfigured deployment
 *   use_llm: true, provider returned   -> a model WAS called and failed; the
 *     503 on all four attempts            deterministic result stands and every
 *                                         LLM figure is zero
 *
 * Each of the three bills nothing, proposes nothing and accepts nothing. On
 * this contract the ONLY field that separates them is `RunStatus.stage`, which
 * is documented free text, and the live API writes the distinction into it in
 * plain English — including the names of the environment variables that are not
 * set. A view that refused to show it could not tell a broken deployment from a
 * deliberately deterministic run, and would never once mention a missing key.
 *
 * So it is shown, and it is shown the way `verifier_reason` is shown on the
 * exception list: rendered exactly as written, labelled as prose for a human,
 * and never turned into a number or a branch. Reading it is the reader's job;
 * parsing it would be this console's mistake.
 */
export function RunStageAccount({
  runId,
  runState,
}: {
  runId: string;
  runState: RunState;
}) {
  const { data, error, loading, refresh } = useResource<RunStatus>(
    `status:${runId}:${runState}`,
    (signal) => api.getRunStatus(runId, { signal }),
  );

  return (
    <section aria-labelledby="stage-heading" className="space-y-4">
      <div className="space-y-2">
        <h2 id="stage-heading" className="text-base font-medium tracking-tight">
          What the run itself says happened
        </h2>
        <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
          <code className="font-mono">RunStatus.stage</code> from{" "}
          <code className="font-mono">GET /api/runs/{runId}/status</code>. It is
          free text and it is the only field on this contract that distinguishes
          an analyst that was never switched on from one that had no credential
          and one whose provider refused the call — none of which is a count, and
          all of which report the same zeros in every typed field above. It is
          reproduced exactly as the API wrote it. Nothing on this page parses it,
          and no verdict above was computed from it.
        </p>
      </div>

      {error ? (
        <ErrorState
          title="The run status did not load"
          error={error}
          recovery={
            <>
              The verdict above is unaffected — it is computed from{" "}
              <code className="font-mono">metrics</code> and the exception list,
              which came from different endpoints. What is missing is the
              run&apos;s own sentence about how it ended, which is the half that
              would tell you whether a model was called.
            </>
          }
          onRetry={refresh}
        />
      ) : loading || data === null ? (
        <Skeleton className="h-14 max-w-[72ch] rounded-lg" />
      ) : (
        <div className="max-w-[72ch] rounded-lg border border-border bg-surface px-4 py-3.5">
          <p className="text-2xs text-muted-foreground">
            State <span className="text-foreground">{RUN_STATE_LABEL[data.state]}</span>
          </p>
          <p className="mt-1.5 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap">
            {data.stage}
          </p>
        </div>
      )}
    </section>
  );
}
