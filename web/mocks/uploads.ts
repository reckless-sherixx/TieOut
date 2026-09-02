/**
 * MSW handlers for the four upload operations, and a store behind them.
 *
 * These mocks do real work rather than returning a canned receipt, because the
 * three things the `/uploads` screen is built on are all properties of the
 * FILE and a fixture cannot exercise them:
 *
 * * **Detection by header shape.** The header is scored against each layout's
 *   required and distinctive columns — the same shape `header_confidence` uses
 *   in `core/adapters/base.py`, not the same code — so dropping a real
 *   Razorpay export in mock mode detects a Razorpay export, and dropping a
 *   renamed one detects it anyway.
 * * **Idempotency by content.** The SHA-256 of the bytes is computed with
 *   `crypto.subtle`, so re-uploading the same file returns the same id with
 *   `already_ingested: true` — the beat that is impossible to demonstrate
 *   against a stub that keys on the filename.
 * * **Quarantine.** Rows with the wrong field count, or with an amount that is
 *   not an exact rupee decimal, are refused and kept verbatim. That is a
 *   fraction of what the six adapters check and it is deliberately not
 *   presented as more: what it produces is a review table with real rows in
 *   it, which is what the screen has to be judged on.
 *
 * THE STORE IS SEEDED WITH FOUR FILES so the listing, the run-from-uploads
 * flow and the quarantine review are all reachable without dropping anything.
 * One of them read cleanly and carried no data rows at all, which is the
 * state the console most has to tell apart from a file full of damage --
 * and another carries the damage, so both are on the screen at once.
 *
 * What these handlers must never do is invent a field the contract does not
 * declare. Everything below is `components["schemas"]["Upload"]` and nothing
 * else.
 */
import { http, HttpResponse, delay } from "msw";
import { API_BASE } from "@/lib/api";
import type {
  PaginatedQuarantine,
  QuarantineReason,
  QuarantinedRow,
  Upload,
  UploadReceipt,
  UploadRefusedError,
} from "@/lib/uploads";

const url = (path: string) => `${API_BASE}${path}`;

const notFound = (detail: string) =>
  HttpResponse.json({ detail }, { status: 404 });

/** `core/adapters/base.py:DETECTION_THRESHOLD`. Below this, nothing is guessed. */
const THRESHOLD = 0.6;

/**
 * Each layout's required and distinctive columns, from the adapters
 * themselves. Required columns decide whether it is this format at all;
 * distinctive ones are what stop two bank layouts tying on one file.
 */
const LAYOUTS: {
  format_id: string;
  format_version: string;
  required: string[];
  distinctive: string[];
  role: "order" | "psp_txn" | "bank_line";
}[] = [
  {
    format_id: "razorpay-settlement-v2",
    format_version: "2.0-per-transaction",
    required: ["entity_id", "type", "amount", "settlement_utr"],
    distinctive: ["fee (exclusive tax)", "tax", "on_hold", "settled"],
    role: "psp_txn",
  },
  {
    format_id: "bank-csv-hdfc-v1",
    format_version: "1.0",
    required: ["date", "narration", "withdrawal amt.", "deposit amt."],
    distinctive: ["narration", "chq./ref.no.", "closing balance"],
    role: "bank_line",
  },
  {
    format_id: "bank-csv-icici-v1",
    format_version: "1.0",
    required: ["transaction date", "transaction remarks", "withdrawal amount"],
    distinctive: ["transaction remarks", "cheque number", "balance (inr)"],
    role: "bank_line",
  },
  {
    format_id: "orders-csv-shopify-v1",
    format_version: "1.0",
    required: ["name", "created at", "total", "financial status"],
    distinctive: ["financial status", "currency", "lineitem quantity"],
    role: "order",
  },
  {
    format_id: "cod-remittance-delhivery-v1",
    format_version: "1.0",
    required: ["remittance ref", "remittance date", "waybill", "cod amount"],
    distinctive: ["waybill", "freight charge", "cod handling fee", "rto charge"],
    role: "psp_txn",
  },
];

/** MT940 is not a CSV at all: it is recognised by its tags, not by a header. */
const MT940 = {
  format_id: "mt940-v1",
  format_version: "1.0",
  role: "bank_line" as const,
};

type StoredUpload = Upload & { rows: QuarantinedRow[] };

/**
 * The contract shape, without the quarantine rows the store keeps beside it.
 *
 * The rows reach a client only through `GET /api/uploads/{id}/quarantine`,
 * which is the rule the real API follows for the same reason: uploaded content
 * is served from one authenticated endpoint and from nowhere else.
 */
function published(upload: StoredUpload): Upload {
  const copy: Partial<StoredUpload> = { ...upload };
  delete copy.rows;
  return copy as Upload;
}

const store: StoredUpload[] = seedUploads();

/* ------------------------------------------------------------------ *
 * Reading a file
 * ------------------------------------------------------------------ */

function headerCells(text: string): string[] {
  const line = text
    .split(/\r?\n/)
    .find((row) => row.trim() !== "" && !row.startsWith("#"));
  return (line ?? "")
    .split(",")
    .map((cell) => cell.trim().replace(/^"|"$/g, "").toLowerCase());
}

/** Mirrors `core/adapters/base.py:header_confidence`: 0, or 0.7 plus 0.3×hits. */
function confidenceFor(
  cells: string[],
  layout: (typeof LAYOUTS)[number],
): number {
  if (!layout.required.every((column) => cells.includes(column))) return 0;
  const hits = layout.distinctive.filter((column) =>
    cells.includes(column),
  ).length;
  return 0.7 + 0.3 * (hits / layout.distinctive.length);
}

/** An exact rupee decimal: at most two places, optional grouping, no exponent. */
const RUPEE = /^[+-]?\d[\d,]*(?:\.\d{1,2})?$/;

const AMOUNT_HINTS = [
  "amount",
  "amt",
  "credit",
  "debit",
  "total",
  "fee",
  "tax",
  "charge",
  "balance",
];

function readRows(
  text: string,
  cells: string[],
): { records: number; quarantined: QuarantinedRow[] } {
  const lines = text.split(/\r?\n/);
  const quarantined: QuarantinedRow[] = [];
  let records = 0;
  let seenHeader = false;

  const amountColumns = cells
    .map((cell, index) => ({ cell, index }))
    .filter(({ cell }) => AMOUNT_HINTS.some((hint) => cell.includes(hint)))
    .map(({ index }) => index);

  lines.forEach((line, i) => {
    const number = i + 1;
    if (line.trim() === "" || line.startsWith("#")) return;
    if (!seenHeader) {
      seenHeader = true;
      return;
    }

    const fields = line.split(",");
    if (fields.length !== cells.length) {
      quarantined.push({
        row_number: number,
        raw: line,
        reason:
          fields.length < cells.length ? "TRUNCATED_ROW" : "EXTRA_FIELDS",
        detail: `the header has ${cells.length} columns and this row has ${fields.length}`,
      });
      return;
    }

    const bad = amountColumns.find((index) => {
      const value = (fields[index] ?? "").trim();
      return value !== "" && !RUPEE.test(value);
    });
    if (bad !== undefined) {
      quarantined.push({
        row_number: number,
        raw: line,
        reason: "BAD_DECIMAL",
        detail: `column ${JSON.stringify(cells[bad])} value ${JSON.stringify(
          fields[bad].trim(),
        )} is not an exact rupee amount`,
      });
      return;
    }

    records += 1;
  });

  return { records, quarantined };
}

async function sha256(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/* ------------------------------------------------------------------ *
 * The handlers
 * ------------------------------------------------------------------ */

export const uploadHandlers = [
  /* POST /api/uploads */
  http.post(url("/api/uploads"), async ({ request }) => {
    const form = await request.formData();
    const file = form.get("file");
    if (!(file instanceof File)) {
      return HttpResponse.json(
        { detail: "field 'file' is required" },
        { status: 422 },
      );
    }

    const bytes = await file.arrayBuffer();
    const digest = await sha256(bytes);
    await delay(220);

    const held = store.find((upload) => upload.content_sha256 === digest);
    if (held) {
      return HttpResponse.json<UploadReceipt>({
        ...published(held),
        already_ingested: true,
      });
    }

    const text = new TextDecoder("utf-8").decode(bytes);
    // Not text at all: the same NUL guard `core/adapters/base.py:decode_bytes`
    // applies before it will trust latin-1, plus the replacement character a
    // non-fatal decode leaves behind on bytes no codec could read.
    if (text.includes("\u0000") || text.includes("\uFFFD")) {
      return HttpResponse.json<UploadRefusedError>(
        {
          detail:
            "no supported encoding decoded these bytes; a spreadsheet or an archive renamed to .csv fails here rather than parsing into nonsense",
          reason: "UNDECODABLE_FILE",
          threshold: THRESHOLD,
          candidates: [],
        },
        { status: 422 },
      );
    }

    const cells = headerCells(text);
    const scored = LAYOUTS.map((layout) => ({
      layout,
      confidence: confidenceFor(cells, layout),
    })).sort((a, b) => b.confidence - a.confidence);

    const mt940 = /(^|\n):61:/.test(text) && /(^|\n):86:/.test(text);
    const best = scored[0];

    if (!mt940 && best.confidence < THRESHOLD) {
      return HttpResponse.json<UploadRefusedError>(
        {
          detail: `no adapter recognised this file's header with confidence >= ${THRESHOLD.toFixed(
            2,
          )}. Detection is by header shape, so check the first line of the file, not its name.`,
          reason: "UNRECOGNISED_FORMAT",
          threshold: THRESHOLD,
          candidates: [
            ...scored.map(({ layout, confidence }) => ({
              format_id: layout.format_id,
              confidence,
            })),
            { format_id: MT940.format_id, confidence: 0 },
          ],
        },
        { status: 422 },
      );
    }

    const chosen = mt940
      ? { format_id: MT940.format_id, format_version: MT940.format_version, role: MT940.role }
      : {
          format_id: best.layout.format_id,
          format_version: best.layout.format_version,
          role: best.layout.role,
        };

    const { records, quarantined } = mt940
      ? { records: (text.match(/(^|\n):61:/g) ?? []).length, quarantined: [] }
      : readRows(text, cells);

    const upload: StoredUpload = {
      upload_id: `upl-${digest.slice(0, 12)}`,
      filename: file.name,
      content_sha256: digest,
      byte_size: bytes.byteLength,
      format_id: chosen.format_id,
      format_version: chosen.format_version,
      confidence: mt940 ? 1 : best.confidence,
      encoding: "utf-8",
      state:
        records > 0 ? "ingested" : quarantined.length > 0 ? "quarantined" : "empty",
      record_count: records,
      quarantine_count: quarantined.length,
      skipped_rows: 0,
      order_count: chosen.role === "order" ? records : 0,
      psp_txn_count: chosen.role === "psp_txn" ? records : 0,
      bank_line_count: chosen.role === "bank_line" ? records : 0,
      uploaded_at: new Date().toISOString(),
      rows: quarantined,
    };
    store.unshift(upload);

    return HttpResponse.json<UploadReceipt>({
      ...published(upload),
      already_ingested: false,
    });
  }),

  /* GET /api/uploads */
  http.get(url("/api/uploads"), () => {
    return HttpResponse.json<Upload[]>(store.map(published));
  }),

  /* GET /api/uploads/{id} */
  http.get(url("/api/uploads/:id"), ({ params }) => {
    const found = store.find((upload) => upload.upload_id === String(params.id));
    if (!found) return notFound(`no upload with id '${params.id}'`);
    return HttpResponse.json<Upload>(published(found));
  }),

  /* GET /api/uploads/{id}/quarantine */
  http.get(url("/api/uploads/:id/quarantine"), ({ params, request }) => {
    const found = store.find((upload) => upload.upload_id === String(params.id));
    if (!found) return notFound(`no upload with id '${params.id}'`);

    const search = new URL(request.url).searchParams;
    const page = Math.max(1, Number(search.get("page") ?? 1));
    const size = Math.max(1, Math.min(200, Number(search.get("size") ?? 50)));
    const start = (page - 1) * size;

    return HttpResponse.json<PaginatedQuarantine>({
      items: found.rows.slice(start, start + size),
      total: found.rows.length,
      page,
      size,
    });
  }),
];

/** The selected uploads' record counts, for the run the console starts. */
export function uploadRecordCount(uploadIds: string[]): number {
  return store
    .filter((upload) => uploadIds.includes(upload.upload_id))
    .reduce((total, upload) => total + upload.order_count, 0);
}

export function uploadExists(uploadId: string): boolean {
  return store.some((upload) => upload.upload_id === uploadId);
}

/* ------------------------------------------------------------------ *
 * Seed
 * ------------------------------------------------------------------ */

function hex(seed: string): string {
  // A stable 64-hex string per seed. Not a real digest and never compared
  // against one: the store's own uploads are matched by id, and only a file
  // the browser actually posted gets a computed hash.
  let out = "";
  let h = 0;
  for (let i = 0; i < seed.length; i += 1) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  while (out.length < 64) {
    h = (h * 1103515245 + 12345) >>> 0;
    out += h.toString(16).padStart(8, "0");
  }
  return out.slice(0, 64);
}

function ago(minutes: number): string {
  return new Date(Date.now() - minutes * 60_000).toISOString();
}

const DAMAGED_ROWS: [number, string, QuarantineReason, string][] = [
  [
    41,
    "05/08/26,NEFT-RAZORPAY setl_00021 CREDIT,SETLX0021,05/08/26,,71153.4055,1284410.22",
    "BAD_DECIMAL",
    'column "deposit amt." value "71153.4055" is 7115340.55 paise, which is not a whole number of paise; sub-paise values are quarantined, never rounded',
  ],
  [
    42,
    "06/08/26,NEFT-RAZORPAY setl_00022",
    "TRUNCATED_ROW",
    "the header has 7 columns and this row has 2",
  ],
  [
    43,
    "07/08/26,ACH D- BILLDESK, RENT AUG,ACH0099,07/08/26,4500.00,,1279910.22",
    "EXTRA_FIELDS",
    "the header has 7 columns and this row has 8 — usually an unquoted comma inside a narration",
  ],
];

function seedUploads(): StoredUpload[] {
  return [
    {
      upload_id: "upl-8c41ae03b2d9",
      filename: "razorpay_settlement_aug2026.csv",
      content_sha256: hex("razorpay-aug"),
      byte_size: 486_112,
      format_id: "razorpay-settlement-v2",
      format_version: "2.0-per-transaction",
      confidence: 1,
      encoding: "utf-8",
      state: "ingested",
      record_count: 1_492,
      quarantine_count: 0,
      skipped_rows: 0,
      order_count: 0,
      psp_txn_count: 1_492,
      bank_line_count: 0,
      uploaded_at: ago(38),
      rows: [],
    },
    {
      upload_id: "upl-2f77bd10c4e6",
      filename: "OpTransactionHistory_Aug26.csv",
      content_sha256: hex("hdfc-aug"),
      byte_size: 74_318,
      format_id: "bank-csv-hdfc-v1",
      format_version: "1.0",
      confidence: 1,
      encoding: "latin-1",
      state: "ingested",
      record_count: 311,
      quarantine_count: DAMAGED_ROWS.length,
      skipped_rows: 2,
      order_count: 0,
      psp_txn_count: 0,
      bank_line_count: 311,
      uploaded_at: ago(31),
      rows: DAMAGED_ROWS.map(([row_number, raw, reason, detail]) => ({
        row_number,
        raw,
        reason,
        detail,
      })),
    },
    {
      upload_id: "upl-5ad3e71c9082",
      filename: "orders_export.csv",
      content_sha256: hex("shopify-aug"),
      byte_size: 211_930,
      format_id: "orders-csv-shopify-v1",
      format_version: "1.0",
      confidence: 1,
      encoding: "utf-8-sig",
      state: "ingested",
      record_count: 1_180,
      quarantine_count: 0,
      skipped_rows: 0,
      order_count: 1_180,
      psp_txn_count: 0,
      bank_line_count: 0,
      uploaded_at: ago(24),
      rows: [],
    },
    {
      // The state that is hardest to render honestly and easiest to get wrong:
      // read, understood, and empty. Not damaged.
      upload_id: "upl-b90c5d217af4",
      filename: "orders_export (3).csv",
      content_sha256: hex("shopify-empty"),
      byte_size: 1_204,
      format_id: "orders-csv-shopify-v1",
      format_version: "1.0",
      confidence: 1,
      encoding: "utf-8",
      state: "empty",
      record_count: 0,
      quarantine_count: 0,
      skipped_rows: 0,
      order_count: 0,
      psp_txn_count: 0,
      bank_line_count: 0,
      uploaded_at: ago(12),
      rows: [],
    },
  ];
}
