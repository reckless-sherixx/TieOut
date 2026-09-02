"""End-to-end engine tests against `fixtures/tiny/`.

The tier walk asserted here is the fixture's designed answer, not an
observation of what the code happens to do:

| Bank line | Settlement | Payment legs | Tier | Why |
|---|---|---:|---|---|
| BL-0001 | setl_A1 | 4 | T0 | narration names it AND it nets exactly |
| BL-0002 | setl_B2 | 2 | T0 | same |
| BL-0003 | setl_C3 | 2 | T2 | no reference; unique exact-net candidate; two payment legs |
| BL-0004 | setl_D4 | 1 | T3 | reference hits but nets +50, so T0/T1/T2 decline |
| BL-0005 | -- | -- | exception | two settlements close it |
| BL-0006 | -- | -- | exception | same |

**T1 matches nothing here and that is correct.** The fixture's only two
single-payment-leg settlements are `setl_D4` (rounding break, so T3) and
`setl_M2` (half the ambiguity trap, so its bank line is an exception).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.ingest.reader import read_bank, read_orders, read_psp
from core.matcher.engine import run_match
from core.models import ReasonCode

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "tiny"


@pytest.fixture(scope="module")
def result():
    return run_match(
        read_orders(FIX / "orders.csv"),
        read_psp(FIX / "psp.csv"),
        read_bank(FIX / "bank.csv"),
    )


def _by_line(result) -> dict[str, str]:
    return {m.bank_line_id: m.tier for m in result.matches}


# --- the plan's three engine tests --------------------------------------------


def test_engine_runs_on_fixture_and_emits_exceptions(result):
    assert len(result.matches) > 0
    assert len(result.exceptions) > 0


def test_every_subject_is_either_matched_or_excepted_exactly_once(result):
    """The partition invariant: this is what makes the rates sum to 1."""
    matched = {m.bank_line_id for m in result.matches}
    excepted = [e.subject_id for e in result.exceptions if e.subject_type == "bank_line"]

    assert matched & set(excepted) == set(), "a subject was both matched and excepted"
    assert len(excepted) == len(set(excepted)), "a subject was excepted twice"
    assert len(matched) == len(result.matches), "a subject was matched twice"

    all_lines = {line.line_id for line in read_bank(FIX / "bank.csv")}
    assert matched | set(excepted) == all_lines, "a subject was neither"


def test_ambiguous_trap_lines_land_in_exceptions(result):
    codes = {e.reason_code.value for e in result.exceptions}
    assert "AMBIGUOUS_MULTI_CANDIDATE" in codes


# --- the designed tier walk ---------------------------------------------------


def test_the_tier_walk_is_the_designed_one(result):
    assert _by_line(result) == {
        "BL-0001": "T0",
        "BL-0002": "T0",
        "BL-0003": "T2",
        "BL-0004": "T3",
    }


def test_matches_carry_the_designed_settlements(result):
    assert {m.bank_line_id: m.settlement_id for m in result.matches} == {
        "BL-0001": "setl_A1",
        "BL-0002": "setl_B2",
        "BL-0003": "setl_C3",
        "BL-0004": "setl_D4",
    }


def test_t1_matches_nothing_on_this_fixture(result):
    """Zero is the correct value here, not a bug. The two tempting "fixes" --
    relaxing the cardinality rule or relaxing the ambiguity rule -- each trade
    the project's honesty argument for a non-zero number in a table."""
    assert [m for m in result.matches if m.tier == "T1"] == []


def test_both_trap_lines_are_ambiguous_exceptions(result):
    trap = {
        e.subject_id: e.reason_code
        for e in result.exceptions
        if e.subject_id in {"BL-0005", "BL-0006"}
    }
    assert trap == {
        "BL-0005": ReasonCode.AMBIGUOUS_MULTI_CANDIDATE,
        "BL-0006": ReasonCode.AMBIGUOUS_MULTI_CANDIDATE,
    }


def test_bl0004_is_t3_because_t0_declined_the_arithmetic(result):
    """The narration names `setl_D4`, so a reference-only T0 would claim it at
    confidence 1.00 and write a match 50 paise away from the credit."""
    (match,) = [m for m in result.matches if m.bank_line_id == "BL-0004"]
    assert match.tier == "T3" and match.confidence == 0.80
    assert any("delta=50" in e for e in match.evidence)
    assert match.gross - match.fees - match.tax == 2_916_456
    assert match.net == 2_916_406


def test_every_match_net_equals_its_bank_line_credit(result):
    credits = {line.line_id: line.credit for line in read_bank(FIX / "bank.csv")}
    for match in result.matches:
        assert match.net == credits[match.bank_line_id], match.match_id


# --- the order set ------------------------------------------------------------


def test_the_recovered_order_is_in_the_match_group(result):
    """`pay_1104` carries an empty `order_id`. Recovering `ORD-004603` is what
    solving the `missing_order_ref` defect means; a matcher that scrapes
    `order_id` off the rows omits it and is marked wrong for being right."""
    (match,) = [m for m in result.matches if m.settlement_id == "setl_C3"]
    assert set(match.order_ids) == {"ORD-004602", "ORD-004603"}


def test_the_dangling_chargeback_reference_is_not_an_order(result):
    """`cb_7701` names `ORD-004018`, which is not in the register. It is not a
    real order, so it is not in the settlement's order set."""
    (match,) = [m for m in result.matches if m.settlement_id == "setl_B2"]
    assert "ORD-004018" not in match.order_ids
    assert set(match.order_ids) == {"ORD-004510", "ORD-004511", "ORD-004472"}


def test_the_cross_period_refund_order_appears_in_both_settlements(result):
    """`ORD-004472` is paid in `setl_A1` and refunded in `setl_B2`."""
    groups = {m.settlement_id: set(m.order_ids) for m in result.matches}
    assert "ORD-004472" in groups["setl_A1"]
    assert "ORD-004472" in groups["setl_B2"]


def test_every_order_id_in_every_match_resolves_to_a_real_order(result):
    real = {o.order_id for o in read_orders(FIX / "orders.csv")}
    for match in result.matches:
        assert set(match.order_ids) <= real, match.match_id


# --- PSP-side exceptions ------------------------------------------------------


def test_the_duplicate_payment_is_excepted_and_kept_out_of_every_batch(result):
    duplicates = [
        e for e in result.exceptions if e.reason_code is ReasonCode.DUPLICATE_PSP_TXN
    ]
    assert [e.subject_id for e in duplicates] == ["pay_1105"]
    assert all("pay_1105" not in m.psp_txn_ids for m in result.matches)


def test_no_false_duplicate_is_raised_on_the_fee_and_tax_legs(result):
    """`fee_1005`/`fee_1006` and `tax_1005`/`tax_1006` are byte-identical on
    the economic tuple without being duplicates: settlement-level legs carry an
    empty `order_id` by design and legitimately repeat across settlements."""
    flagged = {
        e.subject_id
        for e in result.exceptions
        if e.reason_code is ReasonCode.DUPLICATE_PSP_TXN
    }
    assert flagged & {"fee_1005", "fee_1006", "tax_1005", "tax_1006"} == set()


def test_an_in_settlement_duplicate_is_discounted_so_the_batch_can_close():
    """The harsher shape the generator emits at scale: the duplicate carries
    the SAME settlement_id as its twin and inflates the reconstructed net, so
    nothing can close until it is discounted."""
    from .conftest import build_settlement, make_bank_line, make_order, make_txn, net_of

    legs = build_settlement("setl_D1", [1_000_000], order_ids=["ORD-D1"])
    twin = next(t for t in legs if t.txn_type == "payment")
    inflated = [
        *legs,
        make_txn(
            "pay_D1_dup",
            "payment",
            twin.amount,
            order_id=twin.order_id,
            settlement_id="setl_D1",
            captured_at=twin.captured_at.isoformat(),
        ),
    ]
    credit = net_of(legs)
    result = run_match(
        [make_order("ORD-D1", 1_000_000)],
        inflated,
        [make_bank_line("BL-D1", credit)],
    )

    assert [m.settlement_id for m in result.matches] == ["setl_D1"]
    assert result.matches[0].net == credit, "the inflated net had to be discounted"

    # The two rows are economically identical, so WHICH one survives cannot
    # change any arithmetic -- the invariant is that the pair collapses to one.
    pair = {"pay_D1_dup", twin.txn_id}
    kept = pair & set(result.matches[0].psp_txn_ids)
    assert len(kept) == 1
    assert {
        e.subject_id
        for e in result.exceptions
        if e.reason_code is ReasonCode.DUPLICATE_PSP_TXN
    } == pair - kept
    assert result.matches[0].order_ids == ["ORD-D1"]


def test_a_payment_leg_whose_order_cannot_be_recovered_is_excepted():
    from .conftest import build_settlement, make_bank_line, make_txn, net_of

    orphan = make_txn(
        "pay_M1_anon", "payment", 111_111, settlement_id="setl_M1", order_id=None
    )
    legs = build_settlement("setl_M1", [1_000_000], extra_legs=[orphan])
    result = run_match([], legs, [make_bank_line("BL-M1", net_of(legs))])
    assert any(
        e.subject_id == "pay_M1_anon" and e.reason_code is ReasonCode.MISSING_ORDER_REF
        for e in result.exceptions
    )


def test_the_recovered_leg_is_not_also_excepted(result):
    """`pay_1104`'s order WAS recovered, so it is not a missing reference."""
    missing = {
        e.subject_id
        for e in result.exceptions
        if e.reason_code is ReasonCode.MISSING_ORDER_REF
    }
    assert "pay_1104" not in missing


# --- the audit trail ----------------------------------------------------------


def test_the_audit_trail_is_monotonic_and_covers_every_bank_line(result):
    assert [e.sequence for e in result.audit] == list(range(len(result.audit)))
    subjects = {e.subject_id for e in result.audit}
    assert {f"BL-000{i}" for i in range(1, 7)} <= subjects


def test_the_recovery_attempt_is_recorded_even_though_it_succeeded(result):
    trail = " ".join(e.evidence for e in result.audit if e.subject_id == "pay_1104")
    assert "ORD-004603" in trail


def test_the_declined_t0_on_bl0004_is_recorded(result):
    rules = [e.rule for e in result.audit if e.subject_id == "BL-0004"]
    assert any(r.startswith("T0:") for r in rules)
    trail = " ".join(e.evidence for e in result.audit if e.subject_id == "BL-0004")
    assert "setl_D4" in trail and "delta=50" in trail


# --- determinism --------------------------------------------------------------


def test_the_run_is_reproducible():
    def once():
        r = run_match(
            read_orders(FIX / "orders.csv"),
            read_psp(FIX / "psp.csv"),
            read_bank(FIX / "bank.csv"),
        )
        return (
            [(m.match_id, m.settlement_id, m.tier, tuple(m.order_ids)) for m in r.matches],
            [(e.subject_id, e.reason_code) for e in r.exceptions],
            [(e.entry_id, e.rule) for e in r.audit],
        )

    assert once() == once()
