"""The one place the PSP transaction-type sets are named.

This set previously existed twice -- as prose in `docs/CSV_SCHEMAS.md` 3.2.1
and as a literal in `tests/test_fixture_integrity.py` -- which is exactly the
drift this module removes: a dedup key or an exception rule that re-spells the
literal can disagree with the fixture test about what a duplicate is, and only
one of them will be right.
"""

from __future__ import annotations

from typing import Final

#: `txn_type` values that name a single order. Duplicate detection is only
#: meaningful for these (CSV_SCHEMAS.md 3.2.1): settlement-level legs carry an
#: empty `order_id` by design, so two settlements charging the same amount on
#: the same day collide on the economic tuple without being duplicates at all.
ORDER_BEARING_TYPES: Final[frozenset[str]] = frozenset(
    {"payment", "refund", "chargeback"}
)

#: Levied on the settlement batch, never on one order.
SETTLEMENT_LEVEL_TYPES: Final[frozenset[str]] = frozenset({"fee", "tax", "reserve"})

#: The ONLY type that counts toward T1/T2 payment-leg cardinality (spec 7.1).
#: Deliberately narrower than ORDER_BEARING_TYPES: a refund leg does not make a
#: settlement a batch.
PAYMENT_TYPE: Final[str] = "payment"

#: Levied percentages, in basis points (CSV_SCHEMAS.md 3.3). The MDR base is
#: the settlement's OWN payment legs -- refunds, chargebacks, reserves and
#: adjustments reduce the net, not the fee base.
MDR_BPS: Final[int] = 236
GST_BPS: Final[int] = 1800
