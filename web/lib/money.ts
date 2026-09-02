/**
 * Money is ALWAYS an integer number of paise on the wire. Never a float,
 * never rupees. This is the one and only money formatter in the app —
 * every component renders money through `formatINR`. Two formatters is
 * two roundings (LANE-E-web.md §6).
 *
 * Sign, then divmod(abs(value), 100), then
 * `${sign}₹${rupees.toLocaleString("en-IN")}.${paise.padStart(2,"0")}`.
 *   formatINR(4_655_654)   === "₹46,556.54"
 *   formatINR(-50)         === "-₹0.50"
 *   formatINR(69_619_301)  === "₹6,96,193.01"
 *
 * GROUPING IS INDIAN — lakh/crore, `₹6,96,193.01`, not `₹696,193.01`. That is
 * what `toLocaleString("en-IN")` produces and it is a ruling, not an
 * accident: this is an Indian payments product and Indian grouping is what a
 * finance reader here expects. `core/money.py`'s `fmt_inr` currently uses
 * Western grouping; it is a CLI/debug helper being reconciled separately, so
 * this formatter is deliberately NOT a mirror of it. Do not "fix" the two
 * into agreement by changing this one.
 */
export function formatINR(paise: number): string {
  const sign = paise < 0 ? "-" : "";
  const abs = Math.abs(paise);
  const rupees = Math.floor(abs / 100);
  const remainderPaise = abs % 100;
  return `${sign}₹${rupees.toLocaleString("en-IN")}.${String(remainderPaise).padStart(2, "0")}`;
}

/**
 * Rates on the wire are 0.0-1.0 floats, never pre-multiplied percentages.
 * `0.942` renders as `94.2%`.
 */
export function formatRate(rate: number, digits = 1): string {
  return `${(rate * 100).toFixed(digits)}%`;
}
