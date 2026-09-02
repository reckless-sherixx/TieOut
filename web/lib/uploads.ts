/**
 * The four upload operations, and the vocabulary the console renders them in.
 *
 * Beside `lib/explorer.ts` rather than inside `lib/api.ts`, for the reason
 * that module gives: everything one surface needs lives together, and every
 * request still goes through `getJSON` / `postForm` — the ONE place this app
 * calls `fetch`.
 *
 * THREE PROPERTIES OF THE CONTRACT SHAPE EVERY SCREEN BUILT ON THIS.
 *
 * 1. **`already_ingested` is recognition, not failure.** The response is a
 *    200 carrying the first ingest's upload id, counts and format. A UI that
 *    coloured it red would be telling a merchant something went wrong when the
 *    system just told them it already has their file.
 *
 * 2. **Three states, not a record count.** `ingested`, `quarantined` and
 *    `empty` are three different things to say, and a screen that rendered
 *    "0 records" for the last two would collapse "your file is full of damage"
 *    into "your file is empty". `UPLOAD_STATE` below is the only place those
 *    sentences are written.
 *
 * 3. **A refusal is structured.** A file no adapter recognised comes back as a
 *    422 naming the threshold and every candidate format with its score.
 *    `refusalOf` narrows that off `ApiError.body`; a component that rendered
 *    only `error.message` would drop the numbers that answer "why not".
 */
import { ApiError, getJSON, postForm } from "./api";
import type { components } from "./api-types";

export type Upload = components["schemas"]["Upload"];
export type UploadReceipt = components["schemas"]["UploadReceipt"];
export type UploadState = components["schemas"]["UploadState"];
export type QuarantinedRow = components["schemas"]["QuarantinedRow"];
export type QuarantineReason = components["schemas"]["QuarantineReason"];
export type PaginatedQuarantine = components["schemas"]["PaginatedQuarantine"];
export type UploadRefusedError = components["schemas"]["UploadRefusedError"];

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

export const uploadsApi = {
  /** POST /api/uploads — multipart, one file. */
  createUpload(file: File, options?: RequestOptions) {
    const form = new FormData();
    form.append("file", file, file.name);
    return postForm<UploadReceipt>("/api/uploads", form, options);
  },

  /** GET /api/uploads — every file this org has sent, most recent first. */
  listUploads(options?: RequestOptions) {
    return getJSON<Upload[]>("/api/uploads", options);
  },

  /** GET /api/uploads/{id} */
  getUpload(id: string, options?: RequestOptions) {
    return getJSON<Upload>(`/api/uploads/${encodeURIComponent(id)}`, options);
  },

  /** GET /api/uploads/{id}/quarantine — server-side paginated, never client-side. */
  listQuarantine(
    id: string,
    params: { page?: number; size?: number } = {},
    options?: RequestOptions,
  ) {
    return getJSON<PaginatedQuarantine>(
      `/api/uploads/${encodeURIComponent(id)}/quarantine${query(params)}`,
      options,
    );
  },
};

/* ------------------------------------------------------------------ *
 * The refusal, narrowed off the error body
 * ------------------------------------------------------------------ */

/**
 * The structured 422 from `POST /api/uploads`, or null for any other failure.
 *
 * Every field is checked before the body is trusted. It arrives over the
 * network and the alternative — casting and hoping — turns a changed contract
 * into a blank panel rather than into the generic error the caller already
 * knows how to render.
 */
export function refusalOf(error: unknown): UploadRefusedError | null {
  if (!(error instanceof ApiError) || error.status !== 422) return null;
  const body = error.body as Partial<UploadRefusedError> | null;
  if (!body || typeof body !== "object") return null;
  if (typeof body.detail !== "string") return null;
  if (body.reason !== "UNRECOGNISED_FORMAT" && body.reason !== "UNDECODABLE_FILE") {
    return null;
  }
  if (typeof body.threshold !== "number" || !Array.isArray(body.candidates)) {
    return null;
  }
  return body as UploadRefusedError;
}

/* ------------------------------------------------------------------ *
 * Vocabulary
 * ------------------------------------------------------------------ */

/**
 * What each state MEANS, in one noun and one sentence.
 *
 * The sentences are the product, not decoration: `quarantined` and `empty`
 * both have a record count of zero and are the two facts a merchant most needs
 * told apart. One is "your export is damaged, here are the rows"; the other is
 * "you exported a range with nothing in it".
 */
export const UPLOAD_STATE: Record<
  UploadState,
  { label: string; note: string; tone: "matched" | "excepted" | "muted" }
> = {
  ingested: {
    label: "Read",
    note: "Canonical records came out of this file and it can feed a run.",
    tone: "matched",
  },
  quarantined: {
    label: "All rows refused",
    note: "The file was read and every row of it was refused. This is damage, not emptiness — the rows are below, kept exactly as they arrived.",
    tone: "excepted",
  },
  empty: {
    label: "No data rows",
    note: "The header was recognised and the file carried nothing after it. Usually the wrong date range rather than a data-quality problem.",
    tone: "muted",
  },
};

/**
 * Each quarantine reason as a merchant would say it.
 *
 * The API's own `detail` names the column and the offending value and is
 * always rendered beside this; what this adds is the category, so a reviewer
 * scanning four hundred rows can see that they are looking at one problem
 * repeated rather than four hundred problems.
 */
export const QUARANTINE_REASON: Record<QuarantineReason, string> = {
  BAD_DECIMAL: "Amount is not an exact rupee value",
  BAD_DATE: "Date does not match any layout this format uses",
  MISSING_VALUE: "A required cell is empty",
  TRUNCATED_ROW: "The line was cut short",
  EXTRA_FIELDS: "More fields than the header — usually an unquoted comma",
  DUPLICATE_ROW: "Byte-identical to a row already in this file",
  UNKNOWN_VALUE: "A category this layout does not define",
  UNSUPPORTED_ROW_TYPE: "Well-formed, but describes something the schema does not carry",
  AMBIGUOUS_DIRECTION: "Credit and debit both set, or neither",
  ARITHMETIC_MISMATCH: "The row's own columns disagree",
  SCHEMA_VIOLATION: "The values were rejected by the canonical model",
  UNDECODABLE_FILE: "No supported encoding decoded the bytes",
  MISSING_HEADER_COLUMN: "The header is missing a column this layout needs",
  // File-level, not row-level: an accepted file that produced no records at
  // all. Reported rather than passed over, because an ingest that succeeded
  // silently is indistinguishable from one that worked.
  EMPTY_DOCUMENT: "The format was recognised and no transaction rows followed",
  NOT_A_STATEMENT: "Recognised as this format but no readable structure found",
  UNRECOGNISED_FORMAT: "No adapter recognised this file",
};

/** The format ids this build reads, as a merchant would name the file. */
export const FORMAT_LABEL: Record<string, string> = {
  "razorpay-settlement-v2": "Razorpay settlement report",
  "bank-csv-hdfc-v1": "HDFC bank statement",
  "bank-csv-icici-v1": "ICICI bank statement",
  "mt940-v1": "MT940 bank statement",
  "orders-csv-shopify-v1": "Shopify order export",
  "cod-remittance-delhivery-v1": "Delhivery COD remittance",
};

export function formatLabel(formatId: string): string {
  return FORMAT_LABEL[formatId] ?? formatId;
}

/* ------------------------------------------------------------------ *
 * What a run needs
 * ------------------------------------------------------------------ */

/** The three inputs a reconciliation reads. */
export type SourceRole = "order" | "psp_txn" | "bank_line";

export const SOURCE_ROLE_LABEL: Record<SourceRole, string> = {
  order: "Sales register",
  psp_txn: "PSP settlement report",
  bank_line: "Bank statement",
};

/**
 * The same three roles as a COUNTABLE NOUN, singular and plural.
 *
 * `SOURCE_ROLE_LABEL` names the FILE a role comes from and completes "the
 * bank statement"; this names the ROW and completes "171 bank lines". The
 * uploads table read "171 bank statement" until the two were separated, which
 * is a phrase in no language.
 */
export const SOURCE_ROLE_UNIT: Record<SourceRole, [string, string]> = {
  order: ["order", "orders"],
  psp_txn: ["PSP leg", "PSP legs"],
  bank_line: ["bank line", "bank lines"],
};

export function unitFor(role: SourceRole, count: number): string {
  const [one, many] = SOURCE_ROLE_UNIT[role];
  return count === 1 ? one : many;
}

/** Which roles one upload supplies. A COD remittance supplies PSP legs. */
export function rolesOf(upload: Upload): SourceRole[] {
  const roles: SourceRole[] = [];
  if (upload.order_count > 0) roles.push("order");
  if (upload.psp_txn_count > 0) roles.push("psp_txn");
  if (upload.bank_line_count > 0) roles.push("bank_line");
  return roles;
}

/**
 * The roles a selection covers, and the ones it does not.
 *
 * **This is advice and never a gate.** The API accepts any set of uploads that
 * holds at least one record, and it is right to: a merchant reconciling COD
 * remittances against a bank statement has no Shopify export and their run is
 * still a real run. So a missing role is shown as a consequence — "no orders,
 * so nothing can be matched to an order" — rather than as a disabled button
 * that gives no reason.
 */
export function coverageOf(uploads: Upload[]): {
  covered: SourceRole[];
  missing: SourceRole[];
  records: number;
} {
  const covered = new Set<SourceRole>();
  let records = 0;
  for (const upload of uploads) {
    for (const role of rolesOf(upload)) covered.add(role);
    records += upload.record_count;
  }
  const all: SourceRole[] = ["order", "psp_txn", "bank_line"];
  return {
    covered: all.filter((role) => covered.has(role)),
    missing: all.filter((role) => !covered.has(role)),
    records,
  };
}

/** Bytes, at the precision a person reads. Never a fake unit for a small file. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes.toLocaleString("en-IN")} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
