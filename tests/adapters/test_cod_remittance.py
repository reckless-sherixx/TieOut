"""`cod-remittance-delhivery-v1` against the hand-written fixtures.

The last test in this module is the one the format exists for: the legs this
adapter emits, handed to the ENGINE AS IT ALREADY IS, reconcile a COD
remittance against a bank credit. Nothing in `core/matcher/` was touched to
make that work, and the test would fail if it had to be.

Every expected value was computed by hand from the fixture text, and the clean
fixture states the same arithmetic in its comment header.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from core.adapters.base import QuarantineReason
from core.adapters.cod_remittance import (
    DEFAULT_COLUMN_MAP,
    CODRemittanceAdapter,
    settlement_id_for,
)
from core.models import BankLine

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "real-formats"
CLEAN = FIXTURES / "cod-remittance-clean.csv"
DIRTY = FIXTURES / "cod-remittance-dirty.csv"

#: The net of the clean fixture, hand-computed in its comment header:
#: 21396.00 collected less 1157.54 of freight, COD handling, RTO and GST.
EXPECTED_NET_PAISE = 2_023_846
SETTLEMENT_ID = "setl_codDLVREM26081401"


@pytest.fixture
def adapter() -> CODRemittanceAdapter:
    return CODRemittanceAdapter()


def _by_id(result) -> dict:
    return {record.txn_id: record for record in result.records}


# --- identity and detection -------------------------------------------------


def test_format_id_names_the_courier_and_the_layout(adapter):
    assert adapter.format_id == "cod-remittance-delhivery-v1"
    assert adapter.format_version == "1.0"


def test_the_adapter_is_registered_and_detected_from_the_header(adapter):
    from core.adapters import registry

    assert adapter.format_id in {a.format_id for a in registry.adapters()}
    assert registry.detect(CLEAN).format_id == adapter.format_id


@pytest.mark.parametrize(
    "other",
    [
        "hdfc-statement-clean.csv",
        "icici-statement-clean.csv",
        "razorpay-settlement-clean.csv",
        "shopify-orders-clean.csv",
        "mt940-statement-clean.sta",
    ],
)
def test_no_other_fixture_looks_like_a_remittance(adapter, other):
    assert adapter.sniff((FIXTURES / other).read_bytes()[:8192]) == 0.0


# --- the settlement id ------------------------------------------------------


def test_the_settlement_id_is_derived_from_the_remittance_reference():
    assert settlement_id_for("DLV/REM/26081401") == SETTLEMENT_ID
    assert settlement_id_for("DLV-REM-26081401") == SETTLEMENT_ID
    # Deterministic: the same remittance in two files is one batch.
    assert settlement_id_for("DLV/REM/26081401") == settlement_id_for(
        "DLV/REM/26081401"
    )


def test_the_settlement_id_is_findable_by_the_existing_narration_canonicaliser():
    """The constraint that fixes the shape of this string.

    `core/canonicalize/narration.py` matches `setl_[A-Za-z0-9]+` on a word
    boundary, and an underscore is a word character -- so `setl_cod_DLVREM...`
    would match NOTHING and a bank narration carrying it would silently fail to
    join. `setl_cod` is one token for that reason, and this test is what stops
    somebody making it more readable.
    """
    from core.canonicalize.narration import canonicalize

    narration = f"NEFT CR-DELHIVERY LTD-COD REMITTANCE {SETTLEMENT_ID}"
    assert canonicalize(narration).settlement_id == SETTLEMENT_ID

    # The negative control, which is the whole reason the prefix has no
    # second underscore: `setl_cod_DLVREM...` is not found at all.
    assert canonicalize("NEFT CR setl_cod_DLVREM26081401").settlement_id is None


def test_every_leg_of_one_remittance_shares_one_settlement_id(adapter):
    result = adapter.parse(CLEAN)
    assert {record.settlement_id for record in result.records} == {SETTLEMENT_ID}


# --- the worked example -----------------------------------------------------


def test_clean_fixture_parses_with_zero_quarantine(adapter):
    result = adapter.parse(CLEAN)
    assert result.quarantined == []
    assert result.record_count == 25
    assert len(result.row_hashes) == 25


def test_the_legs_sum_to_exactly_the_hand_computed_net(adapter):
    """The assertion the clean fixture's comment header exists to be checked
    against: 21396.00 collected, 1157.54 deducted, 20238.46 credited."""
    result = adapter.parse(CLEAN)
    assert sum(record.amount for record in result.records) == EXPECTED_NET_PAISE


def test_the_leg_shape_is_the_canonical_trio(adapter):
    """5 payment legs, 13 fee legs, 7 tax legs. One COD row collected nothing
    -- it was returned to origin -- and a zero is not a leg."""
    result = adapter.parse(CLEAN)
    counts: dict[str, int] = {}
    for record in result.records:
        counts[record.txn_type] = counts.get(record.txn_type, 0) + 1
    assert counts == {"payment": 5, "fee": 13, "tax": 7}


def test_the_gross_and_each_deduction_class_are_what_the_header_says(adapter):
    records = adapter.parse(CLEAN).records
    gross = sum(r.amount for r in records if r.txn_type == "payment")
    fees = -sum(r.amount for r in records if r.txn_type == "fee")
    tax = -sum(r.amount for r in records if r.txn_type == "tax")
    assert gross == 2_139_600  # 21396.00 collected
    assert fees == 98_096  # freight 365.00 + COD handling 320.96 + RTO 295.00
    assert tax == 17_658  # GST 176.58
    assert gross - fees - tax == EXPECTED_NET_PAISE


def test_one_cod_order_becomes_a_payment_leg_and_its_own_deductions(adapter):
    legs = _by_id(adapter.parse(CLEAN))
    assert legs["cod_DL10000001"].amount == 249_900
    assert legs["cod_DL10000001"].txn_type == "payment"
    assert legs["cod_DL10000001"].order_id == "ORD-004471"
    assert legs["frt_DL10000001"].amount == -4_500
    assert legs["cdf_DL10000001"].amount == -3_749
    assert legs["gst_DL10000001"].amount == -1_485
    assert "rto_DL10000001" not in legs  # no RTO on a delivered order


def test_a_returned_shipment_has_deductions_and_no_payment_leg(adapter):
    """DL10000006 collected nothing and was charged freight, RTO and GST. A
    zero-value payment leg would inflate every per-leg count downstream and
    tell the engine an order settled when none did."""
    legs = _by_id(adapter.parse(CLEAN))
    assert "cod_DL10000006" not in legs
    assert legs["frt_DL10000006"].amount == -4_500
    assert legs["rto_DL10000006"].amount == -4_500
    assert legs["gst_DL10000006"].amount == -1_620


def test_a_remittance_level_deduction_row_has_no_order_and_still_nets(adapter):
    """A monthly RTO reversal belongs to the remittance, not to an order. It
    carries no waybill, so its legs are named after the remittance and the line
    it sat on, and its `order_id` is null -- which the canonical schema allows
    precisely for money that is not attributable to one order."""
    records = adapter.parse(CLEAN).records
    orphan = [record for record in records if record.order_id is None]
    assert len(orphan) == 2
    assert {record.amount for record in orphan} == {-25_000, -4_500}
    assert all(record.settlement_id == SETTLEMENT_ID for record in orphan)
    assert all(record.txn_id.split("_")[1].startswith("DLVREM") for record in orphan)


def test_the_deduction_row_takes_the_remittance_date_having_no_delivery(adapter):
    records = adapter.parse(CLEAN).records
    orphan = [record for record in records if record.order_id is None]
    assert all(record.captured_at == datetime(2026, 8, 14, 0, 0) for record in orphan)
    assert all(record.settled_at == date(2026, 8, 14) for record in orphan)


def test_a_cod_leg_is_captured_on_its_delivery_date_and_settled_on_remittance(adapter):
    leg = _by_id(adapter.parse(CLEAN))["cod_DL10000003"]
    assert leg.captured_at == datetime(2026, 8, 11, 0, 0)
    assert leg.settled_at == date(2026, 8, 14)


def test_indian_digit_grouping_in_a_cod_amount_survives(adapter):
    assert _by_id(adapter.parse(CLEAN))["cod_DL10000005"].amount == 1_250_000


# --- the configurable column map -------------------------------------------


def test_the_column_map_is_configuration_not_code(tmp_path: Path):
    """A courier that spells freight `Forward Charges` is not a new format, so
    it must not need a new class. Real courier files vary and none of them are
    documented; the shape is what is stable, not the spelling."""
    renamed = CLEAN.read_text(encoding="utf-8").replace(
        "Freight Charge", "Forward Charges"
    ).replace("COD Amount", "COD Collected")
    path = tmp_path / "some-other-courier.csv"
    path.write_text(renamed, encoding="utf-8")

    adapter = CODRemittanceAdapter(
        column_map={"freight": "Forward Charges", "cod_amount": "COD Collected"}
    )
    result = adapter.parse(path)
    assert result.quarantined == []
    assert sum(record.amount for record in result.records) == EXPECTED_NET_PAISE


def test_the_default_adapter_does_not_read_the_renamed_file(tmp_path: Path):
    """The negative control. Without the override the required column is
    missing, and that is a file-level quarantine rather than a silent zero --
    a freight column read as absent would over-credit every remittance."""
    renamed = CLEAN.read_text(encoding="utf-8").replace("COD Amount", "COD Collected")
    path = tmp_path / "some-other-courier.csv"
    path.write_text(renamed, encoding="utf-8")

    result = CODRemittanceAdapter().parse(path)
    assert result.records == []
    assert result.quarantined[0].reason is QuarantineReason.MISSING_HEADER_COLUMN


def test_an_unknown_role_is_refused_at_construction():
    with pytest.raises(ValueError, match="unknown column role"):
        CODRemittanceAdapter(column_map={"freight_charge": "Forward Charges"})


def test_the_roles_are_the_documented_ones():
    assert set(DEFAULT_COLUMN_MAP) == {
        "remittance_ref",
        "utr",
        "remittance_date",
        "waybill",
        "order_id",
        "row_type",
        "delivery_date",
        "cod_amount",
        "freight",
        "cod_fee",
        "rto",
        "gst",
    }


def test_a_file_with_no_row_type_column_reads_every_row_as_a_collection(
    tmp_path: Path,
):
    lines = [
        line
        for line in CLEAN.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    columns = lines[0].split(",")
    index = columns.index("Row Type")
    trimmed = [
        ",".join(cell for position, cell in enumerate(line.split(",")) if position != index)
        for line in lines
        # Drop the DEDUCTION row: without the column there is no way to say so.
        if ",DEDUCTION," not in line
    ]
    path = tmp_path / "no-row-type.csv"
    path.write_text("\n".join(trimmed) + "\n", encoding="utf-8")

    result = CODRemittanceAdapter().parse(path)
    assert result.quarantined == []
    # The whole file less the remittance-level row's -250.00 and -45.00 GST.
    assert sum(r.amount for r in result.records) == EXPECTED_NET_PAISE + 29_500


# --- the dirty fixture ------------------------------------------------------


def test_dirty_fixture_still_yields_its_clean_rows(adapter):
    """Three clean rows, four legs each."""
    result = adapter.parse(DIRTY)
    assert result.record_count == 12
    assert {r.txn_id for r in result.records if r.txn_type == "payment"} == {
        "cod_DL20000001",
        "cod_DL20000006",
        "cod_DL20000010",
    }


def test_every_defect_is_named_by_line_and_reason(adapter):
    result = adapter.parse(DIRTY)
    by_line = {q.row_number: q.reason for q in result.quarantined}
    assert by_line == {
        13: QuarantineReason.BAD_DECIMAL,  # COD Amount 1899.005 -- sub-paise
        14: QuarantineReason.MISSING_VALUE,  # Remittance Ref empty
        15: QuarantineReason.TRUNCATED_ROW,  # 4 of 12 fields
        16: QuarantineReason.BAD_DATE,  # Remittance Date 20/08/2026
        18: QuarantineReason.DUPLICATE_ROW,  # waybill DL20000006 twice
        19: QuarantineReason.UNKNOWN_VALUE,  # Row Type `PARTIAL`
        20: QuarantineReason.AMBIGUOUS_DIRECTION,  # moves no money at all
        21: QuarantineReason.EXTRA_FIELDS,  # unquoted comma in Order ID
    }


def test_an_unknown_row_type_is_refused_rather_than_guessed(adapter):
    quarantine = {q.row_number: q for q in adapter.parse(DIRTY).quarantined}[19]
    assert quarantine.reason is QuarantineReason.UNKNOWN_VALUE
    assert "PARTIAL" in quarantine.detail


def test_a_row_that_moves_no_money_is_quarantined_not_emitted_as_nothing(adapter):
    """Emitting zero legs for it would be indistinguishable from dropping it,
    and "never silently drop" has to survive the case where the correct number
    of records happens to be none."""
    quarantine = {q.row_number: q for q in adapter.parse(DIRTY).quarantined}[20]
    assert quarantine.reason is QuarantineReason.AMBIGUOUS_DIRECTION
    assert "moves \nno money" in quarantine.detail.replace("moves no money", "moves \nno money")


def test_nothing_is_dropped(adapter):
    result = adapter.parse(DIRTY)
    parsed_rows = 3
    assert result.quarantine_count + parsed_rows + result.skipped_rows == 11


def test_parsing_twice_is_identical(adapter):
    first = adapter.parse(CLEAN)
    second = adapter.parse(CLEAN)
    assert first.row_hashes == second.row_hashes
    assert [r.model_dump() for r in first.records] == [
        r.model_dump() for r in second.records
    ]


# --- the proof: the existing engine reconciles it --------------------------


def test_the_existing_matcher_reconciles_a_cod_remittance_with_no_engine_change(
    adapter,
):
    """The claim this whole format was built to test.

    A COD remittance is many collections netted against courier deductions,
    arriving as one bank credit -- the same shape as a PSP settlement. If that
    is true, then the legs this adapter emits plus one bank line should close
    through the matcher exactly as a Razorpay settlement does, with no new
    tier, no new model field and nothing in `core/matcher/` touched.

    The bank line here is hand-built rather than read from a fixture on
    purpose: it is the *other* side of the reconciliation and it comes from the
    merchant's bank, not from the courier's file.
    """
    from core.matcher.engine import run_match

    legs = adapter.parse(CLEAN).records
    credit = BankLine(
        line_id="HDFC-COD-0001",
        txn_date=date(2026, 8, 14),
        narration=(
            f"NEFT CR-DELHIVERY LTD-COD REMITTANCE {SETTLEMENT_ID}-"
            "HDFCR52026081409"
        ),
        credit=EXPECTED_NET_PAISE,
        debit=None,
        balance=EXPECTED_NET_PAISE,
        utr="HDFCR52026081409",
    )

    result = run_match(orders=[], psp_txns=list(legs), bank_lines=[credit])

    assert len(result.matches) == 1, [e.reason_code for e in result.exceptions]
    match = result.matches[0]
    assert match.bank_line_id == "HDFC-COD-0001"
    assert match.settlement_id == SETTLEMENT_ID
    assert match.net == EXPECTED_NET_PAISE
    assert match.gross == 2_139_600
    assert match.fees == 98_096
    assert match.tax == 17_658
    # T0: the reference was found AND the arithmetic closes exactly.
    assert match.tier == "T0"


def test_a_remittance_short_by_one_paise_does_not_close_at_t0(adapter):
    """The negative control for the test above. If a credit that is one paise
    out still matched at T0, the T0 above would be proving nothing about the
    arithmetic."""
    from core.matcher.engine import run_match

    legs = adapter.parse(CLEAN).records
    credit = BankLine(
        line_id="HDFC-COD-0002",
        txn_date=date(2026, 8, 14),
        narration=f"NEFT CR-DELHIVERY LTD-COD REMITTANCE {SETTLEMENT_ID}",
        credit=EXPECTED_NET_PAISE - 1,
        debit=None,
        balance=EXPECTED_NET_PAISE - 1,
        utr=None,
    )
    result = run_match(orders=[], psp_txns=list(legs), bank_lines=[credit])
    assert [match.tier for match in result.matches] != ["T0"]
