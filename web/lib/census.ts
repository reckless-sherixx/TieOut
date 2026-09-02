"use client";

import * as React from "react";
import { api } from "./api";
import { REASON_CODES } from "./labels";
import type {
  PaginatedReconExceptions,
  ReasonCode,
  VerifierCheck,
} from "./types";

/**
 * COUNTING THINGS THE CONTRACT DOES NOT COUNT FOR US.
 *
 * Two pages need aggregates the wire does not carry. The exception list wants
 * how many rows each reason code has — including the codes with none, because
 * five of the eight score zero on this generator and a zero there means "not
 * exercised by this data", not "handled". The analyst view wants how many
 * hypotheses were proposed and which check rejected each one, and
 * `Metrics` carries a rejection RATE rather than the two integers behind it.
 *
 * Both are computed here from the paginated exception endpoint, server-side,
 * and both report their own coverage. That second half is the important half:
 * a count over an unknown fraction of the rows is not a measurement, so every
 * result below says exactly how many rows it saw out of how many exist, and
 * whether it saw all of them.
 *
 * NOTHING HERE DIVIDES ONE WIRE FLOAT BY ANOTHER TO RECOVER AN INTEGER. The
 * number of hypotheses proposed is recoverable in principle from
 * `llm_rejection_rate` and `tier_counts.LLM`, and it is not recovered that way,
 * because a derived integer that looks measured is worse than an absent one.
 * What is counted here is counted by looking at rows.
 */

/** Rows per request during a full scan. Larger pages, fewer round trips. */
const SCAN_PAGE_SIZE = 200;

/**
 * The most requests one scan will make: 25 × 200 = 5,000 rows, which is the
 * largest exception list this product is specified to handle. Past that the
 * scan stops and says so rather than issuing an unbounded number of requests
 * against an API that has no aggregate endpoint. A partial census that admits
 * it is partial is useful; an unbounded loop is not.
 */
const MAX_SCAN_PAGES = 25;

export type ReasonCensus = {
  /** Rows per reason code. All eight keys, always — the zeros are the point. */
  byReason: Record<ReasonCode, number>;
  /** Their sum. Checkable against `RunSummary.exception_count`. */
  total: number;
};

export type ReasonCensusResult = {
  data: ReasonCensus | null;
  error: Error | null;
  loading: boolean;
};

/**
 * How many exceptions each reason code has, from the server.
 *
 * One request per code, asking for a single row and reading only `total` off
 * the pagination envelope. Eight tiny requests rather than one scan of every
 * row: the filter is a server-side query parameter, so the server already knows
 * these numbers and the client never has to hold 5,000 rows to count them.
 */
export function useReasonCensus(
  runId: string,
  /** In the key, not decoration: a finishing run goes from 0 rows to all of them. */
  runState: string,
  enabled = true,
): ReasonCensusResult {
  const [state, setState] = React.useState<{
    key: string;
    data: ReasonCensus | null;
    error: Error | null;
  } | null>(null);

  const key = `reason-census:${runId}:${runState}`;

  React.useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const controller = new AbortController();

    void (async () => {
      try {
        const pages = await Promise.all(
          REASON_CODES.map((code) =>
            api.listRunExceptions(
              runId,
              { reason_code: code, page: 1, size: 1 },
              { signal: controller.signal },
            ),
          ),
        );
        if (cancelled) return;
        const byReason = {} as Record<ReasonCode, number>;
        REASON_CODES.forEach((code, i) => {
          byReason[code] = pages[i].total;
        });
        const total = REASON_CODES.reduce((sum, c) => sum + byReason[c], 0);
        setState({ key, data: { byReason, total }, error: null });
      } catch (cause) {
        if (cancelled) return;
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setState({
          key,
          data: null,
          error: cause instanceof Error ? cause : new Error(String(cause)),
        });
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [key, runId, enabled]);

  const current = state?.key === key ? state : null;
  return {
    data: current?.data ?? null,
    error: current?.error ?? null,
    loading: enabled && current === null,
  };
}

export type HypothesisCensus = {
  /** Rows the scan actually read. */
  scanned: number;
  /** Rows the server says exist, from the first response's `total`. */
  total: number;
  /** True when `scanned` reached `total`. Everything else is a sample. */
  complete: boolean;
  /** True when the scan stopped at the request budget rather than at the end. */
  truncated: boolean;
  /** Rows carrying a non-null `llm_hypothesis`. */
  withHypothesis: number;
  /** Rows carrying a hypothesis the verifier rejected. */
  rejected: number;
  /** Rows carrying a hypothesis the verifier accepted, still on this list. */
  accepted: number;
  /** Rows where no hypothesis was attempted at all. */
  notAttempted: number;
  /**
   * Rejections per failed check. All five wire spellings, always, including
   * the zeros — the same argument as the tier counts.
   */
  byCheck: Record<VerifierCheck, number>;
  /** Rejections whose `failed_check` was null, which the contract permits. */
  rejectedWithoutCheck: number;
};

export type HypothesisCensusResult = {
  data: HypothesisCensus | null;
  error: Error | null;
  /** True until the first page lands. After that `data` grows as pages arrive. */
  loading: boolean;
  /** True while more pages are still being read. */
  scanning: boolean;
  refresh: () => void;
};

const EMPTY_CHECKS: Record<VerifierCheck, number> = {
  existence: 0,
  exclusivity: 0,
  causality: 0,
  arithmetic: 0,
  uniqueness: 0,
};

/**
 * Every hypothesis on a run's exception list, and what the verifier did with it.
 *
 * Reads the paginated endpoint page by page — sequentially, so a slow API is
 * never hit with twenty-five requests at once — and publishes a running result
 * after each page. The view can therefore report "counted 800 of 5,000 so far"
 * instead of a spinner, and the numbers it shows are always true of the rows it
 * has actually seen.
 *
 * ONE THING THIS CENSUS STRUCTURALLY CANNOT SEE. An accepted hypothesis becomes
 * a match, and a match is not an exception, so it leaves this list. Acceptances
 * are read from `tier_counts.LLM` instead, and the `accepted` field here counts
 * only the rows that carry an accepted verdict and are still exceptions.
 */
export function useHypothesisCensus(
  runId: string,
  runState: string,
  enabled = true,
): HypothesisCensusResult {
  const [nonce, setNonce] = React.useState(0);
  const [state, setState] = React.useState<{
    key: string;
    data: HypothesisCensus | null;
    error: Error | null;
    scanning: boolean;
  } | null>(null);

  const key = `hypothesis-census:${runId}:${runState}:${nonce}`;

  React.useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const controller = new AbortController();

    void (async () => {
      const acc: HypothesisCensus = {
        scanned: 0,
        total: 0,
        complete: false,
        truncated: false,
        withHypothesis: 0,
        rejected: 0,
        accepted: 0,
        notAttempted: 0,
        byCheck: { ...EMPTY_CHECKS },
        rejectedWithoutCheck: 0,
      };

      try {
        for (let page = 1; page <= MAX_SCAN_PAGES; page += 1) {
          const res: PaginatedReconExceptions = await api.listRunExceptions(
            runId,
            { page, size: SCAN_PAGE_SIZE },
            { signal: controller.signal },
          );
          if (cancelled) return;

          acc.total = res.total;
          for (const row of res.items) {
            acc.scanned += 1;
            if (row.llm_hypothesis !== null) acc.withHypothesis += 1;
            if (row.verifier_verdict === "rejected") {
              acc.rejected += 1;
              if (row.failed_check === null) acc.rejectedWithoutCheck += 1;
              else acc.byCheck[row.failed_check] += 1;
            } else if (row.verifier_verdict === "accepted") {
              acc.accepted += 1;
            } else {
              acc.notAttempted += 1;
            }
          }

          // The server is the authority on how big a page it gave us: asking
          // for 200 and being handed 50 is a legitimate answer, and advancing
          // on the requested size rather than the returned one would skip rows.
          const done =
            res.items.length === 0 || acc.scanned >= res.total || res.size <= 0;
          acc.complete = acc.scanned >= res.total;
          acc.truncated = !acc.complete && page === MAX_SCAN_PAGES;

          setState({
            key,
            data: { ...acc, byCheck: { ...acc.byCheck } },
            error: null,
            scanning: !done && page < MAX_SCAN_PAGES,
          });

          if (done) return;
        }
      } catch (cause) {
        if (cancelled) return;
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setState({
          key,
          data: null,
          error: cause instanceof Error ? cause : new Error(String(cause)),
          scanning: false,
        });
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [key, runId, enabled]);

  const current = state?.key === key ? state : null;
  const refresh = React.useCallback(() => setNonce((n) => n + 1), []);

  return {
    data: current?.data ?? null,
    error: current?.error ?? null,
    loading: enabled && current === null,
    scanning: current?.scanning ?? false,
    refresh,
  };
}
