"use client";

import { api } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { isTerminal } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { RunState, RunStatus } from "@/lib/types";
import { StatePill } from "@/components/StatePill";

/**
 * Live progress for a run that has not finished.
 *
 * Polls `GET /api/runs/{id}/status` at 500 ms and stops on `completed` or
 * `failed`. Mounted only for non-terminal runs, so a finished run is never
 * polled at all.
 */
export function RunProgress({
  runId,
  state,
  className,
  showStage = true,
}: {
  runId: string;
  state: RunState;
  className?: string;
  showStage?: boolean;
}) {
  const { data } = usePoll<RunStatus>(
    `status:${runId}`,
    (signal) => api.getRunStatus(runId, { signal }),
    {
      intervalMs: 500,
      enabled: !isTerminal(state),
      shouldContinue: (status) => !isTerminal(status.state),
    },
  );

  const current = data?.state ?? state;
  const progress = data?.progress ?? 0;

  return (
    <div className={cn("flex min-w-44 flex-col gap-1.5", className)}>
      <StatePill state={current} />
      {!isTerminal(current) ? (
        <>
          <div
            className="h-1 w-full overflow-hidden rounded-full bg-muted"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress * 100)}
          >
            <div
              className="h-full rounded-full bg-brand transition-[width] duration-300 ease-out"
              style={{ width: `${Math.round(progress * 100)}%` }}
            />
          </div>
          {showStage ? (
            <p className="tnum text-2xs text-muted-foreground">
              {Math.round(progress * 100)}% · {data?.stage ?? "queued"}
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
