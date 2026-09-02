"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ArrowLeftIcon, FileTextIcon } from "lucide-react";
import { api, ApiError, API_BASE } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { formatTimestamp, fullTimestamp } from "@/lib/datetime";
import { isFromUploads, isTerminal } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { RunSummary } from "@/lib/types";
import { ErrorState, LoadingBlock } from "@/components/States";
import { RunProgress } from "@/components/RunProgress";
import { StatePill } from "@/components/StatePill";

const count = (n: number) => n.toLocaleString("en-IN");

/**
 * Every view of a run reads the same RunSummary.
 *
 * It is fetched once here, in the nested layout, and handed down. Six sibling
 * routes each polling `GET /api/runs/{id}` at 500 ms would be six times the
 * traffic for one answer, and — worse — six answers that can disagree with
 * each other for half a second while a run finishes.
 */
const RunContext = React.createContext<RunSummary | null>(null);

export function useRun(): RunSummary {
  const run = React.useContext(RunContext);
  if (!run) {
    throw new Error("useRun must be called inside the /runs/[id] layout");
  }
  return run;
}

/**
 * The seven views of a run, as real routes.
 *
 * Deliberately not tab state: a reviewer needs to send someone a link to the
 * exception list of a specific run, and a URL that says /runs/x/exceptions
 * does that where a URL that says /runs/x plus a remembered click does not.
 *
 * Drift is last on purpose. The first six are this run; the seventh is this run
 * set against another one, and it is the only view whose URL carries a second
 * run id.
 */
const VIEWS = [
  { segment: null, slug: "", label: "Summary" },
  { segment: "tiers", slug: "/tiers", label: "Tiers" },
  { segment: "exceptions", slug: "/exceptions", label: "Exceptions" },
  { segment: "settlements", slug: "/settlements", label: "Settlements" },
  { segment: "records", slug: "/records", label: "Records" },
  { segment: "analyst", slug: "/analyst", label: "Analyst" },
  { segment: "drift", slug: "/drift", label: "Drift" },
] as const;

export function RunScope({
  runId,
  children,
}: {
  runId: string;
  children: React.ReactNode;
}) {
  // 500 ms while the run is executing; stops the moment it reaches a terminal
  // state, so a finished run is never polled.
  const { data: run, error, loading, refresh } = usePoll<RunSummary>(
    `run:${runId}`,
    (signal) => api.getRun(runId, { signal }),
    { intervalMs: 500, shouldContinue: (s) => !isTerminal(s.state) },
  );

  if (error) {
    // A 404 IS A DIFFERENT FACT FROM A FAILED REQUEST, AND THE COPY BRANCHES.
    // The recovery text used to say "this is a failed request, not a missing
    // run" unconditionally -- correct for the network-failure case below it,
    // and the exact opposite of the API's own line for a 404, which was
    // printed two lines further down saying `no run with id '...'`. A "Try
    // again" button on a definitive 404 is a loop, so it is not offered.
    const missing = error instanceof ApiError && error.status === 404;
    return (
      <div className="mx-auto w-full max-w-[92rem] space-y-6 px-6 py-10 lg:px-8">
        <BackLink />
        <ErrorState
          title={
            missing ? `There is no run ${runId}` : `Run ${runId} did not load`
          }
          error={error}
          recovery={
            missing ? (
              <>
                The API answered definitively: no run with this id exists. That
                is not a transient failure and retrying cannot change it — a
                mistyped id, or a run from a database that has since been
                replaced. The run history has every id this API knows about.
              </>
            ) : (
              <>
                The run may still exist — this is a failed request, not a
                missing run. Retry below; if it keeps failing, check that the
                API is reachable and that this id appears in the run history.
              </>
            )
          }
          onRetry={missing ? undefined : refresh}
        />
      </div>
    );
  }

  if (loading || !run) {
    return (
      <div className="mx-auto w-full max-w-[92rem] space-y-8 px-6 py-10 lg:px-8">
        <BackLink />
        <LoadingBlock label={`Loading run ${runId}`} lines={2} className="max-w-md" />
      </div>
    );
  }

  return (
    <RunContext.Provider value={run}>
      <RunHeader run={run} />
      <div className="mx-auto w-full max-w-[92rem] px-6 py-10 lg:px-8">
        {children}
      </div>
    </RunContext.Provider>
  );
}

/**
 * The run as a document.
 *
 * This page shows the answer; the report defends it — the derivations, the
 * denominators, what each rung requires, and what the run cannot tell you. It
 * is a plain anchor rather than a fetch-and-blob because the browser already
 * knows how to open a PDF, and a download built in JavaScript is one more
 * thing that can fail between a reviewer and the evidence.
 *
 * Only on a terminal run: a report of a run still executing would describe
 * numbers that have not been measured.
 */
function ReportLink({ runId }: { runId: string }) {
  return (
    <a
      href={`${API_BASE}/api/runs/${encodeURIComponent(runId)}/report.pdf`}
      target="_blank"
      rel="noopener noreferrer"
      className="brut-press inline-flex h-8 items-center gap-1.5 border-[length:var(--border-w)] border-[var(--ink)] bg-primary px-2.5 text-xs font-semibold text-primary-foreground focus-visible:focus-ring"
    >
      <FileTextIcon aria-hidden className="size-3.5" strokeWidth={2.25} />
      Report
    </a>
  );
}


function RunHeader({ run }: { run: RunSummary }) {
  const terminal = isTerminal(run.state);
  return (
    <div className="border-b-[length:var(--border-w)] border-[var(--ink)] bg-surface">
      <div className="mx-auto w-full max-w-[92rem] px-6 lg:px-8">
        <div className="flex flex-wrap items-start justify-between gap-x-8 gap-y-4 pt-6 pb-5">
          <div className="min-w-0 space-y-3">
            <BackLink />
            <h1 className="font-mono text-lg font-medium tracking-tight">
              {run.run_id}
            </h1>
            <dl className="flex flex-wrap items-baseline gap-x-6 gap-y-1 text-xs text-muted-foreground">
              <Meta
                label="Created"
                value={formatTimestamp(run.created_at)}
                title={fullTimestamp(run.created_at)}
              />
              {/* No seed means no seed. A run over uploaded files reports
                  -1 because RunSummary.seed cannot be null, and this header
                  says what that means rather than printing the sentinel. */}
              <Meta
                label={isFromUploads(run) ? "Source" : "Seed"}
                value={isFromUploads(run) ? "Uploaded files" : count(run.seed)}
                title={
                  isFromUploads(run)
                    ? "This run reconciled files uploaded to /uploads, so it has no seed and no ground truth to score against."
                    : undefined
                }
              />
              <Meta label="Records" value={count(run.record_count)} />
              {/* `match_count` and `exception_count` ARE non-nullable on the
                  contract and DO read 0 on a running run -- so printing them
                  is faithful to the wire and still wrong. The body of the
                  summary page, eight lines below this header, promises not to
                  show a measured zero for a number that has not been measured;
                  a header that prints `Matches 0` for the whole execution
                  commits exactly that, in the same viewport, during the part
                  of a demo where a progress bar is the only other thing to
                  read. They are counted when the run reaches a terminal state,
                  so until then this shows an absence. */}
              <Meta
                label="Matches"
                value={terminal ? count(run.match_count) : null}
              />
              <Meta
                label="Exceptions"
                value={terminal ? count(run.exception_count) : null}
              />
            </dl>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            {terminal ? (
              <StatePill state={run.state} />
            ) : (
              <RunProgress runId={run.run_id} state={run.state} />
            )}
            {terminal ? <ReportLink runId={run.run_id} /> : null}
          </div>
        </div>
        <RunNav runId={run.run_id} />
      </div>
    </div>
  );
}

function RunNav({ runId }: { runId: string }) {
  const pathname = usePathname();
  const base = `/runs/${encodeURIComponent(runId)}`;

  return (
    <nav aria-label="Run views" className="-mb-px flex gap-1 overflow-x-auto">
      {VIEWS.map((view) => {
        const href = `${base}${view.slug}`;
        const current = pathname === href;
        return (
          <Link
            key={view.label}
            href={href}
            aria-current={current ? "page" : undefined}
            className={cn(
              "relative whitespace-nowrap rounded-t-md px-3 py-2.5 text-xs transition-colors duration-150",
              "after:absolute after:inset-x-0 after:bottom-0 after:h-px after:transition-colors after:duration-150",
              "focus-visible:focus-ring focus-visible:z-10",
              current
                ? "text-foreground after:bg-brand"
                : "text-muted-foreground after:bg-transparent hover:bg-surface-hover hover:text-foreground active:bg-surface-active",
            )}
          >
            {view.label}
          </Link>
        );
      })}
    </nav>
  );
}

function BackLink() {
  return (
    <Link
      href="/"
      className="inline-flex items-center gap-1.5 rounded-sm text-xs text-muted-foreground underline-offset-4 transition-colors duration-150 hover:text-foreground hover:underline focus-visible:focus-ring"
    >
      <ArrowLeftIcon aria-hidden className="size-3" strokeWidth={2} />
      All runs
    </Link>
  );
}

/**
 * One identity fact in the run header. A null `value` is a quantity this run
 * has not produced yet -- rendered as an em dash with the reason on it, never
 * as a zero.
 */
function Meta({
  label,
  value,
  title,
}: {
  label: string;
  value: string | null;
  title?: string;
}) {
  return (
    <span className="inline-flex items-baseline gap-1.5" title={title}>
      <dt>{label}</dt>
      {value === null ? (
        <dd className="text-muted-foreground" title="Counted when the run finishes">
          <span aria-hidden>&mdash;</span>
          <span className="sr-only">not counted until this run finishes</span>
        </dd>
      ) : (
        <dd className="tnum text-foreground">{value}</dd>
      )}
    </span>
  );
}
