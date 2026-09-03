/**
 * The five connection operations, and the vocabulary the console renders them in.
 *
 * Beside `lib/uploads.ts` for the same reason that module gives: everything one
 * surface needs lives together, and every request still goes through `getJSON` /
 * `postJSON` — the ONE place this app calls `fetch`.
 *
 * FOUR PROPERTIES OF THE CONTRACT SHAPE EVERY SCREEN BUILT ON THIS.
 *
 * 1. **The secret is write-only.** `ConnectionRequest` carries `password`;
 *    `Connection` carries `has_password`. There is no endpoint that returns the
 *    value — not to the owner, not to an admin. A form that pre-filled a
 *    password field would have to invent one, so the form leaves it blank and
 *    says what blank means.
 *
 * 2. **A keyless build refuses to store anything.** `POST` answers 422 naming
 *    `RECON_BLOB_KEY` rather than saving a plaintext credential. That is a
 *    configuration error with a fix, not a bug, and the screen renders it as
 *    instructions instead of as a failure.
 *
 * 3. **Test and sync are different questions.** "Your password is wrong" and
 *    "your sender filter matches nothing" both surface as zero attachments, and
 *    a merchant cannot tell them apart from that. `/test` authenticates and
 *    fetches nothing, so the two answers stay separable.
 *
 * 4. **A skipped attachment is a fact, not an omission.** `skipped_names` lists
 *    what the filter declined — the credit report it refuses to read is the
 *    reason that field exists, and hiding it would make the privacy control
 *    invisible to the person it protects.
 */
import { deleteJSON, getJSON, postJSON, type RequestOptions } from "./api";
import type { components } from "./api-types";

export type Connection = components["schemas"]["Connection"];
export type ConnectionRequest = components["schemas"]["ConnectionRequest"];
export type ConnectionSyncResult = components["schemas"]["ConnectionSyncResult"];
export type ConnectionTestResult = components["schemas"]["ConnectionTestResult"];
export type ConnectionDeleted = components["schemas"]["ConnectionDeleted"];

export const connectionsApi = {
  list(options?: RequestOptions) {
    return getJSON<Connection[]>("/api/connections", options);
  },

  save(body: ConnectionRequest, options?: RequestOptions) {
    return postJSON<Connection>("/api/connections", body, options);
  },

  test(id: string, options?: RequestOptions) {
    return postJSON<ConnectionTestResult>(
      `/api/connections/${encodeURIComponent(id)}/test`,
      {},
      options,
    );
  },

  sync(id: string, options?: RequestOptions) {
    return postJSON<ConnectionSyncResult>(
      `/api/connections/${encodeURIComponent(id)}/sync`,
      {},
      options,
    );
  },

  remove(id: string, options?: RequestOptions) {
    return deleteJSON<ConnectionDeleted>(
      `/api/connections/${encodeURIComponent(id)}`,
      options,
    );
  },
};

/**
 * What each sync status means, in the merchant's terms rather than the
 * scheduler's.
 *
 * `never` is deliberately not an error state. A connection saved a minute ago
 * has not synced and nothing is wrong with it; colouring that red would teach
 * a merchant to ignore red.
 */
export const SYNC_STATUS: Record<string, { label: string; tone: Tone }> = {
  never: { label: "Not synced yet", tone: "neutral" },
  ok: { label: "Synced", tone: "good" },
  failed: { label: "Last sync failed", tone: "bad" },
};

export type Tone = "neutral" | "good" | "bad";

/**
 * The next scheduled fetch, as a sentence.
 *
 * The fetcher wakes daily and syncs a connection whose last success is 30 days
 * old, so "next month" is a range and not a date. Printing a precise timestamp
 * would be more confident than the scheduler is.
 */
export function nextSyncHint(connection: Connection): string {
  if (!connection.last_sync_at) {
    return "Will fetch on the next daily check.";
  }
  const last = new Date(connection.last_sync_at).getTime();
  const days = Math.floor((Date.now() - last) / 86_400_000);
  const remaining = 30 - days;
  if (remaining <= 0) return "Due now — will fetch on the next daily check.";
  if (remaining === 1) return "Fetches again in about a day.";
  return `Fetches again in about ${remaining} days.`;
}
