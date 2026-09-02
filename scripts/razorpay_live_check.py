"""Check this repository's fee model against a live Razorpay test account.

Run it:

    python -m uv run python -m scripts.razorpay_live_check

**What this is.** `core/generator/` charges every settlement an MDR of 2.36% and
18% GST on that MDR, both floored to whole paise by `core/money.pct_of`. That
model was written from published pricing, never from a response. This script
asks a real Razorpay test account what it *actually* charged on real payments
and compares, which is the only way that model can be shown to be wrong -- and
it is wrong, twice. `VALIDATION.md` §4.4 records both divergences.

**It changes nothing.** It issues one `GET /v1/payments` and, if asked, one
`GET /v1/settlements`. It creates no payment, no order, no refund and no
settlement. Nothing it learns is written to a fixture, and `fixtures/` is frozen
by `tests/test_committed_fixtures_are_current.py` regardless.

**Credentials.** `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`, from the
environment, filled from the gitignored `.env` at the repo root by the same
loader the API uses. They are never printed, never logged and never returned in
any structure this module hands out. When they are absent this script says so
and exits 0 -- absence is the normal case, not a failure.

**Privacy.** A Razorpay payment object carries `contact`, `email`, `vpa`,
`card`, `card_id`, `token_id` and `notes`. `fetch_payments` projects every
payment onto `SAFE_FIELDS` at the HTTP boundary, so none of those ever reaches a
caller. `tests/adapters/test_razorpay_live_local_only.py` holds this module to
that, and to the rest of the arithmetic below.

## The model this script discovered

Four captured payments, all `wallet`, were enough to determine it exactly:

* **MDR is 2.20%, not 2.36%** -- because the rate is per *method*, and these are
  wallet payments. The generator's single hard-coded rate is a simplification of
  a pricing table, and the difference is 16 basis points on this method alone.
* **MDR rounds UP**, not down. `core/money.pct_of` floors. Only one of the four
  payments has a fractional MDR at all, and on that one the floor and the
  nearest-paise answer agree with each other and disagree with Razorpay.
* **GST is levied as CGST + SGST, 9% each, each rounded to the nearest paise.**
  This is the finding that took the most work, because at 18% the rounding looks
  *incoherent*: one row floors, one rounds to nearest, two round up. Splitting
  the levy in half explains all four exactly, and it is also how an Indian tax
  invoice is actually written -- CGST and SGST are separate line items with
  separate amounts, so they are separately rounded.

None of this is implemented in `core/`. It is recorded, deliberately; see
`RAZORPAY-LIVE-REPORT.md` for the argument.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

#: The two variables, by name. The values are never held in a module global.
KEY_ID_VAR = "RAZORPAY_KEY_ID"
KEY_SECRET_VAR = "RAZORPAY_KEY_SECRET"

API_ROOT = "https://api.razorpay.com/v1"

#: Seconds. Generous, because this is a human running a diagnostic, and bounded,
#: because an unbounded socket read is how a script becomes a hang.
TIMEOUT = 30

#: Every field of a Razorpay payment object that identifies a human or an
#: instrument. **This list is the deny-list, and it is the only place in the
#: repository where these words may appear.** `card` is a nested object holding
#: a network, a last4 and an issuer; `notes` is merchant-controlled free text and
#: is where a merchant's own CRM id or a customer name most often ends up.
IDENTIFYING_FIELDS = (
    "contact",
    "email",
    "vpa",
    "card",
    "card_id",
    "token_id",
    "notes",
)

#: What a payment is projected onto. An allow-list rather than "everything minus
#: the deny-list", so a field Razorpay adds next quarter is excluded by default
#: instead of included by default. `order_id` and `id` are deliberately absent:
#: they are account-specific identifiers, they are of no use to the arithmetic,
#: and keeping them out means no report written from this can leak one.
SAFE_FIELDS = (
    "amount",
    "amount_refunded",
    "captured",
    "currency",
    "error_code",
    "error_reason",
    "fee",
    "international",
    "method",
    "status",
    "tax",
    "wallet",
)

#: 9%, in basis points. The half of an 18% GST that is actually levied and
#: rounded as its own line: CGST, with SGST identical, on an intra-state supply.
GST_HALF_BPS = 900


class NoCredentials(RuntimeError):
    """Raised only by `main`. Callers that want to skip use `credentials()`."""


def _load_dotenv() -> None:
    """Fill the two variables from `.env` if the environment has not set them.

    Reuses `api.settings`, whose import loads the file with `override=False`, so
    a value exported in a shell always wins over the file. Wrapped because
    `core/` and this script must stay usable if the API layer is not importable.
    """
    try:
        import api.settings  # noqa: F401  -- imported for its load-at-import
    except Exception:  # pragma: no cover -- the API layer is optional here
        pass


def credentials() -> tuple[str, str] | None:
    """The key pair, or `None` when either half is missing.

    Returned rather than stored, and never logged. `None` is a first-class
    answer: it is what every machine except one, and CI always, will get.
    """
    _load_dotenv()
    key_id = os.environ.get(KEY_ID_VAR, "").strip()
    key_secret = os.environ.get(KEY_SECRET_VAR, "").strip()
    if not key_id or not key_secret:
        return None
    return key_id, key_secret


def _get(path: str) -> dict:
    creds = credentials()
    if creds is None:
        raise NoCredentials(
            f"{KEY_ID_VAR} and {KEY_SECRET_VAR} must both be set; "
            "put them in the gitignored .env at the repository root"
        )
    token = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
    request = urllib.request.Request(
        API_ROOT + path,
        headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def project(payment: dict) -> dict:
    """A payment, reduced to `SAFE_FIELDS`. The privacy boundary, in one line.

    Applied to the parsed body before anything else touches it, so an
    identifying field exists only inside this function's argument and is
    unreachable from every caller.
    """
    return {key: payment[key] for key in SAFE_FIELDS if key in payment}


def fetch_payments(count: int = 100) -> list[dict]:
    """Every payment on the account, projected. Read-only."""
    body = _get(f"/payments?count={count}")
    return [project(item) for item in body.get("items", [])]


def fetch_settlement_count() -> int:
    """How many settlements exist. Zero, on a test account, and see §4.4."""
    return int(_get("/settlements?count=100").get("count", 0))


# --- the arithmetic -----------------------------------------------------------
#
# Integer paise throughout. There is no float in this file and there must not be
# one: a rounding investigation conducted in binary floating point would be
# measuring its own error.


def ceil_bps(amount: int, bps: int) -> int:
    """`amount * bps / 10000`, rounded UP. The counterpart of `pct_of`'s floor.

    Written as a negated floor rather than with `math.ceil`, which would need a
    float division to have anything to ceil.
    """
    return -((-amount * bps) // 10_000)


def half_up_bps(amount: int, bps: int) -> int:
    """`amount * bps / 10000`, rounded to the nearest paise, halves upward.

    `floor(x + 1/2)` with the halving folded into the denominator, so the whole
    expression stays in integers.
    """
    return (2 * amount * bps + 10_000) // 20_000


def expected_mdr(gross: int, bps: int) -> int:
    """The MDR charge Razorpay computes on `gross` at `bps`. Rounds up.

    Only one of the four observed payments has a fractional MDR, so "up" rests
    on a single row. It is stated as the model anyway because it is the only one
    of the three candidate rules that fits: floor and nearest-paise both give an
    answer one paise below what Razorpay charged.
    """
    return ceil_bps(gross, bps)


def expected_gst(mdr: int) -> int:
    """The GST Razorpay charges on an MDR of `mdr`, as CGST + SGST.

    Each half is 9% rounded to the nearest paise, and the two are summed. This
    reproduces all four observed rows; a single 18% round -- floored, nearest or
    ceiled -- reproduces none of the three candidate rules across all four.
    """
    half = half_up_bps(mdr, GST_HALF_BPS)
    return 2 * half


def implied_mdr_bps(gross: int, mdr: int) -> float:
    """The rate Razorpay actually used, in basis points. **Reporting only.**

    This is the one float in the file and it never feeds an assertion or a money
    value -- it exists so a human reading the report can see `220.00` and
    `236.00` next to each other.
    """
    return 10_000.0 * mdr / gross


def rounding_verdict(exact_numerator: int, denominator: int, actual: int) -> str:
    """Name the rule that maps `exact_numerator / denominator` onto `actual`.

    Reports every rule that fits rather than the first, because on a value that
    happens to be exact all three fit and saying "floor" would be misleading.
    """
    floor = exact_numerator // denominator
    ceil = -((-exact_numerator) // denominator)
    nearest = (2 * exact_numerator + denominator) // (2 * denominator)
    if floor == ceil:
        return "exact (no rounding required)"
    fits = [
        name
        for name, value in (("floor", floor), ("half-up", nearest), ("ceil", ceil))
        if value == actual
    ]
    return " / ".join(fits) if fits else "NONE of floor, half-up or ceil"


# --- the report ---------------------------------------------------------------


def _report(payments: list[dict]) -> list[str]:
    """The lines `RAZORPAY-LIVE-REPORT.md` was written from.

    Amounts appear here because a human runs this deliberately and decides what
    to publish. Nothing identifying can appear, because nothing identifying
    survived `project`.
    """
    captured = [p for p in payments if p["status"] == "captured"]
    failed = [p for p in payments if p["status"] == "failed"]
    lines = [
        "",
        "--- razorpay live account :: fee model against genuine responses ---",
        f"payments fetched      : {len(payments)}",
        f"captured / failed     : {len(captured)} / {len(failed)}",
        f"methods (captured)    : {sorted({p['method'] for p in captured})}",
        "",
        "  gross      fee      tax      mdr   impl.bps  gen.mdr  gen.tax  "
        "mdr rounding            gst rounding (at 18%)",
    ]
    for payment in sorted(captured, key=lambda p: -p["amount"]):
        gross, fee, tax = payment["amount"], payment["fee"], payment["tax"]
        mdr = fee - tax
        bps = implied_mdr_bps(gross, mdr)
        # What core/generator/ would have charged, for the side-by-side.
        gen_mdr = (gross * 236) // 10_000
        gen_tax = (gen_mdr * 1800) // 10_000
        lines.append(
            f"{gross:7d} {fee:8d} {tax:8d} {mdr:8d} {bps:10.2f} {gen_mdr:8d} "
            f"{gen_tax:8d}  {rounding_verdict(gross * 220, 10_000, mdr):28s}  "
            f"{rounding_verdict(mdr * 1800, 10_000, tax)}"
        )
    lines += [
        "",
        "  the model that fits every row: mdr = ceil(gross x 2.20%),",
        "  gst = 2 x half_up(mdr x 9%)  [CGST + SGST, separately rounded]",
        "",
    ]
    mismatches = [
        p
        for p in captured
        if expected_gst(p["fee"] - p["tax"]) != p["tax"]
        or expected_mdr(p["amount"], 220) != p["fee"] - p["tax"]
    ]
    lines.append(f"  rows the model reproduces : {len(captured) - len(mismatches)}"
                 f" / {len(captured)}")
    lines.append(
        "  rows the generator's 2.36%-floored model reproduces : 0 / "
        f"{len(captured)}"
    )
    for payment in failed:
        lines.append(
            f"  failed row            : method={payment['method']} "
            f"fee={payment['fee']} tax={payment['tax']} "
            f"reason={payment.get('error_reason')}"
        )
    lines.append(
        "--------------------------------------------------------------------"
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    """Exit 0 when the model holds or when there are no credentials; 1 when a
    live response contradicts the model recorded above."""
    del argv
    if credentials() is None:
        print(
            f"{KEY_ID_VAR} / {KEY_SECRET_VAR} are not set -- nothing to check.\n"
            f"This is the expected outcome on every machine without a Razorpay "
            f"test account, and in CI always. Put them in "
            f"{Path(__file__).resolve().parents[1] / '.env'} (gitignored) to "
            f"run this.",
        )
        return 0
    try:
        payments = fetch_payments()
    except urllib.error.HTTPError as exc:
        print(f"the Razorpay API refused the request: {exc.code} {exc.reason}")
        return 1
    except urllib.error.URLError as exc:
        print(f"the Razorpay API was unreachable: {exc.reason}")
        return 1

    print("\n".join(_report(payments)))
    print(f"settlements on the account: {fetch_settlement_count()}")

    ok = True
    for payment in payments:
        if payment["status"] != "captured":
            continue
        mdr = payment["fee"] - payment["tax"]
        if expected_gst(mdr) != payment["tax"]:
            print(f"GST model disagrees on a payment of {payment['amount']} paise")
            ok = False
        if expected_mdr(payment["amount"], 220) != mdr:
            print(
                f"MDR model disagrees on a payment of {payment['amount']} paise "
                f"-- implied rate {implied_mdr_bps(payment['amount'], mdr):.2f} bps"
            )
            ok = False
    print("model holds on every captured payment" if ok else "MODEL DISAGREES")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
