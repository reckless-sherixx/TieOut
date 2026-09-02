"use client";

import * as React from "react";
import { XIcon } from "lucide-react";
import { useResource } from "@/lib/hooks";
import { formatINR } from "@/lib/money";
import { cn } from "@/lib/utils";
import {
  explorerApi,
  recordIdOf,
  RECORD_SOURCE_LABEL,
  RECORD_SOURCE_NOTE,
  type PaginatedRecords,
  type RecordSource,
} from "@/lib/explorer";
import type {
  BankLine,
  Order,
  PSPTransaction,
  RunState,
  SubjectRecord,
} from "@/lib/types";
import { EmptyState, ErrorState } from "@/components/States";
import { Panel, PanelHeader } from "@/components/Panel";
import { SubjectRecordView } from "@/components/SubjectRecordView";
import { Button } from "@/components/ui/button";
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

/**
 * The rows the engine actually read, one input table at a time.
 *
 * NOTHING IS PROJECTED OR SUMMARISED ON THE WAY OUT, so a null in a cell is
 * the ingested null: a PSP leg with no `order_id` IS the missing-order-
 * reference defect, and a bank line with no UTR is the reason narration
 * parsing has to work at all. They are written as nulls rather than as blank
 * cells for that reason.
 *
 * `source` travels with every request. It is required by the contract, and the
 * table narrows the union on the source the response echoes back — never by
 * sniffing for a field, which the contract forbids because `SubjectRecord` is
 * a bare union with no discriminator inside the records themselves.
 */
export function RecordTable({
  runId,
  runState,
  source,
  page,
  openId,
  onPageChange,
  onOpen,
  onClose,
}: {
  runId: string;
  /**
   * In the fetch key so a run that finishes re-reads. Records exist from
   * ingest onwards, so unlike the settlements listing this one is not empty
   * while a run executes.
   */
  runState: RunState;
  source: RecordSource;
  page: number;
  openId: string | null;
  onPageChange: (next: number) => void;
  onOpen: (recordId: string) => void;
  onClose: () => void;
}) {
  const { data, error, loading, refresh } = useResource<PaginatedRecords>(
    `records:${runId}:${runState}:${source}:${page}`,
    (signal) =>
      explorerApi.listRunRecords(
        runId,
        { source, page, size: PAGE_SIZE },
        { signal },
      ),
  );

  // The source is part of the row-set identity, not of the page: switching
  // from orders to bank lines is a different table, and holding the old rows
  // across it would leave order columns filled with bank data for a beat.
  const { shown, stale } = useHeldPage(
    `${runId}:${runState}:${source}`,
    data,
    loading,
  );

  const rows = shown?.items ?? [];
  const range = pageRange(page, shown?.total ?? 0);
  const columns = COLUMNS[source];

  const openRecord =
    rows.find((record) => recordIdOf(source, record) === openId) ?? null;

  if (error) {
    return (
      <ErrorState
        title={`The ${RECORD_SOURCE_LABEL[source].toLowerCase()} did not load`}
        error={error}
        recovery={
          <>
            <code className="font-mono">
              GET /api/runs/{runId}/records?source={source}&amp;page={page}
            </code>{" "}
            failed. Retry below. If the message names{" "}
            <code className="font-mono">source</code>, the request reached the
            API and was refused for a missing or unrecognised value — the three
            it accepts are <code className="font-mono">order</code>,{" "}
            <code className="font-mono">psp_txn</code> and{" "}
            <code className="font-mono">bank_line</code>, and picking one above
            recovers it.
          </>
        }
        onRetry={refresh}
      />
    );
  }

  if (loading && shown === null) {
    return (
      <TableSkeleton
        label={`Loading the ${RECORD_SOURCE_LABEL[source].toLowerCase()}`}
        columns={columns.map((column) => column.skeleton)}
      />
    );
  }

  if (isPastTheEnd(range, rows.length)) {
    return (
      <PastTheEnd
        range={range}
        unit={RECORD_SOURCE_LABEL[source].toLowerCase()}
        onPageChange={onPageChange}
      />
    );
  }

  if (rows.length === 0) {
    return (
      <EmptyState
        title={`This run ingested no ${RECORD_SOURCE_LABEL[source].toLowerCase()}`}
        reason={
          <>
            The request named{" "}
            <code className="font-mono">source={source}</code> and the API
            answered with zero rows, so this is a measured emptiness rather than
            a question that was never asked. {RECORD_SOURCE_NOTE[source]}
          </>
        }
      />
    );
  }

  return (
    <div className="space-y-4">
      <PageBar
        range={range}
        unit={RECORD_SOURCE_LABEL[source].toLowerCase()}
        busy={stale}
        onPageChange={onPageChange}
      />

      <div
        aria-busy={stale}
        className={cn(
          // `relative` is load-bearing: the screen-reader-only spans in these
          // cells are absolutely positioned, and without a positioned ancestor
          // they escape the scroll container and drag the page out to the
          // table's full width on a narrow viewport.
          "relative overflow-x-auto rounded-xl border border-border transition-opacity duration-150",
          stale && "opacity-60",
        )}
      >
        {stale ? (
          <span className="sr-only" role="status">
            Loading page {page}
          </span>
        ) : null}

        <table
          className={cn("w-full border-collapse text-left", MIN_WIDTH[source])}
        >
          <caption className="sr-only">
            {RECORD_SOURCE_LABEL[source]} {range.from} to {range.to} of{" "}
            {range.total}, as ingested.
          </caption>
          <thead>
            <tr className="border-b border-border bg-surface">
              {columns.map((column, i) => (
                <Th
                  key={column.key}
                  className={cn(column.width, i === 0 && "pl-4")}
                >
                  {column.header}
                </Th>
              ))}
              <Th className="w-24 pr-4 text-right">Record</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((record) => {
              const id = recordIdOf(source, record);
              const open = id === openId;
              return (
                <tr
                  key={id}
                  onClick={() => onOpen(id)}
                  className={cn(
                    "cursor-pointer border-b border-border transition-colors duration-150 last:border-b-0",
                    open ? "bg-surface-selected" : "hover:bg-surface-hover",
                  )}
                >
                  {columns.map((column, i) =>
                    i === 0 ? (
                      <th
                        key={column.key}
                        scope="row"
                        className="py-2.5 pr-4 pl-4 text-left font-mono text-xs font-normal"
                      >
                        {column.cell(record)}
                      </th>
                    ) : (
                      <td
                        key={column.key}
                        className={cn("py-2.5 pr-4 text-xs", column.align)}
                      >
                        {column.cell(record)}
                      </td>
                    ),
                  )}
                  <td className="py-2.5 pr-4 text-right">
                    {/* The row is clickable for a mouse; a clickable <tr>
                        cannot be reached from a keyboard. THIS is the control. */}
                    <button
                      type="button"
                      aria-expanded={open}
                      aria-controls="record-detail"
                      onClick={(event) => {
                        event.stopPropagation();
                        onOpen(id);
                      }}
                      className="rounded-sm px-1 text-xs text-muted-foreground underline-offset-4 transition-colors duration-150 hover:text-foreground hover:underline active:text-foreground focus-visible:focus-ring"
                    >
                      Open
                      <span className="sr-only"> the full record {id}</span>
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {openRecord ? (
        <Panel id="record-detail">
          <PanelHeader
            title={
              <span className="font-mono text-sm">
                {recordIdOf(source, openRecord)}
              </span>
            }
            description={
              <>
                Every field as ingested, including the ones the table above has
                no column for. {RECORD_SOURCE_NOTE[source]}
              </>
            }
            action={
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={`Close the record ${recordIdOf(source, openRecord)}`}
                onClick={onClose}
              >
                <XIcon aria-hidden strokeWidth={2} />
              </Button>
            }
          />
          <div className="border-t border-border px-6 py-6">
            {/* No scroller of ours around this any more. The record view used
                to set `white-space: nowrap` across a figure and its paise
                integer together, which was wider than a 375px column; it now
                breaks between the two and fits, so wrapping it was treating a
                symptom in the wrong component. */}
            <SubjectRecordView subjectType={source} subject={openRecord} />
          </div>
        </Panel>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Columns, one set per input shape
 * ------------------------------------------------------------------ */

type Column = {
  key: string;
  header: string;
  /** Column width utility, so the three tables share one rhythm. */
  width?: string;
  /** Cell alignment utility. */
  align?: string;
  /** Placeholder width used by the first-load skeleton. */
  skeleton: string;
  cell: (record: SubjectRecord) => React.ReactNode;
};

const money = (paise: number) => (
  <span className="money">{formatINR(paise)}</span>
);

const ORDER_COLUMNS: Column[] = [
  {
    key: "order_id",
    header: "Order",
    width: "w-36",
    skeleton: "6rem",
    cell: (r) => (r as Order).order_id,
  },
  {
    key: "order_date",
    header: "Order date",
    width: "w-32",
    skeleton: "5rem",
    cell: (r) => (r as Order).order_date,
  },
  {
    key: "customer_ref",
    header: "Customer",
    width: "w-36",
    skeleton: "5rem",
    cell: (r) => <span className="font-mono">{(r as Order).customer_ref}</span>,
  },
  {
    key: "gross_amount",
    header: "Gross",
    width: "w-36",
    align: "text-right",
    skeleton: "5rem",
    cell: (r) => money((r as Order).gross_amount),
  },
  // `currency` has one legal value in the contract, so a column of it would be
  // 50 identical cells. It is in the record panel, where a constant still
  // answers a question a reader might have.
  {
    key: "status",
    header: "Status",
    skeleton: "6rem",
    cell: (r) => (r as Order).status,
  },
];

const PSP_COLUMNS: Column[] = [
  {
    key: "txn_id",
    header: "Transaction",
    width: "w-36",
    skeleton: "6rem",
    cell: (r) => (r as PSPTransaction).txn_id,
  },
  {
    key: "txn_type",
    header: "Type",
    width: "w-28",
    skeleton: "4rem",
    cell: (r) => (r as PSPTransaction).txn_type,
  },
  {
    key: "order_id",
    header: "Order",
    width: "w-36",
    skeleton: "5rem",
    cell: (r) => {
      const id = (r as PSPTransaction).order_id;
      return id !== null ? (
        <span className="font-mono">{id}</span>
      ) : (
        <Absent note="missing order reference" />
      );
    },
  },
  {
    key: "captured_at",
    header: "Captured at",
    width: "w-52",
    skeleton: "8rem",
    cell: (r) => (
      <span className="font-mono text-2xs">
        {(r as PSPTransaction).captured_at}
      </span>
    ),
  },
  {
    // SIGNED from the merchant's point of view: payments positive, every
    // deduction negative. Not a bank line's unsigned magnitude.
    key: "amount",
    header: "Amount (signed)",
    width: "w-36",
    align: "text-right",
    skeleton: "5rem",
    cell: (r) => money((r as PSPTransaction).amount),
  },
  {
    key: "settlement_id",
    header: "Settlement",
    width: "w-36",
    skeleton: "5rem",
    cell: (r) => {
      const id = (r as PSPTransaction).settlement_id;
      return id !== null ? (
        <span className="font-mono">{id}</span>
      ) : (
        <Absent note="in no settlement batch" />
      );
    },
  },
  {
    key: "settled_at",
    header: "Settled at",
    skeleton: "5rem",
    cell: (r) => {
      const at = (r as PSPTransaction).settled_at;
      return at !== null ? at : <Absent note="not settled" />;
    },
  },
];

const BANK_COLUMNS: Column[] = [
  {
    key: "line_id",
    header: "Line",
    width: "w-32",
    skeleton: "5rem",
    cell: (r) => (r as BankLine).line_id,
  },
  {
    key: "txn_date",
    header: "Date",
    width: "w-28",
    skeleton: "5rem",
    cell: (r) => (r as BankLine).txn_date,
  },
  {
    key: "narration",
    header: "Narration",
    width: "w-[24rem]",
    skeleton: "12rem",
    cell: (r) => (
      // Verbatim, double spaces and all — the garbling IS the data, and
      // normalising it for display erases the defect. Clipped to the column
      // rather than rewritten; the full string is in the record panel.
      <span
        className="block overflow-hidden font-mono text-2xs text-ellipsis whitespace-pre"
        title={(r as BankLine).narration}
      >
        {(r as BankLine).narration}
      </span>
    ),
  },
  {
    key: "credit",
    header: "Credit",
    width: "w-36",
    align: "text-right",
    skeleton: "5rem",
    cell: (r) => {
      const credit = (r as BankLine).credit;
      return credit !== null ? money(credit) : <Absent note="debit line" />;
    },
  },
  {
    key: "debit",
    header: "Debit",
    width: "w-36",
    align: "text-right",
    skeleton: "5rem",
    cell: (r) => {
      const debit = (r as BankLine).debit;
      return debit !== null ? money(debit) : <Absent note="credit line" />;
    },
  },
  {
    key: "balance",
    header: "Balance",
    width: "w-36",
    align: "text-right",
    skeleton: "5rem",
    cell: (r) => money((r as BankLine).balance),
  },
  {
    key: "utr",
    header: "UTR",
    skeleton: "7rem",
    cell: (r) => {
      const utr = (r as BankLine).utr;
      return utr !== null ? (
        <span className="font-mono">{utr}</span>
      ) : (
        <Absent note="absent from this line" />
      );
    },
  },
];

const COLUMNS: Record<RecordSource, Column[]> = {
  order: ORDER_COLUMNS,
  psp_txn: PSP_COLUMNS,
  bank_line: BANK_COLUMNS,
};

/** Below these the table scrolls inside its own container, never the page. */
const MIN_WIDTH: Record<RecordSource, string> = {
  order: "min-w-[52rem]",
  psp_txn: "min-w-[62rem]",
  bank_line: "min-w-[70rem]",
};
