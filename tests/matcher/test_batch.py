"""Batch reconstruction -- the single most important function in the project.

`psp.csv` amounts are SIGNED from the merchant's point of view, so a
settlement's net is a plain sum: no subtraction logic, no per-type branching.
The itemised breakdown exists for reporting, and it must never disagree with
that sum -- a sign-convention bug has to crash loudly rather than quietly
produce a wrong match rate.
"""

from collections import defaultdict
from pathlib import Path

from core.ingest.reader import read_psp
from core.matcher.batch import (
    expected_fee_and_tax,
    payment_gross,
    payment_leg_count,
    reconstruct,
)
from core.models import PSPTransaction

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "tiny"


def _t(txn_id, txn_type, amount, order_id=None):
    return PSPTransaction(
        txn_id=txn_id,
        txn_type=txn_type,
        order_id=order_id,
        captured_at="2026-08-01T10:00:00",
        amount=amount,
        settlement_id="setl_A",
        settled_at="2026-08-03",
    )


def _by_settlement() -> dict[str, list[PSPTransaction]]:
    groups: dict[str, list[PSPTransaction]] = defaultdict(list)
    for txn in read_psp(FIX / "psp.csv"):
        if txn.settlement_id:
            groups[txn.settlement_id].append(txn)
    return dict(groups)


def test_reconstructs_net_from_signed_amounts():
    txns = [
        _t("pay_1", "payment", 4_932_000),
        _t("fee_1", "fee", -116_395),
        _t("tax_1", "tax", -20_951),
        _t("rfnd_1", "refund", -89_000),
        _t("cb_1", "chargeback", -50_000),
    ]
    tot = reconstruct(txns)
    assert tot.gross == 4_932_000
    assert tot.fees == 116_395
    assert tot.tax == 20_951
    assert tot.refunds == 89_000
    assert tot.holds == 50_000
    assert tot.net == 4_655_654


def test_net_equals_plain_signed_sum():
    """Internal consistency: the itemised breakdown must agree with the naive sum."""
    txns = [
        _t("pay_1", "payment", 100_000),
        _t("fee_1", "fee", -2_360),
        _t("tax_1", "tax", -424),
    ]
    assert reconstruct(txns).net == sum(t.amount for t in txns)


def test_empty_batch_is_zero():
    assert reconstruct([]).net == 0


def test_reserve_legs_land_in_holds():
    txns = [_t("pay_1", "payment", 100_000), _t("rsv_1", "reserve", -10_000)]
    tot = reconstruct(txns)
    assert tot.holds == 10_000
    assert tot.net == 90_000 == sum(t.amount for t in txns)


def test_adjustments_carry_their_own_sign_and_fold_into_gross():
    negative = [_t("pay_1", "payment", 100_000), _t("adj_1", "adjustment", -500)]
    positive = [_t("pay_1", "payment", 100_000), _t("adj_1", "adjustment", 500)]
    assert reconstruct(negative).net == 99_500 == sum(t.amount for t in negative)
    assert reconstruct(positive).net == 100_500 == sum(t.amount for t in positive)


def test_totals_are_ints_never_floats():
    tot = reconstruct([_t("pay_1", "payment", 100_000), _t("fee_1", "fee", -2_360)])
    for value in (tot.gross, tot.fees, tot.tax, tot.refunds, tot.holds, tot.net):
        assert isinstance(value, int) and not isinstance(value, bool)


def test_reconstruct_does_not_mutate_its_argument():
    """Lane C imports reconstruct for its verifier's arithmetic check. It has
    to stay a pure function of its argument list, with no matcher state."""
    txns = [_t("pay_1", "payment", 100_000), _t("fee_1", "fee", -2_360)]
    before = [(t.txn_id, t.amount) for t in txns]
    reconstruct(txns)
    assert [(t.txn_id, t.amount) for t in txns] == before
    assert len(txns) == 2


# --- against the hand-verified fixture (CSV_SCHEMAS.md 6) --------------------

EXPECTED = {
    #                gross      fees     tax  refunds   holds        net
    "setl_A1": (4_932_000, 116_395, 20_951, 0, 0, 4_794_654),
    "setl_B2": (2_775_000, 65_490, 11_788, 890_000, 50_000, 1_757_722),
    "setl_C3": (1_770_000, 41_772, 7_518, 0, 0, 1_720_710),
    "setl_D4": (3_000_000, 70_800, 12_744, 0, 0, 2_916_456),
    "setl_K9": (2_500_000, 59_000, 10_620, 0, 0, 2_430_380),
    "setl_M2": (2_500_000, 59_000, 10_620, 0, 0, 2_430_380),
}


def test_every_fixture_settlement_reconstructs_to_the_documented_totals():
    groups = _by_settlement()
    assert set(groups) == set(EXPECTED)
    for settlement_id, expected in EXPECTED.items():
        tot = reconstruct(groups[settlement_id])
        actual = (tot.gross, tot.fees, tot.tax, tot.refunds, tot.holds, tot.net)
        assert actual == expected, settlement_id


def test_the_signed_sum_identity_holds_across_the_whole_fixture():
    for settlement_id, legs in _by_settlement().items():
        assert reconstruct(legs).net == sum(t.amount for t in legs), settlement_id


def test_the_rounding_break_settlement_does_not_net_to_its_bank_credit():
    """setl_D4 reconstructs to 2916456 against a bank credit of 2916406.
    Residual is always `net - credit`, so this is +50: the bank credited 50
    paise LESS than the reconstruction. This is what forces T0 to decline."""
    assert reconstruct(_by_settlement()["setl_D4"]).net - 2_916_406 == 50


def test_the_two_trap_settlements_are_arithmetically_identical():
    groups = _by_settlement()
    assert reconstruct(groups["setl_K9"]).net == reconstruct(groups["setl_M2"]).net


# --- the MDR fee base (CSV_SCHEMAS.md 3.3, brief 5.1) -----------------------


def test_the_fee_base_is_the_payment_legs_only():
    """setl_B2 nets a refund and a chargeback. Deriving the fee from the net,
    or from gross-minus-refunds, is wrong on exactly the settlements that
    carry a deduction -- which is to say, on the interesting ones."""
    legs = _by_settlement()["setl_B2"]
    assert payment_gross(legs) == 2_775_000
    fee, tax = expected_fee_and_tax(legs)
    assert (fee, tax) == (65_490, 11_788)


def test_expected_fee_matches_the_actual_fee_legs_on_every_settlement():
    for settlement_id, legs in _by_settlement().items():
        fee, tax = expected_fee_and_tax(legs)
        tot = reconstruct(legs)
        assert (fee, tax) == (tot.fees, tot.tax), settlement_id


def test_gst_is_charged_on_the_fee_never_on_the_gross():
    legs = _by_settlement()["setl_A1"]
    fee, tax = expected_fee_and_tax(legs)
    assert tax == 20_951
    assert tax != (4_932_000 * 1800) // 10_000  # the classic error


def test_adjustments_are_excluded_from_the_fee_base():
    """reconstruct() folds adjustments into `gross` for reporting, so
    totals.gross is NOT the fee base whenever an adjustment is present."""
    legs = [
        _t("pay_1", "payment", 100_000),
        _t("adj_1", "adjustment", 50_000),
        _t("rfnd_1", "refund", -10_000),
    ]
    assert reconstruct(legs).gross == 150_000
    assert payment_gross(legs) == 100_000
    assert expected_fee_and_tax(legs)[0] == 2_360  # pct_of(100_000, 236)


# --- cardinality (spec 7.1) -------------------------------------------------


def test_cardinality_counts_payment_legs_alone():
    """A refund leg does not make a settlement a batch."""
    legs = [
        _t("pay_1", "payment", 100_000),
        _t("rfnd_1", "refund", -10_000),
        _t("cb_1", "chargeback", -5_000),
        _t("fee_1", "fee", -2_360),
        _t("tax_1", "tax", -424),
    ]
    assert payment_leg_count(legs) == 1


def test_fixture_cardinalities():
    """The trap's two settlements differ in cardinality -- which is exactly
    why candidacy must never be filtered by it."""
    groups = _by_settlement()
    counts = {sid: payment_leg_count(legs) for sid, legs in groups.items()}
    assert counts == {
        "setl_A1": 4,
        "setl_B2": 2,
        "setl_C3": 2,
        "setl_D4": 1,
        "setl_K9": 2,
        "setl_M2": 1,
    }
