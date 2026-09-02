"use client";

import * as React from "react";
import { useResource } from "@/lib/hooks";
import { formatINR } from "@/lib/money";
import { isTerminal } from "@/lib/labels";
import { cn } from "@/lib/utils";
import {
  explorerApi,
  residualOf,
  type PaginatedSettlements,
  type Settlement,
} from "@/lib/explorer";
import type { RunState } from "@/lib/types";
import { EmptyState, ErrorState } from "@/components/States";
import { SettlementDetail } from "@/components/settlements/SettlementDetail";
import {
  Absent,
  PAGE_SIZE,
  PageBar,
  PastTheEnd,
  Th,
  TableSkeleton,
  isPastTheEnd,
  pageRange,
  useHeldPage,
} from "@/components/explorer/paging";

const int = (n: number) => n.toLocaleString("en-IN");

const SKELETON_COLUMNS = [
  "6rem",
  "2rem",
  "5rem",
  "4rem",
  "4rem",
  "4rem",
  "5rem",
  "4rem",
  "5rem",
];

/**
 * Every settlement of a run, matched or not.
 *
 * EVERY NUMBER IN A ROW IS THE ENGINE'S OWN. Nothing here re-sums a PSP leg to
 * produce a gross: a matched row carries the `MatchGroup`'s fields verbatim
 * and an unmatched row carries the matcher's own reconstruction over the
 * batch's ACTIVE legs. Re-deriving them in the browser would overstate gross
 * on every batch where a duplicate payment leg was suppressed before the
 * reconstruction ran — 14 rows in 166 on the reference dataset.
 *
 * THE RESIDUAL COLUMN IS THE POINT OF THE TABLE. `net` is the bank credit,
 * not the sum of the five columns to its left, and on a T3 row the two differ
 * within the matcher's tolerance. The gap is rendered rather than closed. A
 * table that balanced its own arithmetic would be claiming the batch
 * reconstructed to the credit exactly, when what actually happened is that it
 * did not and a tolerance rule accepted it anyway.
 */
export function SettlementTable({
  runId,
  runState,
  page,
  openId,
  onPageChange,
  onOpen,
  onClose,
}: {
  runId: string;
  /**
   * In the fetch key, not decoration: a run that finishes goes from having no
   * settlements to having all of them, and without the state here this list
   * would keep showing the empty set it read while the run was executing.
   */
  runState: RunState;
  page: number;
  openId: string | null;
  onPageChange: (next: number) => void;
  onOpen: (settlementId: string) => void;
  onClose: () => void;
}) {
  const { data, error, loading, refresh } = useResource<PaginatedSettlements>(
    `settlements:${runId}:${runState}:${page}`,
    (signal) =>
      explorerApi.listRunSettlements(runId, { page, size: PAGE_SIZE }, { signal }),
  );

  const { shown, stale } = useHeldPage(`${runId}:${runState}`, data, loading);

  const rows = shown?.items ?? [];
  const range = pageRange(page, shown?.total ?? 0);

  // Resolved from the page in front of the reader rather than fetched: the
  // contract has no single-settlement operation, and the row carrying every
  // number the breakdown needs is already here. A link that names a row on
  // another page therefore opens nothing, which is why the page number
  // travels in the URL beside the settlement id.
  const openRow = rows.find((row) => row.settlement_id === openId) ?? null;

  // Fifty rows, counted on every render rather than memoised: the memo would
  // cost more to keep honest than the loop costs to run.
  const onPage = { matched: 0, unmatched: 0, residuals: 0 };
  for (const row of rows) {
    if (row.matched) onPage.matched += 1;
    else onPage.unmatched += 1;
    if (residualOf(row) !== 0) onPage.residuals += 1;
  }

  if (error) {
    return (
      <ErrorState
        title="The settlement list did not load"
        error={error}
        recovery={
          <>
            <code className="font-mono">
              GET /api/runs/{runId}/settlements?page={page}
            </code>{" "}
            failed. Retry below. The run header above loaded from a different
            endpoint, so this is the settlements endpoint rather than the run
            being gone.
          </>
        }
        onRetry={refresh}
      />
    );
  }

  if (loading && shown === null) {
    return (
      <TableSkeleton
        label="Loading the settlement list"
        columns={SKELETON_COLUMNS}
      />
    );
  }

  if (isPastTheEnd(range, rows.length)) {
    return <PastTheEnd range={range} unit="settlements" onPageChange={onPageChange} />;
  }

  if (rows.length === 0) {
    return (
      <EmptyState
        title={
          runState === "failed"
            ? "This run failed before it reconstructed anything"
            : isTerminal(runState)
              ? "This run named no settlements"
              : "This run is still executing"
        }
        reason={
          runState === "failed" ? (
            <>
              A settlement is a batch the matcher reconstructed, and this run did
              not reach that stage. The rows it read are still on the Records
              view — an empty list here is the run&apos;s failure, not an empty
              input.
            </>
          ) : isTerminal(runState) ? (
            <>
              A settlement appears here as soon as one PSP leg names it, closed
              or not, so an empty list means the PSP report carried no settlement
              ids at all. That is a property of the input rather than a matching
              result — nothing was declined, because nothing was batched.
            </>
          ) : (
            <>
              Settlements are reconstructed when the run reaches a terminal
              state. This list re-reads when it does; the run header above is
              polled every 500&nbsp;ms until then.
            </>
          )
        }
      />
    );
  }

  return (
    <div className="space-y-4">
      <PageBar
        range={range}
        unit="settlements"
        busy={stale}
        onPageChange={onPageChange}
      />

      <p className="text-2xs leading-relaxed text-muted-foreground">
        On this page: <span className="tnum">{int(onPage.matched)}</span> closed
        against a bank line,{" "}
        <span className="tnum">{int(onPage.unmatched)}</span> not closed,{" "}
        <span className="tnum">{int(onPage.residuals)}</span> where the bank
        credit and the reconstruction disagree. Counted over the fifty rows in
        front of you, not over the run — the contract carries no run-level
        census of settlements, and inventing one from one page would be a guess
        wearing a number.
      </p>

      <div
        aria-busy={stale}
        className={cn(
          // `relative` is load-bearing, not decoration. The screen-reader-only
          // spans in these cells are absolutely positioned, and without a
          // positioned ancestor they resolve against the initial containing
          // block — escaping the scroll container and dragging the PAGE out to
          // the table's full width on a narrow viewport.
          "relative overflow-x-auto rounded-xl border border-border transition-opacity duration-150",
          stale && "opacity-60",
        )}
      >
        {stale ? (
          <span className="sr-only" role="status">
            Loading page {page}
          </span>
        ) : null}

        <table className="w-full min-w-[74rem] border-collapse text-left">
          <caption className="sr-only">
            Settlements {range.from} to {range.to} of {range.total}, ordered by
            settlement id. Residual is the bank credit minus the reconstructed
            net and is non-zero only where the engine matched within tolerance.
          </caption>
          <thead>
            <tr className="border-b border-border bg-surface">
              <Th className="w-44 pl-4">Settlement</Th>
              <Th className="w-20 text-right">Legs</Th>
              <Th className="w-28 text-right">Gross</Th>
              <Th className="w-28 text-right">MDR</Th>
              <Th className="w-28 text-right">GST</Th>
              <Th className="w-28 text-right">Refunds</Th>
              <Th className="w-28 text-right">Holds</Th>
              <Th className="w-32 text-right">Net (credit)</Th>
              <Th className="w-28 text-right">
                <span title="reconstruction − net, where net is the bank credit">
                  Residual
                </span>
                <span className="sr-only">
                  {" "}
                  — the reconstruction less the bank credit, in that order
                </span>
              </Th>
              <Th className="w-32">Closed at</Th>
              <Th className="w-40">Bank line</Th>
              <Th className="w-24 pr-4 text-right">Breakdown</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <Row
                key={row.settlement_id}
                row={row}
                open={row.settlement_id === openId}
                onOpen={onOpen}
              />
            ))}
          </tbody>
        </table>
      </div>

      {openRow ? (
        <SettlementDetail
          runId={runId}
          runState={runState}
          settlement={openRow}
          onClose={onClose}
        />
      ) : null}
    </div>
  );
}

function Row({
  row,
  open,
  onOpen,
}: {
  row: Settlement;
  open: boolean;
  onOpen: (settlementId: string) => void;
}) {
  const residual = residualOf(row);

  return (
    <tr
      onClick={() => onOpen(row.settlement_id)}
      className={cn(
        "cursor-pointer border-b border-border transition-colors duration-150 last:border-b-0",
        open ? "bg-surface-selected" : "hover:bg-surface-hover",
      )}
    >
      <th
        scope="row"
        className="py-2.5 pr-4 pl-4 text-left font-mono text-xs font-normal"
      >
        {row.settlement_id}
      </th>
      <td className="tnum py-2.5 pr-4 text-right text-xs">
        {row.payment_leg_count}
      </td>
      <Amount value={row.gross} />
      <Amount value={row.fees} />
      <Amount value={row.tax} />
      <Amount value={row.refunds} muted={row.refunds === 0} />
      <Amount value={row.holds} muted={row.holds === 0} />
      <td className="money py-2.5 pr-4 text-right text-xs font-medium">
        {formatINR(row.net)}
      </td>
      <td className="money py-2.5 pr-4 text-right text-xs">
        {residual === 0 ? (
          <>
            <span aria-hidden className="text-muted-foreground">
              &mdash;
            </span>
            <span className="sr-only">
              no residual: the reconstruction equals the credit
            </span>
          </>
        ) : (
          <span className="font-medium">
            {formatINR(residual)}
            <span className="sr-only">
              {" "}
              — the reconstruction is this much{" "}
              {residual > 0 ? "above" : "below"} the bank credit
            </span>
          </span>
        )}
      </td>
      <td className="py-2.5 pr-4 text-xs">
        {row.tier !== null ? (
          <span className="font-mono text-xs">{row.tier}</span>
        ) : (
          // Not an error and not a gap: an unclosed batch is what a reviewer
          // opens this listing to find. The outcome hue is carried by a 1px
          // border, so the label keeps full text contrast on a selected row.
          <span className="inline-flex items-center rounded-full border border-excepted/60 px-1.5 py-px text-2xs font-medium">
            Not closed
          </span>
        )}
      </td>
      <td className="py-2.5 pr-4 font-mono text-xs">
        {row.bank_line_id ?? <Absent note="this batch closed against nothing" />}
      </td>
      <td className="py-2.5 pr-4 text-right">
        {/* The row is clickable for a mouse; a clickable <tr> cannot be
            reached from a keyboard. THIS is the control — in the tab order,
            naming its action, showing the shared focus ring. */}
        <button
          type="button"
          // The breakdown restores focus here when it closes, and finds this
          // button by the id it carries rather than through a ref threaded
          // down from the listing to the panel and back.
          data-settlement-trigger={row.settlement_id}
          aria-expanded={open}
          aria-controls="settlement-breakdown"
          onClick={(event) => {
            event.stopPropagation();
            onOpen(row.settlement_id);
          }}
          className="rounded-sm px-1 text-xs text-muted-foreground underline-offset-4 transition-colors duration-150 hover:text-foreground hover:underline active:text-foreground focus-visible:focus-ring"
        >
          Open
          <span className="sr-only"> the breakdown of {row.settlement_id}</span>
        </button>
      </td>
    </tr>
  );
}

function Amount({ value, muted }: { value: number; muted?: boolean }) {
  return (
    <td
      className={cn(
        "money py-2.5 pr-4 text-right text-xs",
        muted && "text-muted-foreground",
      )}
    >
      {formatINR(value)}
    </td>
  );
}
