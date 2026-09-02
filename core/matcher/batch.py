"""Batch reconstruction -- the arithmetic core of the matcher.

A bank credit is always net of fees, so no tier can match on a raw amount:
every tier below T0 has to rebuild a settlement from its legs. This module is
that rebuild, and nothing else. It is a pure function of its argument list with
no matcher state, because Lane C's verifier imports `reconstruct` for its own
arithmetic check and that import has to stay honest.

`psp.csv` amounts are signed from the merchant's point of view (payments
positive; fees, tax, refunds, chargebacks and reserves negative), so the net is
a plain sum. The itemised breakdown is for reporting only, and the `assert`
below pins it to that sum: a sign-convention bug must crash loudly rather than
silently produce a wrong match rate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from core.canonicalize.txn_types import GST_BPS, MDR_BPS, PAYMENT_TYPE
from core.models import PSPTransaction
from core.money import Money, pct_of

CREDIT_TYPES = ("payment",)
DEBIT_TYPES = {
    "fee": "fees",
    "tax": "tax",
    "refund": "refunds",
    "chargeback": "holds",
    "reserve": "holds",
}


@dataclass(frozen=True)
class BatchTotals:
    gross: Money
    fees: Money
    tax: Money
    refunds: Money
    holds: Money
    net: Money


def reconstruct(txns: Sequence[PSPTransaction]) -> BatchTotals:
    """Rebuild a settlement's totals from its legs.

    `gross` folds `adjustment` legs in alongside payments, because an
    adjustment carries its own sign and belongs on the credit side of the
    report. That makes `gross` a REPORTING figure, not the MDR fee base -- use
    `payment_gross` for anything that re-derives a fee.
    """
    buckets = {"gross": 0, "fees": 0, "tax": 0, "refunds": 0, "holds": 0}
    for t in txns:
        if t.txn_type in CREDIT_TYPES:
            buckets["gross"] += t.amount
        elif t.txn_type in DEBIT_TYPES:
            buckets[DEBIT_TYPES[t.txn_type]] += -t.amount
        elif t.txn_type == "adjustment":
            # adjustments carry their own sign; fold into gross
            buckets["gross"] += t.amount
    net = (
        buckets["gross"]
        - buckets["fees"]
        - buckets["tax"]
        - buckets["refunds"]
        - buckets["holds"]
    )
    assert net == sum(t.amount for t in txns), "breakdown disagrees with signed sum"
    return BatchTotals(**buckets, net=net)


def payment_gross(txns: Sequence[PSPTransaction]) -> Money:
    """The MDR fee base: the settlement's OWN `payment` legs, and nothing else.

    Refunds, chargebacks, reserves and adjustments netted into a settlement
    reduce the net; they do not reduce the fee, because real MDR is not
    returned when a payment is refunded. Deriving the base from the settlement
    net, or from gross-minus-refunds, is wrong on exactly the settlements that
    carry a deduction (CSV_SCHEMAS.md 3.3).
    """
    return sum(t.amount for t in txns if t.txn_type == PAYMENT_TYPE)


def expected_fee_and_tax(txns: Sequence[PSPTransaction]) -> tuple[Money, Money]:
    """What this settlement's fee and tax legs SHOULD be, as positive paise.

    Used for exception explanations and mismatch diagnostics -- never to
    replace the fee legs, which are present as rows and are simply summed.
    GST is charged on the MDR, never on the gross.
    """
    fee = pct_of(payment_gross(txns), MDR_BPS)
    return fee, pct_of(fee, GST_BPS)


def payment_leg_count(txns: Sequence[PSPTransaction]) -> int:
    """T1/T2 cardinality: `payment` legs alone.

    `fee`, `tax`, `refund`, `chargeback`, `reserve` and `adjustment` legs do
    not count (spec 7.1). A settlement with one payment, one fee and one tax
    leg settles one order: the deduction legs are the arithmetic, not the
    batch.

    This count LABELS a match that is already established as unique. It must
    never filter a candidate pool -- see core/matcher/tiers.py.
    """
    return sum(1 for t in txns if t.txn_type == PAYMENT_TYPE)
