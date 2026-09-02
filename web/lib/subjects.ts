"use client";

import * as React from "react";
import { explorerApi } from "./explorer";
import { REASON_CODES } from "./labels";
import { useReasonCensus } from "./census";
import type { ReasonCode, RunState, SubjectType } from "./types";

/**
 * TWO POPULATIONS, ONE PARTITION, AND THE NOUN THAT WAS WRONG.
 *
 * The engine's central invariant is that every SUBJECT is matched or excepted,
 * exactly once. `match_count + exception_count` is therefore the subject count
 * and nothing else. It is not the bank-line count, and the summary used to
 * label it as one — 181 under the caption "Bank lines this run accounted for"
 * on a run whose Records tab reports 171 bank lines, one click away.
 *
 * The gap is real and is not an error: matches are all bank-line subjects, but
 * exceptions are raised against TWO subject types. `core/matcher/engine.py`
 * builds the exception partition in two passes — `_bank_line_exceptions` over
 * every unmatched bank line, and `_psp_exceptions` over PSP rows that could not
 * be placed — so a run's exception list mixes bank lines with PSP transactions.
 * On seed 42 at 500 records that is 30 bank lines and 10 PSP rows.
 *
 * THE IDENTITY THIS MODULE EXISTS TO MAKE RENDERABLE:
 *
 *     match_count + bank-line exceptions  ==  bank lines in the run
 *     141         + 30                    ==  171
 *
 *     match_count + ALL exceptions        ==  subjects accounted for
 *     141         + 40                    ==  181
 *
 * Both are true, they are different quantities, and the console must name which
 * one it is showing. The first is the number the Records tab reports; the
 * second is the one the partition sums to.
 */

/**
 * WHICH SUBJECT TYPE EACH REASON CODE IS RAISED AGAINST.
 *
 * Fixed by construction rather than by convention: `_classify` in
 * `core/matcher/engine.py` runs only over bank lines and can only return the
 * five codes below, and `_psp_exceptions` raises only the three PSP ones. No
 * code appears in both passes.
 *
 * This map is nonetheless AN INFERENCE, because the exceptions endpoint has no
 * `subject_type` filter and `Metrics` carries no split — so it is checked
 * rather than trusted. `useSubjectAccounting` measures the run's bank lines
 * independently from `GET /api/runs/{id}/records?source=bank_line` and the view
 * reports the two against each other. If the engine ever raised a code against
 * the other subject type, the identity above would stop holding and the summary
 * would say so instead of quietly printing a wrong number under a right label.
 */
export const REASON_CODE_SUBJECT: Record<ReasonCode, SubjectType> = {
  NO_SETTLEMENT_REF: "bank_line",
  AMOUNT_MISMATCH: "bank_line",
  ORPHAN_BANK_LINE: "bank_line",
  AMBIGUOUS_MULTI_CANDIDATE: "bank_line",
  UNPARSEABLE_NARRATION: "bank_line",
  ORPHAN_PSP_TXN: "psp_txn",
  DUPLICATE_PSP_TXN: "psp_txn",
  MISSING_ORDER_REF: "psp_txn",
};

export type SubjectAccountingData = {
  /** Exceptions whose subject is a bank line, from the reason-code census. */
  bankLineExceptions: number;
  /** Exceptions whose subject is a PSP transaction. */
  pspExceptions: number;
  /** Their sum. Checkable against `RunSummary.exception_count`. */
  censusTotal: number;
  /**
   * Bank lines the run ingested, from the records listing's own `total` — the
   * same figure, from the same endpoint, that the Records tab renders.
   */
  bankLineRecords: number;
};

export type SubjectAccountingResult = {
  data: SubjectAccountingData | null;
  error: Error | null;
  loading: boolean;
};

/**
 * The exception split, and the bank-line count to check it against.
 *
 * Nine requests, all of them `size=1`, all of them answered from the server's
 * own counts: eight filtered exception totals (the same census the exception
 * list already builds, so the two surfaces cannot disagree) and one bank-line
 * records total. Nothing is scanned and nothing is re-counted in the browser.
 */
export function useSubjectAccounting(
  runId: string,
  /**
   * In the key, not decoration: a finishing run goes from zero exceptions to
   * all of them, and a census read while it was still executing would stick.
   */
  runState: RunState,
  enabled = true,
): SubjectAccountingResult {
  const census = useReasonCensus(runId, runState, enabled);

  const [records, setRecords] = React.useState<{
    key: string;
    total: number | null;
    error: Error | null;
  } | null>(null);

  const key = `bank-lines:${runId}:${runState}`;

  React.useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const controller = new AbortController();

    void (async () => {
      try {
        const page = await explorerApi.listRunRecords(
          runId,
          { source: "bank_line", page: 1, size: 1 },
          { signal: controller.signal },
        );
        if (cancelled) return;
        setRecords({ key, total: page.total, error: null });
      } catch (cause) {
        if (cancelled) return;
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setRecords({
          key,
          total: null,
          error: cause instanceof Error ? cause : new Error(String(cause)),
        });
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [key, runId, enabled]);

  const current = records?.key === key ? records : null;

  const error = census.error ?? current?.error ?? null;
  const loading = enabled && (census.loading || current === null) && error === null;

  if (error !== null || census.data === null || current?.total == null) {
    return { data: null, error, loading };
  }

  let bankLineExceptions = 0;
  let pspExceptions = 0;
  for (const code of REASON_CODES) {
    const rows = census.data.byReason[code];
    if (REASON_CODE_SUBJECT[code] === "bank_line") bankLineExceptions += rows;
    else pspExceptions += rows;
  }

  return {
    data: {
      bankLineExceptions,
      pspExceptions,
      censusTotal: census.data.total,
      bankLineRecords: current.total,
    },
    error: null,
    loading: false,
  };
}
