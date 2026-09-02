"use client";

import Link from "next/link";
import { usePoll } from "@/lib/hooks";
import { api, API_BASE } from "@/lib/api";
import { formatRate } from "@/lib/money";
import { formatTimestamp, fullTimestamp } from "@/lib/datetime";
import { isFromUploads, isTerminal } from "@/lib/labels";
import type { RunSummary } from "@/lib/types";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { StatePill } from "@/components/StatePill";
import { RunProgress } from "@/components/RunProgress";
import { ErrorState } from "@/components/States";

const number = (n: number) => n.toLocaleString("en-IN");

/**
 * Run history.
 *
 * Polls `GET /api/runs` at 500 ms while any run is still executing and stops
 * the moment every run reaches a terminal state. A finished run is never
 * polled again.
 */
export function RunTable() {
  const { data, error, loading, refresh } = usePoll<RunSummary[]>(
    "runs",
    (signal) => api.listRuns({ signal }),
    {
      intervalMs: 500,
      shouldContinue: (runs) => runs.some((r) => !isTerminal(r.state)),
    },
  );

  if (error) {
    // A FAILURE THAT CAN RECOVER MUST OFFER THE RECOVERY.
    //
    // This used to be a line of red text with no control, so recovering the
    // run history -- the entry point to the whole console -- meant reloading
    // the page, while the run-scope error state one click away recovered
    // cleanly from the same outage with one button. Polling stops on error by
    // design rather than hammering a failing endpoint every 500 ms, which is
    // exactly why the retry has to be a control the reader can reach.
    return (
      <div className="px-6 py-6">
        <ErrorState
          title="The run history did not load"
          error={error}
          recovery={
            <>
              <code className="font-mono">GET {API_BASE}/api/runs</code> did not
              answer. Polling stops on a failure rather than retrying every
              500&nbsp;ms against an API that is down, so nothing is happening
              until you ask it to. If the message below says the API could not
              be reached, start it and try again — the page does not need a
              reload.
            </>
          }
          onRetry={refresh}
        />
      </div>
    );
  }

  if (loading && !data) {
    return (
      <p className="px-6 py-8 text-sm text-muted-foreground">Loading runs…</p>
    );
  }

  const runs = data ?? [];
  if (runs.length === 0) {
    return (
      <p className="px-6 py-8 text-sm text-muted-foreground">
        No runs yet. Generate a dataset to start one.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead className="pl-6">Run</TableHead>
            <TableHead>Created</TableHead>
            <TableHead className="text-right">Seed</TableHead>
            <TableHead className="text-right">Records</TableHead>
            <TableHead className="text-right">Auto-match</TableHead>
            <TableHead className="text-right">False match</TableHead>
            <TableHead className="text-right">Exceptions</TableHead>
            <TableHead className="pr-6">State</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {runs.map((run) => {
            const m = run.metrics;
            return (
              <TableRow key={run.run_id} className="group">
                <TableCell className="pl-6">
                  <Link
                    href={`/runs/${run.run_id}`}
                    className="font-mono text-xs underline-offset-4 group-hover:underline"
                  >
                    {run.run_id}
                  </Link>
                </TableCell>
                {/* `created_at` is stamped by the API and is present on every
                    run state, including pending and running — so this column
                    never falls back to a client clock and never renders a
                    dash. */}
                <TableCell
                  className="tnum whitespace-nowrap text-xs text-muted-foreground"
                  title={fullTimestamp(run.created_at)}
                >
                  {formatTimestamp(run.created_at)}
                </TableCell>
                {/* A run over uploaded files has no seed -- nothing generated
                    its records -- and the API says so with a value no caller
                    could have supplied. Printing "-1" would be faithful to the
                    wire and meaningless on the screen; printing "0" would be
                    worse, because 0 is a seed somebody could have chosen. */}
                <TableCell className="tnum text-right text-xs text-muted-foreground">
                  {isFromUploads(run) ? (
                    <span title="No seed: this run reconciled uploaded files">
                      <span aria-hidden>&mdash;</span>
                      <span className="sr-only">
                        no seed — this run reconciled uploaded files
                      </span>
                    </span>
                  ) : (
                    number(run.seed)
                  )}
                </TableCell>
                <TableCell className="tnum text-right">
                  {number(run.record_count)}
                </TableCell>
                {/* `metrics` is null in TWO different situations and they are
                    not the same absence. A run still executing has not been
                    scored YET; a run over uploaded files will never be scored
                    at all, because there is no ground truth for a merchant's
                    own files. Both render a dash -- 0% would be a lie in
                    either -- and the dash says which. */}
                <TableCell className="tnum text-right font-medium">
                  {m ? formatRate(m.auto_match_rate) : <NoRate run={run} />}
                </TableCell>
                <TableCell className="tnum text-right font-medium">
                  {m ? formatRate(m.false_match_rate) : <NoRate run={run} />}
                </TableCell>
                {/* Counted, not scored: `exception_count` is a real number on
                    any terminal run, uploaded files included. Gating it on
                    `metrics` used to hide it for exactly the runs where it is
                    the only thing there is to report. */}
                <TableCell className="tnum text-right">
                  {isTerminal(run.state) ? (
                    number(run.exception_count)
                  ) : (
                    <NoRate run={run} />
                  )}
                </TableCell>
                <TableCell className="pr-6">
                  {isTerminal(run.state) ? (
                    <StatePill state={run.state} />
                  ) : (
                    <RunProgress runId={run.run_id} state={run.state} />
                  )}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}

function NoRate({ run }: { run: RunSummary }) {
  const uploaded = isFromUploads(run) && isTerminal(run.state);
  const reason = uploaded
    ? "No scorecard: this run reconciled uploaded files, and there is no ground truth to score them against"
    : "Not computed yet";
  return (
    <span className="text-muted-foreground" title={reason}>
      <span aria-hidden>&mdash;</span>
      <span className="sr-only">{reason}</span>
    </span>
  );
}
