"""Task A.2 -- clean batch generation and the fee/GST arithmetic.

The three tests from the plan, plus the ones that pin the cross-lane contracts
a wrong generator would break silently:

* the MDR base is the settlement's own `payment` legs only (CSV_SCHEMAS 3.3);
* GST is charged on the fee, never on the gross;
* settlement-level legs carry no `order_id` (CSV_SCHEMAS 3.2.1 -- breaking this
  hands Lane B two false DUPLICATE_PSP_TXN exceptions per settlement);
* every amount is `int` paise.
"""

from datetime import date, datetime

from core.generator.batches import (
    GST_BPS,
    MDR_BPS,
    generate_clean_batch,
    payment_gross,
    true_net,
)
from core.generator.rng import SeededRng
from core.money import pct_of


def test_bank_credit_equals_gross_minus_fee_minus_gst():
    b = generate_clean_batch(SeededRng(42), "setl_A1", order_count=63)
    gross = sum(o.gross_amount for o in b.orders)
    fee = sum(-t.amount for t in b.psp_txns if t.txn_type == "fee")
    tax = sum(-t.amount for t in b.psp_txns if t.txn_type == "tax")
    assert b.bank_line.credit == gross - fee - tax


def test_fee_and_gst_use_integer_basis_points():
    b = generate_clean_batch(SeededRng(42), "setl_A1", order_count=10)
    gross = sum(o.gross_amount for o in b.orders)
    fee = sum(-t.amount for t in b.psp_txns if t.txn_type == "fee")
    tax = sum(-t.amount for t in b.psp_txns if t.txn_type == "tax")
    assert fee == pct_of(gross, MDR_BPS)
    assert tax == pct_of(fee, GST_BPS)


def test_linkage_lists_every_order_and_txn():
    b = generate_clean_batch(SeededRng(42), "setl_A1", order_count=7)
    assert set(b.linkage.order_ids) == {o.order_id for o in b.orders}
    assert set(b.linkage.psp_txn_ids) == {t.txn_id for t in b.psp_txns}


def test_gst_is_charged_on_the_fee_not_on_the_gross():
    """The classic error. pct_of(gross, 1800) is roughly 7.6x the right answer."""
    b = generate_clean_batch(SeededRng(9), "setl_G1", order_count=5)
    gross = payment_gross(b)
    tax = sum(-t.amount for t in b.psp_txns if t.txn_type == "tax")
    assert tax != pct_of(gross, GST_BPS)
    assert tax == pct_of(pct_of(gross, MDR_BPS), GST_BPS)


def test_mdr_base_is_the_payment_legs_only():
    """A deduction netted into the settlement must not shrink the fee base.

    CSV_SCHEMAS 3.3: refunds, chargebacks, reserves and adjustments reduce the
    net, never the fee. `payment_gross` is the one function that answers "what
    is the base", so it is the one place that rule can be got wrong.
    """
    b = generate_clean_batch(SeededRng(11), "setl_H1", order_count=4)
    base_before = payment_gross(b)
    net_before = true_net(b)

    b.psp_txns.append(
        b.psp_txns[0].model_copy(
            update={
                "txn_id": "cb_999999",
                "txn_type": "chargeback",
                "amount": -50_000,
                "order_id": "ORD-000999",
            }
        )
    )

    assert payment_gross(b) == base_before
    assert true_net(b) == net_before - 50_000


def test_settlement_level_legs_carry_no_order_id():
    b = generate_clean_batch(SeededRng(3), "setl_I1", order_count=6)
    for t in b.psp_txns:
        if t.txn_type in ("fee", "tax", "reserve"):
            assert t.order_id is None


def test_sign_convention_makes_the_net_a_plain_sum():
    b = generate_clean_batch(SeededRng(4), "setl_J1", order_count=6)
    assert all(t.amount > 0 for t in b.psp_txns if t.txn_type == "payment")
    assert all(t.amount < 0 for t in b.psp_txns if t.txn_type in ("fee", "tax"))
    assert sum(t.amount for t in b.psp_txns) == b.bank_line.credit


def test_every_amount_is_integer_paise():
    b = generate_clean_batch(SeededRng(5), "setl_L1", order_count=6)
    for o in b.orders:
        assert type(o.gross_amount) is int
    for t in b.psp_txns:
        assert type(t.amount) is int
    assert type(b.bank_line.credit) is int
    assert b.bank_line.debit is None


def test_a_one_order_batch_has_exactly_one_payment_leg():
    """Cardinality is what separates T1 from T2 (spec 7.1)."""
    b = generate_clean_batch(SeededRng(6), "setl_N1", order_count=1)
    assert len([t for t in b.psp_txns if t.txn_type == "payment"]) == 1


def test_the_same_seed_rebuilds_the_same_batch():
    a = generate_clean_batch(SeededRng(42), "setl_A1", 8, settled_at=date(2026, 3, 4))
    b = generate_clean_batch(SeededRng(42), "setl_A1", 8, settled_at=date(2026, 3, 4))
    assert [o.model_dump() for o in a.orders] == [o.model_dump() for o in b.orders]
    assert [t.model_dump() for t in a.psp_txns] == [t.model_dump() for t in b.psp_txns]
    assert a.bank_line.model_dump() == b.bank_line.model_dump()


def test_settled_at_is_within_the_two_day_window_of_the_bank_line():
    """T1/T2 both require |settled_at - txn_date| <= 2 days (spec 7)."""
    for i in range(20):
        b = generate_clean_batch(SeededRng(i), f"setl_{i:04X}", 3)
        assert abs((b.bank_line.txn_date - b.settled_at).days) <= 2
        for t in b.psp_txns:
            assert t.settled_at == b.settled_at
            assert t.settlement_id == b.settlement_id
            assert isinstance(t.captured_at, datetime)
