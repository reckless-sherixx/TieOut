"""Synthetic pool builders for the tier tests.

Everything here is hand-built rather than read from `fixtures/tiny/`, for two
reasons:

* **T1 fires on nothing in `fixtures/tiny/`** and that is the correct result
  (LANE-B-matcher.md 8). Its only two single-payment-leg settlements are
  `setl_D4` (rounding break, so it lands in T3) and `setl_M2` (half the
  ambiguity trap, so its bank line is an exception). T1 therefore has to be
  tested on a synthetic settlement or not at all.
* A tier test that reads the shipped fixture cannot construct the *negative*
  cases -- a 150-paise break, a contested candidate, a settlement outside the
  date window -- because the fixture is frozen and deliberately does not
  contain them.

Fee and tax legs are computed with the frozen rounding rule (`pct_of`) off the
settlement's OWN payment legs, so a builder can never accidentally encode a fee
base that the rest of the project would call wrong.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from core.canonicalize.txn_types import GST_BPS, MDR_BPS
from core.matcher.pool import CandidatePool
from core.models import BankLine, Order, PSPTransaction
from core.money import pct_of

SETTLED_AT = "2026-07-24"
CAPTURED_AT = "2026-07-22T11:05:47"


def make_order(
    order_id: str,
    gross_amount: int,
    *,
    order_date: str = "2026-07-22",
    customer_ref: str = "CUST-00001",
    status: str = "paid",
) -> Order:
    return Order(
        order_id=order_id,
        order_date=order_date,
        customer_ref=customer_ref,
        gross_amount=gross_amount,
        currency="INR",
        status=status,
    )


def make_txn(
    txn_id: str,
    txn_type: str,
    amount: int,
    *,
    order_id: str | None = None,
    settlement_id: str | None = None,
    settled_at: str | None = SETTLED_AT,
    captured_at: str = CAPTURED_AT,
) -> PSPTransaction:
    return PSPTransaction(
        txn_id=txn_id,
        txn_type=txn_type,
        order_id=order_id,
        captured_at=captured_at,
        amount=amount,
        settlement_id=settlement_id,
        settled_at=settled_at if settlement_id else None,
    )


def make_bank_line(
    line_id: str,
    credit: int | None,
    *,
    txn_date: str = SETTLED_AT,
    narration: str = "NEFT CR   PAYOUT",
    utr: str | None = None,
    debit: int | None = None,
    balance: int = 0,
) -> BankLine:
    return BankLine(
        line_id=line_id,
        txn_date=txn_date,
        narration=narration,
        credit=credit,
        debit=debit,
        balance=balance,
        utr=utr,
    )


def build_settlement(
    settlement_id: str,
    payment_amounts: Sequence[int],
    *,
    settled_at: str = SETTLED_AT,
    order_ids: Sequence[str | None] | None = None,
    extra_legs: Sequence[PSPTransaction] = (),
    fee_override: int | None = None,
) -> list[PSPTransaction]:
    """One settlement's legs: its payments, then its fee and tax legs.

    `fee_override` bends the fee leg by a few paise so a break of a chosen size
    can be constructed. It is the only way to build a rounding break without
    touching the frozen fixture, and it deliberately does NOT change the fee
    base: the tax leg still follows from the real MDR.
    """
    ids = list(order_ids) if order_ids is not None else [
        f"ORD-{settlement_id}-{i}" for i in range(len(payment_amounts))
    ]
    legs = [
        make_txn(
            f"pay_{settlement_id}_{i}",
            "payment",
            amount,
            order_id=ids[i],
            settlement_id=settlement_id,
            settled_at=settled_at,
        )
        for i, amount in enumerate(payment_amounts)
    ]
    legs.extend(extra_legs)
    gross = sum(payment_amounts)
    fee = pct_of(gross, MDR_BPS) if fee_override is None else fee_override
    tax = pct_of(pct_of(gross, MDR_BPS), GST_BPS)
    legs.append(
        make_txn(
            f"fee_{settlement_id}",
            "fee",
            -fee,
            settlement_id=settlement_id,
            settled_at=settled_at,
        )
    )
    legs.append(
        make_txn(
            f"tax_{settlement_id}",
            "tax",
            -tax,
            settlement_id=settlement_id,
            settled_at=settled_at,
        )
    )
    return legs


def net_of(legs: Sequence[PSPTransaction]) -> int:
    return sum(leg.amount for leg in legs)


def make_pool(
    legs: Sequence[PSPTransaction],
    bank_lines: Sequence[BankLine],
    orders: Sequence[Order] = (),
) -> CandidatePool:
    return CandidatePool(
        orders=list(orders), psp_txns=list(legs), bank_lines=list(bank_lines)
    )


# --- pools named by the plan's tier tests ------------------------------------


@pytest.fixture
def pool_with_setl_in_narration() -> CandidatePool:
    """A reference hit AND the arithmetic closing exactly: the T0 case."""
    legs = build_settlement("setl_X1", [2_100_000, 675_000])
    return make_pool(
        legs,
        [
            make_bank_line(
                "BL-9001",
                net_of(legs),
                narration="NEFT CR RAZORPAY SOFTWARE PVT LTD SETL setl_X1",
                utr="HDFCN26072400001",
            )
        ],
    )


@pytest.fixture
def pool_with_setl_ref_and_50p_break() -> CandidatePool:
    """`setl_D4`'s shape: the narration names the settlement and the
    reconstruction lands 50 paise high. T0 must decline so T3 can claim it."""
    legs = build_settlement("setl_X2", [3_000_000])
    return make_pool(
        legs,
        [
            make_bank_line(
                "BL-9002",
                net_of(legs) - 50,
                narration="NEFT CR RAZORPAY SOFTWARE PVT LTD SETL setl_X2",
                utr="HDFCN26072400002",
            )
        ],
    )


@pytest.fixture
def pool_with_one_payment_leg() -> CandidatePool:
    """Exactly one `payment` leg -- fee and tax legs do not count -- so a
    unique exact-net candidate is labelled T1, not T2."""
    legs = build_settlement("setl_X3", [2_500_000])
    return make_pool(legs, [make_bank_line("BL-9003", net_of(legs))])


@pytest.fixture
def pool_with_netted_batch() -> CandidatePool:
    """Two payment legs plus a netted refund and a chargeback hold: T2."""
    refund = make_txn(
        "rfnd_X4",
        "refund",
        -890_000,
        order_id="ORD-OLD",
        settlement_id="setl_X4",
    )
    hold = make_txn(
        "cb_X4",
        "chargeback",
        -50_000,
        order_id="ORD-GONE",
        settlement_id="setl_X4",
    )
    legs = build_settlement(
        "setl_X4", [2_100_000, 675_000], extra_legs=[refund, hold]
    )
    return make_pool(
        legs, [make_bank_line("BL-9004", net_of(legs), narration="RZPX*ACME  RET PL")]
    )


@pytest.fixture
def pool_with_two_candidates_of_different_cardinality() -> CandidatePool:
    """**One** bank line, two settlements that close it, differing in
    payment-leg count.

    The single subject is the whole point. A tier that filtered candidates BY
    cardinality would show T1 a pool of exactly one and match it, and this is
    the only shape that catches that: with two subjects competing for the same
    filtered candidate the contest rule declines them anyway, so a two-line
    version passes on the broken implementation and proves nothing.
    """
    two_legs = build_settlement("setl_P1", [1_600_000, 900_000])
    one_leg = build_settlement("setl_P2", [2_500_000])
    assert net_of(two_legs) == net_of(one_leg)
    return make_pool(
        [*two_legs, *one_leg], [make_bank_line("BL-9005", net_of(two_legs))]
    )


@pytest.fixture
def pool_with_the_full_trap_shape() -> CandidatePool:
    """The fixture's trap as it actually stands: two indistinguishable bank
    lines and two settlements of different cardinality that both close them."""
    two_legs = build_settlement("setl_P1", [1_600_000, 900_000])
    one_leg = build_settlement("setl_P2", [2_500_000])
    credit = net_of(two_legs)
    return make_pool(
        [*two_legs, *one_leg],
        [make_bank_line("BL-9005", credit), make_bank_line("BL-9006", credit)],
    )


@pytest.fixture
def pool_with_50p_break() -> CandidatePool:
    """No reference at all, so only T3 can reach it. Residual is `net - credit`
    == +50: the bank credited 50 paise less than the reconstruction."""
    legs = build_settlement("setl_X5", [3_000_000])
    return make_pool(legs, [make_bank_line("BL-9007", net_of(legs) - 50)])


@pytest.fixture
def pool_with_150p_break() -> CandidatePool:
    """150 paise is outside T3's +/-100 tolerance: an exception, not a match."""
    legs = build_settlement("setl_X6", [3_000_000])
    return make_pool(legs, [make_bank_line("BL-9008", net_of(legs) - 150)])


@pytest.fixture
def pool_with_two_identical_candidates() -> CandidatePool:
    """Two same-cardinality settlements closing the same credit.

    This is the WEAK form of the ambiguity test: it passes even on an
    implementation that partitions the pool by cardinality. The strong form is
    `pool_with_two_candidates_of_different_cardinality`.
    """
    first = build_settlement("setl_Q1", [1_600_000, 900_000])
    second = build_settlement("setl_Q2", [1_500_000, 1_000_000])
    assert net_of(first) == net_of(second)
    return make_pool(
        [*first, *second], [make_bank_line("BL-9009", net_of(first))]
    )
