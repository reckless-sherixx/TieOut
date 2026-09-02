"use client";

import * as React from "react";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  ChevronsLeftIcon,
  ChevronsRightIcon,
} from "lucide-react";
import { api } from "@/lib/api";
import { useResource } from "@/lib/hooks";
import { formatINR } from "@/lib/money";
import {
  isTerminal,
  REASON_CODE_DESCRIPTION,
  REASON_CODE_LABEL,
  SUBJECT_TYPE_LABEL,
  UNRESOLVABLE_BY_DESIGN,
  VERDICT_LABEL,
} from "@/lib/labels";
import { cn } from "@/lib/utils";
import type {
  PaginatedReconExceptions,
  ReasonCode,
  ReconExceptionDetail,
  RunState,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState } from "@/components/States";

export const PAGE_SIZE = 50;

const int = (n: number) => n.toLocaleString("en-IN");

/**
 * The exception list. Server-paginated, server-filtered, and stable at 5,000
 * rows because the browser never holds more than one page of them.
 *
 * Pagination and filtering are both request parameters, never a client-side
 * slice of a list fetched whole: the difference is invisible at fifty rows and
 * is the entire difference at five thousand. The API guarantees a stable
 * `ORDER BY exception_id`, so no row can appear on two pages — and there is
 * deliberately no client-side dedup here, because dedup would paper over an
 * ordering bug instead of surfacing it.
 *
 * WHILE A NEW PAGE LOADS, THE OLD ONE STAYS ON SCREEN. A table that empties
 * itself between pages moves everything below it twice per click and gives a
 * reader nothing to read in the meantime. The rows recede, the controls
 * disable, and `aria-busy` says what is happening.
 */
export function ExceptionTable({
  runId,
  runState,
  reasonCode,
  page,
  openId,
  onPageChange,
  onClearFilter,
  onOpen,
}: {
  runId: string;
  /**
   * Part of the fetch key, not decoration: a run that finishes goes from
   * reporting zero exceptions to reporting all of them, and without the state
   * in the key this list would keep showing the empty set it read while the run
   * was still executing.
   */
  runState: RunState;
  reasonCode: ReasonCode | null;
  page: number;
  openId: string | null;
  onPageChange: (next: number) => void;
  onClearFilter: () => void;
  onOpen: (row: ReconExceptionDetail) => void;
}) {
  const { data, error, loading, refresh } = useResource<PaginatedReconExceptions>(
    `exceptions:${runId}:${runState}:${reasonCode ?? "all"}:${page}`,
    (signal) =>
      api.listRunExceptions(
        runId,
        { reason_code: reasonCode, page, size: PAGE_SIZE },
        { signal },
      ),
  );

  // The last page that actually arrived, so a page change swaps content rather
  // than collapsing the table and reflowing everything under it.
  //
  // Adjusted during render rather than in an effect: the value is derived from
  // props and the previous result, so an effect would render once with the
  // wrong thing on screen and then again with the right one. The filter is part
  // of the group key because a filter change is a different ROW SET, not a
  // different page of the same one — holding rows across it would leave the
  // wrong reason codes visible for a beat.
  const group = `${runId}:${runState}:${reasonCode ?? "all"}`;
  const [held, setHeld] = React.useState<{
    group: string;
    page: PaginatedReconExceptions;
  } | null>(null);
  if (data) {
    if (held?.page !== data) setHeld({ group, page: data });
  } else if (held !== null && held.group !== group) {
    setHeld(null);
  }
  const heldPage = held?.group === group ? held.page : null;

  const shown = data ?? heldPage;
  const rows = shown?.items ?? [];
  const total = shown?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const from = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const to = Math.min(page * PAGE_SIZE, total);
  const stale = loading && data === null && heldPage !== null;

  if (error) {
    return (
      <ErrorState
        title="The exception list did not load"
        error={error}
        recovery={
          <>
            <code className="font-mono">
              GET /api/runs/{runId}/exceptions?page={page}
            </code>{" "}
            failed. Retry below. If it keeps failing, the run summary above
            loaded from a different endpoint, so this is the exception endpoint
            rather than the run being gone.
          </>
        }
        onRetry={refresh}
      />
    );
  }

  if (loading && heldPage === null) {
    return <TableSkeleton />;
  }

  // PAGED PAST THE END IS NOT AN EMPTY RUN.
  //
  // `?page=99` on a run with 40 exceptions returns zero items and a `total` of
  // 40, and reading only `items.length` turned that into "This run raised no
  // exceptions" -- printed directly beneath the census panel on the same
  // screen, which was simultaneously reporting all forty. The page contradicted
  // itself and the confident half was the wrong one. `total` is the fact that
  // separates the two, and it is on every response.
  const pastTheEnd = rows.length === 0 && total > 0 && page > pageCount;

  if (pastTheEnd) {
    return (
      <EmptyState
        title={`Page ${int(page)} is past the end of this list`}
        reason={
          <>
            There {total === 1 ? "is" : "are"}{" "}
            <span className="tnum">{int(total)}</span>{" "}
            {reasonCode ? (
              <>
                {REASON_CODE_LABEL[reasonCode]} {total === 1 ? "exception" : "exceptions"}
              </>
            ) : (
              <>{total === 1 ? "exception" : "exceptions"}</>
            )}{" "}
            in this run, which is{" "}
            <span className="tnum">{int(pageCount)}</span>{" "}
            {pageCount === 1 ? "page" : "pages"} at {PAGE_SIZE} per request. The
            URL asks for one that does not exist. Nothing is missing and nothing
            is broken — this is the end of the list, not an empty one, and the
            page number was not silently corrected because a corrected URL and
            the URL you followed would then mean different things.
          </>
        }
        action={
          <Button variant="outline" size="sm" onClick={() => onPageChange(pageCount)}>
            Go to page {int(pageCount)}
          </Button>
        }
      />
    );
  }

  if (rows.length === 0) {
    return (
      <EmptyState
        title={
          !isTerminal(runState)
            ? "This run is still executing"
            : reasonCode
              ? `No ${REASON_CODE_LABEL[reasonCode]} exceptions in this run`
              : "This run raised no exceptions"
        }
        reason={
          !isTerminal(runState) ? (
            <>
              Exceptions are itemised when the run reaches a terminal state.
              This list re-reads when it does — the summary above is polled every
              500&nbsp;ms until then.
            </>
          ) : reasonCode ? (
            <>
              {REASON_CODE_DESCRIPTION[reasonCode]} This dataset produced none of
              them, which is a property of the data rather than evidence that the
              code path works.
            </>
          ) : (
            <>
              Every subject in this run was matched. That is an unusual result
              rather than an empty page: the generator injects two defect classes
              that nothing can resolve, so a dataset carrying them should always
              leave something here.
            </>
          )
        }
        action={
          reasonCode ? (
            <Button variant="outline" size="sm" onClick={onClearFilter}>
              Show all reason codes
            </Button>
          ) : null
        }
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <p className="tnum text-xs text-muted-foreground" role="status">
          {int(from)}–{int(to)} of {int(total)}
          {reasonCode ? (
            <> matching {REASON_CODE_LABEL[reasonCode]}</>
          ) : (
            <> exceptions</>
          )}
          , {PAGE_SIZE} per request
        </p>

        <div className="flex items-center gap-1">
          <PageButton
            label="First page"
            icon={<ChevronsLeftIcon />}
            disabled={page <= 1 || stale}
            onClick={() => onPageChange(1)}
          />
          <PageButton
            label="Previous page"
            icon={<ChevronLeftIcon />}
            disabled={page <= 1 || stale}
            onClick={() => onPageChange(Math.max(1, page - 1))}
          />
          <span className="tnum px-2 text-xs text-muted-foreground">
            Page {int(page)} of {int(pageCount)}
          </span>
          <PageButton
            label="Next page"
            icon={<ChevronRightIcon />}
            disabled={page >= pageCount || stale}
            onClick={() => onPageChange(Math.min(pageCount, page + 1))}
          />
          <PageButton
            label="Last page"
            icon={<ChevronsRightIcon />}
            disabled={page >= pageCount || stale}
            onClick={() => onPageChange(pageCount)}
          />
        </div>
      </div>

      <div
        aria-busy={stale}
        className={cn(
          // `relative` is load-bearing, not cosmetic. The sr-only spans below
          // are absolutely positioned; without a positioned ancestor they
          // resolve against the document and drag it to the table's full
          // width -- measured 837px inside a 375px viewport. Removing this
          // reintroduces horizontal page scroll on mobile.
          "relative overflow-x-auto rounded-xl border border-border transition-opacity duration-150",
          stale && "opacity-60",
        )}
      >
        {stale ? (
          <span className="sr-only" role="status">
            Loading page {page}
          </span>
        ) : null}
        <table className="w-full min-w-[52rem] border-collapse text-left">
          <caption className="sr-only">
            Exceptions {from} to {to} of {total}
            {reasonCode ? `, filtered to ${REASON_CODE_LABEL[reasonCode]}` : ""}
          </caption>
          <thead>
            <tr className="border-b border-border bg-surface">
              <Th className="w-40 pl-4">Subject</Th>
              <Th className="w-28">Type</Th>
              <Th className="w-56">Reason</Th>
              <Th className="w-32 text-right">Amount</Th>
              <Th>Verifier</Th>
              <Th className="w-20 pr-4 text-right">Audit</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const open = row.exception_id === openId;
              return (
                <tr
                  key={row.exception_id}
                  onClick={() => onOpen(row)}
                  className={cn(
                    "cursor-pointer border-b border-border transition-colors duration-150 last:border-b-0",
                    open ? "bg-surface-selected" : "hover:bg-surface-hover",
                  )}
                >
                  <td className="py-2.5 pr-4 pl-4 font-mono text-xs">
                    {row.subject_id}
                  </td>
                  <td className="py-2.5 pr-4 text-xs text-muted-foreground">
                    {SUBJECT_TYPE_LABEL[row.subject_type]}
                  </td>
                  <td className="py-2.5 pr-4 text-xs">
                    <span
                      className={cn(
                        // Unresolvable by construction is an OUTCOME, not an
                        // error and not an emphasis: it takes the exception
                        // colour, never the accent and never destructive.
                        UNRESOLVABLE_BY_DESIGN.has(row.reason_code) &&
                          "text-excepted-fg",
                      )}
                    >
                      {REASON_CODE_LABEL[row.reason_code]}
                    </span>
                  </td>
                  <td className="money py-2.5 pr-4 text-right text-xs">
                    {formatINR(row.amount)}
                  </td>
                  <td className="py-2.5 pr-4 text-xs">
                    <VerdictCell row={row} />
                  </td>
                  <td className="py-2.5 pr-4 text-right">
                    {/* The row is clickable for a mouse, but a clickable <tr>
                        cannot be reached from a keyboard. THIS is the control:
                        it is in the tab order, it names its action, and it
                        shows the shared focus ring. */}
                    <button
                      type="button"
                      aria-haspopup="dialog"
                      aria-expanded={open}
                      onClick={(event) => {
                        event.stopPropagation();
                        onOpen(row);
                      }}
                      className="rounded-sm px-1 text-xs text-muted-foreground underline-offset-4 transition-colors duration-150 hover:text-foreground hover:underline active:text-foreground focus-visible:focus-ring"
                    >
                      Open
                      <span className="sr-only">
                        {" "}
                        the audit trail for {row.subject_id}
                      </span>
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/**
 * The verifier's verdict in the list, carrying `failed_check` with it.
 *
 * The strongest fact in this dataset is which check refused a hypothesis, and
 * it is visible here without opening anything — and never filtered out.
 */
function VerdictCell({ row }: { row: ReconExceptionDetail }) {
  if (row.verifier_verdict === "not_attempted") {
    return (
      <span className="text-muted-foreground">
        Not attempted
        <span className="sr-only"> — no hypothesis was made for this subject</span>
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={cn(
          "inline-flex items-center rounded-full border px-1.5 py-px text-2xs font-medium",
          row.verifier_verdict === "rejected"
            ? "border-rejected/40 bg-rejected/10 text-rejected-fg"
            : "border-border text-muted-foreground",
        )}
      >
        {VERDICT_LABEL[row.verifier_verdict]}
      </span>
      {row.failed_check !== null ? (
        <span className="font-mono text-2xs text-muted-foreground">
          {row.failed_check}
        </span>
      ) : null}
    </span>
  );
}

/**
 * The first load. A skeleton and not a spinner, because the shape of what is
 * coming is already known: fifty rows of six columns. A spinner would say only
 * that something is happening.
 */
function TableSkeleton() {
  return (
    <div
      aria-busy="true"
      // `relative` for the same reason as the table wrapper above: the status
      // span below is `sr-only`, which is absolutely positioned, and an
      // absolutely positioned box whose containing block is outside this
      // element is not clipped by its overflow. Harmless here only because
      // that span sits at the left edge — which is luck, not a design, and
      // the explorer's equivalent skeleton is already `relative`.
      className="relative overflow-hidden rounded-xl border border-border"
    >
      <span className="sr-only" role="status">
        Loading the exception list
      </span>
      <div className="border-b border-border bg-surface px-4 py-2.5">
        <span aria-hidden className="skeleton block h-2.5 w-24" />
      </div>
      {Array.from({ length: 8 }, (_, i) => (
        <div
          key={i}
          className="grid grid-cols-[10rem_7rem_14rem_8rem_1fr] items-center gap-4 border-b border-border px-4 py-3 last:border-b-0"
        >
          <span aria-hidden className="skeleton block h-3 w-24" />
          <span aria-hidden className="skeleton block h-3 w-16" />
          <span aria-hidden className="skeleton block h-3 w-32" />
          <span aria-hidden className="skeleton block h-3 w-20 justify-self-end" />
          <span aria-hidden className="skeleton block h-3 w-28" />
        </div>
      ))}
    </div>
  );
}

function PageButton({
  label,
  icon,
  disabled,
  onClick,
}: {
  label: string;
  icon: React.ReactNode;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <Button
      variant="outline"
      size="icon-sm"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
    >
      {icon}
    </Button>
  );
}

function Th({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <th
      scope="col"
      className={cn(
        "py-2.5 pr-4 text-2xs font-medium text-muted-foreground",
        className,
      )}
    >
      {children}
    </th>
  );
}
