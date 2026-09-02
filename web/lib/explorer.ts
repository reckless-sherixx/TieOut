/**
 * The two data-explorer listings, typed off `api/openapi.yaml`.
 *
 * WHY THIS IS NOT IN `lib/api.ts` AND `lib/types.ts`. Those two are shared
 * with every other view of the console and are being edited on another branch
 * while this one is being written, so everything the explorer needs is added
 * here instead of threaded through them. Nothing is re-implemented: the types
 * come from the same generated `lib/api-types.ts` the rest of the app reads,
 * and every request below goes through `getJSON` — the ONE function in this
 * app that calls `fetch`. There is still exactly one network boundary.
 *
 * TWO PROPERTIES OF THE CONTRACT SHAPE EVERYTHING BELOW.
 *
 * 1. `source` on the records listing is REQUIRED. Omitting it is a 422 naming
 *    the three legal values, deliberately, because an empty page reads as
 *    "this run has no orders" and that is a different claim from "no such
 *    source". `listRunRecords` therefore takes `source` as a required
 *    positional part of its params object rather than an optional filter, so
 *    a call that forgets it does not compile.
 *
 * 2. Every money field on a `Settlement` is the engine's own number, and on a
 *    T3 row `gross - fees - tax - refunds - holds` does not equal `net`. That
 *    is the definition, not a rounding slip — `net` IS the bank credit, and a
 *    T3 match is precisely one where the reconstruction and the credit
 *    disagree within the matcher's tolerance. `residualOf` below exists to
 *    RENDER that gap. Nothing here ever writes a corrected `net` back.
 */
import { getJSON } from "./api";
import type { components } from "./api-types";
import type { BankLine, Order, PSPTransaction, SubjectRecord } from "./types";

export type Settlement = components["schemas"]["Settlement"];
export type PaginatedSettlements =
  components["schemas"]["PaginatedSettlements"];
export type PaginatedRecords = components["schemas"]["PaginatedRecords"];
export type RecordSource = components["schemas"]["RecordSource"];

/** The tier that closed a settlement. Null on the wire means unmatched. */
export type SettlementTier = NonNullable<Settlement["tier"]>;

type RequestOptions = { signal?: AbortSignal };

function query(params: Record<string, string | number | undefined | null>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const s = search.toString();
  return s ? `?${s}` : "";
}

export const explorerApi = {
  /** GET /api/runs/{id}/settlements — every settlement, matched or not. */
  listRunSettlements(
    id: string,
    params: { page?: number; size?: number } = {},
    options?: RequestOptions,
  ) {
    return getJSON<PaginatedSettlements>(
      `/api/runs/${encodeURIComponent(id)}/settlements${query(params)}`,
      options,
    );
  },

  /**
   * GET /api/runs/{id}/records — one source at a time.
   *
   * `source` is not optional here and must not be made optional: see the
   * module header. The response echoes it back, which is the only reliable
   * tag for narrowing `SubjectRecord`.
   */
  listRunRecords(
    id: string,
    params: { source: RecordSource; page?: number; size?: number },
    options?: RequestOptions,
  ) {
    return getJSON<PaginatedRecords>(
      `/api/runs/${encodeURIComponent(id)}/records${query(params)}`,
      options,
    );
  },
};

/* ------------------------------------------------------------------ *
 * The settlement breakdown, and the gap the contract permits in it
 * ------------------------------------------------------------------ */

/**
 * The six money fields the netting arithmetic runs over.
 *
 * `Settlement` and `BatchNetting` both carry them, and both are netted the same
 * way — so the two functions below take the shape rather than either type. The
 * netting diagram used to hold its own copy of this arithmetic, WITH THE
 * OPPOSITE SIGN, which is how one screen came to show the residual as −₹0.50 in
 * a table and ₹0.50 in the drawing beside it.
 */
export type NettingColumns = {
  gross: number;
  fees: number;
  tax: number;
  refunds: number;
  holds: number;
  /** The bank credit. NOT the sum of the five above — that is the point. */
  net: number;
};

/**
 * `gross - fees - tax - refunds - holds`, from the engine's own emitted
 * fields.
 *
 * This is NOT a second reconstruction: it never touches a raw PSP row. A
 * naive listing that re-summed the stored legs was wrong on 14 of 166
 * settlements, because the matcher suppresses duplicate payment legs before
 * it reconstructs — so the only correct source for these six numbers is the
 * row the API sent, and this function just adds five of them up so the sixth
 * can be compared against the total.
 */
export function reconstructedNet(s: NettingColumns): number {
  return s.gross - s.fees - s.tax - s.refunds - s.holds;
}

/**
 * THE RESIDUAL, WITH ONE SIGN, IN ONE PLACE: reconstruction − bank credit.
 *
 * Zero on almost every row; non-zero exactly where the engine matched at T3.
 * Positive means the batch reconstructs to MORE than the bank actually
 * credited.
 *
 * The direction is the engine's own, not a choice made here. `setl_00021`'s
 * evidence line — quoted verbatim at the foot of the breakdown panel that
 * renders this number — reads `residual delta=50 paise (net - credit)`, where
 * `net` is the reconstruction and `credit` is the bank line. This function
 * computes that same subtraction and therefore agrees with it. It previously
 * computed the reverse and printed −50 beside the engine's +50, on the same
 * screen, four times in two directions.
 *
 * Note the collision the direction resolves: the WIRE field `Settlement.net` is
 * the bank CREDIT, while the engine's prose calls the reconstruction "net". So
 * the label everywhere in this UI is `reconstruction − net`, naming both halves
 * by what they are rather than reusing a word that means two things.
 *
 * MUST BE RENDERED WHEREVER IT IS NON-ZERO. A UI that quietly balanced the
 * columns would be telling a reviewer something false about how the match was
 * made: it would claim the batch reconstructed to the credit exactly, when
 * what actually happened is that the two disagreed and a tolerance rule
 * accepted them anyway.
 */
export function residualOf(s: NettingColumns): number {
  return reconstructedNet(s) - s.net;
}

/* ------------------------------------------------------------------ *
 * Record sources
 * ------------------------------------------------------------------ */

export const RECORD_SOURCES: readonly RecordSource[] = [
  "order",
  "psp_txn",
  "bank_line",
] as const;

/** Plural, because these name a table rather than one row of it. */
export const RECORD_SOURCE_LABEL: Record<RecordSource, string> = {
  order: "Orders",
  psp_txn: "PSP transactions",
  bank_line: "Bank lines",
};

/** Which of the three real-world files each source is. */
export const RECORD_SOURCE_NOTE: Record<RecordSource, string> = {
  order: "The sales register: one row per order, with the gross the customer was charged.",
  psp_txn:
    "The PSP settlement report: payment, refund, fee, tax, chargeback, reserve and adjustment legs. Amounts are signed from the merchant's point of view.",
  bank_line:
    "The bank statement: one row per posted line, narration verbatim. Credit and debit are unsigned magnitudes and direction is carried by which of the two is populated.",
};

export function isRecordSource(value: unknown): value is RecordSource {
  return (
    typeof value === "string" &&
    (RECORD_SOURCES as readonly string[]).includes(value)
  );
}

/**
 * The id field of a record, chosen by its declared source.
 *
 * Narrowed on the page's `source`, never by sniffing for a field —
 * `SubjectRecord` is a bare union with no discriminator inside the records
 * themselves, and the contract says so explicitly.
 */
export function recordIdOf(source: RecordSource, record: SubjectRecord): string {
  switch (source) {
    case "order":
      return (record as Order).order_id;
    case "psp_txn":
      return (record as PSPTransaction).txn_id;
    case "bank_line":
      return (record as BankLine).line_id;
  }
}
