"""Integrity checks for the hand-written tiny fixture in `fixtures/tiny/`.

Lane B (the matcher) measures its own match rate against this fixture from its
first hour. If the fixture's arithmetic is wrong, that lane's correctness signal
is silently wrong too, so the fixture is asserted here rather than eyeballed.

Deliberately imports nothing from `core.matcher` or `core.ingest` - neither
exists yet. Only `core.money.pct_of` (the frozen rounding rule) and the stdlib
`csv`/`json` readers are used, so this file stays runnable for the whole of
Phase 0.
"""

import csv
import json
from pathlib import Path

import pytest

from core.models import BankLine, Order, PSPTransaction
from core.money import pct_of

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "tiny"

MDR_BPS = 236  # 2.36%
GST_BPS = 1800  # 18%, charged on the fee - never on gross

# Residual convention is always `net - credit`, in that order (CSV_SCHEMAS.md 6).
ROUNDING_BREAK_DELTA = 50  # paise, exactly; positive == bank credited less

OPENING_BALANCE = 10_000_000

# Duplicate detection is only meaningful for legs that name an order.
# fee/tax/reserve legs are settlement-level and legitimately repeat across
# settlements - see CSV_SCHEMAS.md 3.2.1.
ORDER_BEARING_TYPES = {"payment", "refund", "chargeback"}

AMOUNT_COLUMNS = {
    "orders.csv": ["gross_amount"],
    "psp.csv": ["amount"],
    "bank.csv": ["credit", "debit", "balance"],
}


def _read(name: str) -> list[dict[str, str]]:
    with (FIXTURE_DIR / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def orders() -> list[dict[str, str]]:
    return _read("orders.csv")


@pytest.fixture(scope="module")
def psp() -> list[dict[str, str]]:
    return _read("psp.csv")


@pytest.fixture(scope="module")
def bank() -> list[dict[str, str]]:
    return _read("bank.csv")


@pytest.fixture(scope="module")
def truth() -> dict:
    with (FIXTURE_DIR / "truth.json").open(encoding="utf-8") as fh:
        return json.load(fh)


# --- derived views -----------------------------------------------------------


def _bank_by_id(bank: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row["line_id"]: row for row in bank}


def _settlement_of(truth: dict, bank_line_id: str) -> str:
    return next(
        link["settlement_id"]
        for link in truth["linkages"]
        if link["bank_line_id"] == bank_line_id
    )


def _defect(truth: dict, defect_type: str) -> dict:
    hits = [d for d in truth["injected_defects"] if d["defect_type"] == defect_type]
    assert len(hits) == 1, f"expected exactly one {defect_type} defect, got {len(hits)}"
    return hits[0]


def _net(psp: list[dict[str, str]], settlement_id: str) -> int:
    """Reconstructed net: sum of the signed paise amounts carrying this settlement_id."""
    rows = [r for r in psp if r["settlement_id"] == settlement_id]
    assert rows, f"no psp rows for {settlement_id}"
    return sum(int(r["amount"]) for r in rows)


def _component(psp: list[dict[str, str]], settlement_id: str, txn_type: str) -> int:
    return sum(
        int(r["amount"])
        for r in psp
        if r["settlement_id"] == settlement_id and r["txn_type"] == txn_type
    )


def _rounding_break_settlement(truth: dict) -> str:
    (line_id,) = _defect(truth, "rounding_break")["affected_ids"]
    return _settlement_of(truth, line_id)


def _trap_settlements(truth: dict) -> list[str]:
    return [_settlement_of(truth, line_id) for line_id in truth["unresolvable_ids"]]


def _economic_tuple(row: dict[str, str]) -> tuple[str, str, str, str]:
    """The identity of an economic event: everything except txn_id and settlement."""
    return (row["txn_type"], row["order_id"], row["captured_at"], row["amount"])


def _group_by_economic_tuple(rows: list[dict[str, str]]) -> dict[tuple, list[str]]:
    groups: dict[tuple, list[str]] = {}
    for row in rows:
        groups.setdefault(_economic_tuple(row), []).append(row["txn_id"])
    return groups


# --- 1. clean settlements reconstruct exactly ---------------------------------


def test_reconstructed_net_equals_bank_credit(psp, bank, truth):
    """Sum of signed PSP amounts for a settlement == that bank line's credit.

    Excludes the rounding-break settlement (asserted separately) and the two
    trap settlements (either assignment satisfies the identity by construction).
    """
    excluded = {_rounding_break_settlement(truth), *_trap_settlements(truth)}
    by_id = _bank_by_id(bank)

    checked = 0
    for link in truth["linkages"]:
        settlement_id = link["settlement_id"]
        if settlement_id in excluded:
            continue
        credit = int(by_id[link["bank_line_id"]]["credit"])
        assert _net(psp, settlement_id) == credit, (
            f"{settlement_id} does not reconstruct to {link['bank_line_id']}"
        )
        checked += 1

    assert checked == 3, "expected 3 exactly-reconstructing settlements"


# --- 2. the rounding break is exactly 50 paise --------------------------------


def test_rounding_break_is_exactly_fifty_paise(psp, bank, truth):
    (line_id,) = _defect(truth, "rounding_break")["affected_ids"]
    settlement_id = _settlement_of(truth, line_id)
    credit = int(_bank_by_id(bank)[line_id]["credit"])
    net = _net(psp, settlement_id)

    assert net != credit
    assert abs(net - credit) == ROUNDING_BREAK_DELTA
    # Sign convention is `net - credit`: positive means the bank credited less.
    assert net - credit == ROUNDING_BREAK_DELTA


# --- 3. fee and GST arithmetic ------------------------------------------------


def test_fee_and_gst_use_the_frozen_rounding_rule(psp, truth):
    settlement_ids = [link["settlement_id"] for link in truth["linkages"]]
    assert len(settlement_ids) == 6

    for settlement_id in settlement_ids:
        gross = _component(psp, settlement_id, "payment")
        fee = -_component(psp, settlement_id, "fee")
        tax = -_component(psp, settlement_id, "tax")

        assert gross > 0, f"{settlement_id} has no payment legs"
        assert fee == pct_of(gross, MDR_BPS), f"{settlement_id} fee != 2.36% of gross"
        assert tax == pct_of(fee, GST_BPS), f"{settlement_id} tax != 18% of fee"
        # GST is on the fee, never on gross - guard against the classic slip.
        assert tax != pct_of(gross, GST_BPS)


def test_every_settlement_carries_a_fee_and_a_tax_leg(psp, truth):
    for link in truth["linkages"]:
        settlement_id = link["settlement_id"]
        types = {
            r["txn_type"] for r in psp if r["settlement_id"] == settlement_id
        }
        assert {"payment", "fee", "tax"} <= types, settlement_id


# --- 4. referential integrity of truth.json -----------------------------------


def test_linkage_ids_all_exist_in_the_csvs(orders, psp, bank, truth):
    order_ids = {r["order_id"] for r in orders}
    txn_ids = {r["txn_id"] for r in psp}
    line_ids = {r["line_id"] for r in bank}
    settlement_ids = {r["settlement_id"] for r in psp if r["settlement_id"]}

    for link in truth["linkages"]:
        assert link["bank_line_id"] in line_ids, link["bank_line_id"]
        assert link["settlement_id"] in settlement_ids, link["settlement_id"]
        for txn_id in link["psp_txn_ids"]:
            assert txn_id in txn_ids, txn_id
        for order_id in link["order_ids"]:
            assert order_id in order_ids, order_id


def test_linkage_psp_ids_match_the_settlement_membership_in_psp_csv(psp, truth):
    """A linkage must name every PSP row carrying its settlement_id, and no other."""
    for link in truth["linkages"]:
        settlement_id = link["settlement_id"]
        actual = {r["txn_id"] for r in psp if r["settlement_id"] == settlement_id}
        assert set(link["psp_txn_ids"]) == actual, settlement_id


def test_record_count_matches_orders_csv(orders, truth):
    assert truth["record_count"] == len(orders) == 12


def test_seed_is_zero_because_the_fixture_is_hand_written(truth):
    assert truth["seed"] == 0


def test_all_nine_defect_types_are_present(truth):
    expected = {
        "many_to_one_batch",
        "cross_period_refund",
        "fee_plus_gst",
        "garbled_narration",
        "duplicate_psp_txn",
        "rounding_break",
        "chargeback_hold",
        "missing_order_ref",
        "ambiguous_unresolvable",
    }
    actual = {d["defect_type"] for d in truth["injected_defects"]}
    assert actual == expected
    assert len(truth["injected_defects"]) == 9
    # split_settlement is Lane A's generator, deliberately absent here.
    assert "split_settlement" not in actual


# --- 5. unresolvable ids are real bank lines ----------------------------------


def test_unresolvable_ids_are_real_bank_lines(bank, truth):
    line_ids = {r["line_id"] for r in bank}
    assert len(truth["unresolvable_ids"]) == 2
    for line_id in truth["unresolvable_ids"]:
        assert line_id in line_ids, line_id


# --- 6. integer paise everywhere ----------------------------------------------


def test_no_amount_column_contains_a_decimal_point(orders, psp, bank):
    tables = {"orders.csv": orders, "psp.csv": psp, "bank.csv": bank}
    for name, rows in tables.items():
        for column in AMOUNT_COLUMNS[name]:
            for row in rows:
                value = row[column]
                assert "." not in value, f"{name}:{column} = {value!r}"
                if value != "":
                    int(value)  # raises if it is not a plain integer


def test_raw_amount_fields_are_never_the_literal_none(orders, psp, bank):
    """An absent optional field is the empty string, not the string 'None'."""
    for rows in (orders, psp, bank):
        for row in rows:
            for value in row.values():
                assert value != "None"
                assert value is not None


# --- 7. the trap --------------------------------------------------------------


def test_trap_lines_are_indistinguishable_on_the_bank_side(bank, truth):
    first, second = (_bank_by_id(bank)[i] for i in truth["unresolvable_ids"])

    assert first["credit"] == second["credit"]
    assert first["txn_date"] == second["txn_date"]
    assert first["utr"] == "" and second["utr"] == ""
    # Narrations carry no entity and no settlement reference; here they are
    # byte-identical, so narration cannot break the tie either.
    assert first["narration"] == second["narration"]
    assert "setl_" not in first["narration"]


def test_both_trap_settlements_reconstruct_to_the_same_credit(psp, bank, truth):
    """Either assignment satisfies the arithmetic - that is what makes it a trap."""
    line_ids = truth["unresolvable_ids"]
    settlement_ids = _trap_settlements(truth)
    assert len(set(settlement_ids)) == 2

    credits = {int(_bank_by_id(bank)[i]["credit"]) for i in line_ids}
    nets = {_net(psp, s) for s in settlement_ids}
    assert len(credits) == 1
    assert nets == credits


def test_trap_settlements_share_a_settled_at_date(psp, truth):
    for settlement_id in _trap_settlements(truth):
        dates = {
            r["settled_at"] for r in psp if r["settlement_id"] == settlement_id
        }
        assert dates == {"2026-07-24"}


def test_no_other_bank_line_shares_the_trap_amount(bank, truth):
    trap_ids = set(truth["unresolvable_ids"])
    (trap_credit,) = {
        r["credit"] for r in bank if r["line_id"] in trap_ids
    }
    others = [r for r in bank if r["line_id"] not in trap_ids]
    assert all(r["credit"] != trap_credit for r in others)


# --- conformance to the frozen models (core/models.py) ------------------------


def _int_or_none(value: str) -> int | None:
    return int(value) if value != "" else None


def _str_or_none(value: str) -> str | None:
    return value if value != "" else None


def test_every_row_validates_against_the_frozen_models(orders, psp, bank):
    """The fixture must be loadable into the Phase-0 contracts unchanged.

    The coercion below is only the documented CSV convention (empty string means
    absent; amounts are plain integers). Real ingest lives in another lane.
    """
    for row in orders:
        Order(
            order_id=row["order_id"],
            order_date=row["order_date"],
            customer_ref=row["customer_ref"],
            gross_amount=int(row["gross_amount"]),
            currency=row["currency"],
            status=row["status"],
        )

    for row in psp:
        PSPTransaction(
            txn_id=row["txn_id"],
            txn_type=row["txn_type"],
            order_id=_str_or_none(row["order_id"]),
            captured_at=row["captured_at"],
            amount=int(row["amount"]),
            settlement_id=_str_or_none(row["settlement_id"]),
            settled_at=_str_or_none(row["settled_at"]),
        )

    for row in bank:
        BankLine(
            line_id=row["line_id"],
            txn_date=row["txn_date"],
            narration=row["narration"],
            credit=_int_or_none(row["credit"]),
            debit=_int_or_none(row["debit"]),
            balance=int(row["balance"]),
            utr=_str_or_none(row["utr"]),
        )


def test_psp_amount_signs_follow_the_convention(psp):
    positive = {"payment"}
    negative = {"fee", "tax", "refund", "chargeback", "reserve"}
    for row in psp:
        amount, txn_type = int(row["amount"]), row["txn_type"]
        if txn_type in positive:
            assert amount > 0, row["txn_id"]
        elif txn_type in negative:
            assert amount < 0, row["txn_id"]


def test_bank_balance_chain_is_exact(bank):
    """balance[n] == balance[n-1] + credit - debit, from a known opening balance."""
    running = OPENING_BALANCE
    for row in bank:
        credit = int(row["credit"]) if row["credit"] else 0
        debit = int(row["debit"]) if row["debit"] else 0
        running += credit - debit
        assert running == int(row["balance"]), row["line_id"]
    assert running == 26_050_252


def test_only_payment_rows_treat_an_empty_order_id_as_a_defect(psp, truth):
    """fee/tax/chargeback legs are settlement-level and legitimately order-less."""
    defect_ids = set(_defect(truth, "missing_order_ref")["affected_ids"])
    offenders = {
        r["txn_id"]
        for r in psp
        if r["txn_type"] == "payment" and r["order_id"] == ""
    }
    assert offenders == defect_ids


# --- duplicate detection scope (CSV_SCHEMAS.md 3.2.1) -------------------------
#
# fee_1005/fee_1006 and tax_1005/tax_1006 are byte-identical on the economic
# tuple (txn_type, order_id, captured_at, amount) - the SAME discriminator that
# separates pay_1105 from pay_1103. That collision is deliberate: perturbing the
# settlement-leg timestamps to dodge it would introduce a PSP-side ordering that
# the ambiguity trap depends on being absent. The resolving rule is that
# duplicate detection only applies to legs that name an order.


def test_settlement_level_legs_collide_on_the_economic_tuple(psp):
    """The collision must stay deliberate, not become accidental."""
    settlement_level = [r for r in psp if r["txn_type"] not in ORDER_BEARING_TYPES]
    collisions = {
        tuple_: ids
        for tuple_, ids in _group_by_economic_tuple(settlement_level).items()
        if len(ids) > 1
    }

    assert sorted(sorted(ids) for ids in collisions.values()) == [
        ["fee_1005", "fee_1006"],
        ["tax_1005", "tax_1006"],
    ]
    # Every colliding pair has an empty order_id - that is what makes the
    # order-bearing restriction a sound rule rather than a lucky one.
    for txn_type, order_id, _captured_at, _amount in collisions:
        assert order_id == ""
        assert txn_type in {"fee", "tax"}


def test_dedup_on_order_bearing_legs_finds_exactly_the_injected_duplicate(psp, truth):
    """Applying the documented rule yields the defect and nothing else."""
    order_bearing = [
        r for r in psp if r["txn_type"] in ORDER_BEARING_TYPES and r["order_id"]
    ]
    collisions = [
        sorted(ids)
        for ids in _group_by_economic_tuple(order_bearing).values()
        if len(ids) > 1
    ]

    expected = sorted(_defect(truth, "duplicate_psp_txn")["affected_ids"])
    assert collisions == [expected]


def test_naive_dedup_rules_are_the_two_failure_modes_the_docs_warn_about(psp, truth):
    """Pins WHY the order-bearing restriction exists, so nobody drops it."""
    duplicate_id, canonical_id = _defect(truth, "duplicate_psp_txn")["affected_ids"]

    # Failure mode A: no order_id restriction -> false positives on fee/tax.
    all_collisions = {
        tuple(sorted(ids))
        for ids in _group_by_economic_tuple(psp).values()
        if len(ids) > 1
    }
    assert ("fee_1005", "fee_1006") in all_collisions
    assert ("tax_1005", "tax_1006") in all_collisions

    # Failure mode B: settlement_id in the key -> the real duplicate is missed,
    # because the duplicate row is unsettled and the canonical row is not.
    by_id = {r["txn_id"]: r for r in psp}
    assert _economic_tuple(by_id[duplicate_id]) == _economic_tuple(by_id[canonical_id])
    assert by_id[duplicate_id]["settlement_id"] != by_id[canonical_id]["settlement_id"]


# --- per-defect assertions against the CSVs, not against truth.json ----------
#
# test_all_nine_defect_types_are_present only reads truth.json - it is a
# self-consistency check on a file asserting things about itself. These tie each
# defect class to the actual rows, so the fixture cannot drift out from under
# its own labels.


def test_defect_affected_ids_all_exist_in_the_csvs(orders, psp, bank, truth):
    universe = (
        {r["order_id"] for r in orders}
        | {r["txn_id"] for r in psp}
        | {r["line_id"] for r in bank}
    )
    for defect in truth["injected_defects"]:
        assert defect["affected_ids"], defect["defect_type"]
        for affected_id in defect["affected_ids"]:
            assert affected_id in universe, (defect["defect_type"], affected_id)


def test_many_to_one_batch_really_is_many_to_one(orders, psp, truth):
    defect = _defect(truth, "many_to_one_batch")
    line_id = next(i for i in defect["affected_ids"] if i.startswith("BL-"))
    settlement_id = _settlement_of(truth, line_id)

    payments = [
        r
        for r in psp
        if r["settlement_id"] == settlement_id and r["txn_type"] == "payment"
    ]
    assert len(payments) == 4, "the clean batch must collapse 4 orders"
    assert all(r["order_id"] for r in payments)
    assert len({r["order_id"] for r in payments}) == 4

    # The defect's order ids are exactly that batch, and all are real orders.
    order_ids = {i for i in defect["affected_ids"] if i.startswith("ORD-")}
    assert order_ids == {r["order_id"] for r in payments}
    assert order_ids <= {r["order_id"] for r in orders}


def test_cross_period_refund_points_at_an_earlier_settlement(psp, truth):
    refund_id, order_id = _defect(truth, "cross_period_refund")["affected_ids"]
    by_id = {r["txn_id"]: r for r in psp}
    refund = by_id[refund_id]

    assert refund["txn_type"] == "refund"
    assert int(refund["amount"]) < 0
    assert refund["order_id"] == order_id

    # The refunded order was paid in a DIFFERENT, EARLIER settlement.
    payment = next(
        r
        for r in psp
        if r["txn_type"] == "payment"
        and r["order_id"] == order_id
        and r["settlement_id"]
    )
    assert payment["settlement_id"] != refund["settlement_id"]
    assert payment["settled_at"] < refund["settled_at"]
    # ...and the refund was captured before the cycle it lands in settles.
    assert refund["captured_at"][:10] < refund["settled_at"]


def test_garbled_narration_line_carries_no_usable_reference(bank, truth):
    (line_id,) = _defect(truth, "garbled_narration")["affected_ids"]
    row = _bank_by_id(bank)[line_id]

    assert row["utr"] == ""
    assert "setl_" not in row["narration"]
    assert "  " in row["narration"], "doubled spaces are part of the defect"
    # Still resolvable: amount + date pick exactly one settlement. No other bank
    # line shares both, so there is nothing to be ambiguous with.
    twins = [
        r
        for r in bank
        if r["line_id"] != line_id
        and r["credit"] == row["credit"]
        and r["txn_date"] == row["txn_date"]
    ]
    assert twins == []


def test_chargeback_hold_has_no_corresponding_order(orders, psp, truth):
    (txn_id,) = _defect(truth, "chargeback_hold")["affected_ids"]
    row = next(r for r in psp if r["txn_id"] == txn_id)

    assert row["txn_type"] == "chargeback"
    assert int(row["amount"]) < 0
    # It HAS a reference - the reference just dangles. That is what keeps it
    # distinguishable from missing_order_ref, which has no reference at all.
    assert row["order_id"] != ""
    assert row["order_id"] not in {o["order_id"] for o in orders}
    # It is still inside a settlement, so that settlement's arithmetic closes.
    assert row["settlement_id"] != ""
    # And it is never listed as an order in any linkage.
    for link in truth["linkages"]:
        assert row["order_id"] not in link["order_ids"]


def test_missing_order_ref_is_recoverable_and_recorded_in_truth(orders, psp, truth):
    (txn_id,) = _defect(truth, "missing_order_ref")["affected_ids"]
    row = next(r for r in psp if r["txn_id"] == txn_id)

    assert row["txn_type"] == "payment"
    assert row["order_id"] == ""
    assert row["settlement_id"] != ""

    # Exactly one order in the register has that gross - that is the recovery.
    candidates = [o for o in orders if o["gross_amount"] == row["amount"]]
    assert len(candidates) == 1
    recovered = candidates[0]["order_id"]

    # Truth records the recovered order even though no PSP row names it.
    link = next(
        lk for lk in truth["linkages"] if lk["settlement_id"] == row["settlement_id"]
    )
    assert recovered in link["order_ids"]
    named_by_legs = {
        r["order_id"]
        for r in psp
        if r["settlement_id"] == row["settlement_id"] and r["order_id"]
    }
    assert recovered not in named_by_legs


def test_fee_plus_gst_defect_covers_every_settlement(psp, truth):
    affected = set(_defect(truth, "fee_plus_gst")["affected_ids"])
    assert affected == {r["txn_id"] for r in psp if r["txn_type"] in {"fee", "tax"}}
    for link in truth["linkages"]:
        legs = {
            r["txn_type"]
            for r in psp
            if r["settlement_id"] == link["settlement_id"] and r["txn_id"] in affected
        }
        assert legs == {"fee", "tax"}, link["settlement_id"]
