/**
 * The one timestamp formatter in the app, for the same reason there is one
 * money formatter: two formatters is two answers to the same question.
 *
 * The only timestamp the UI renders from the contract is
 * `RunSummary.created_at` — an RFC-3339 instant stamped by the API when the
 * run row is created. The client never invents one, and never substitutes its
 * own clock when the field is absent, because the field is never absent.
 *
 * Rendered in the viewer's own timezone, which is the truthful reading of an
 * instant. `hour12: false` keeps the column narrow and monotonic, so a run
 * history reads as a sequence rather than a wall of am/pm.
 */
const TIMESTAMP = new Intl.DateTimeFormat("en-IN", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export function formatTimestamp(iso: string): string {
  const at = new Date(iso);
  // A malformed instant is a contract violation, not something to paper over
  // with `Date.now()`. Render it as unknown and let it be visible.
  if (Number.isNaN(at.getTime())) return "—";
  return TIMESTAMP.format(at);
}

/** The full instant, for a `title` tooltip on a truncated cell. */
export function fullTimestamp(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toISOString();
}
