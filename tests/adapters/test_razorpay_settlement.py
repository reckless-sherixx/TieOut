"""`razorpay-settlement-v2` against the hand-written fixtures.

Every expected value in the clean-parse tests was computed by hand from the
fixture text, not read back out of the adapter. That is the only way a fixture
test proves anything: an assertion generated from the code under test asserts
that the code equals itself.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from core.adapters.base import QuarantineReason
from core.adapters.razorpay_settlement import RazorpaySettlementAdapter
from core.models import PSPTransaction

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "real-formats"
CLEAN = FIXTURES / "razorpay-settlement-clean.csv"
DIRTY = FIXTURES / "razorpay-settlement-dirty.csv"


@pytest.fixture
def adapter() -> RazorpaySettlementAdapter:
    return RazorpaySettlementAdapter()


def _by_id(result) -> dict[str, PSPTransaction]:
    return {record.txn_id: record for record in result.records}


# --- identity ---------------------------------------------------------------


def test_format_id_names_the_layout_not_just_the_vendor(adapter):
    assert adapter.format_id == "razorpay-settlement-v2"
    assert adapter.format_version == "2.0-per-transaction"


def test_the_adapter_is_registered_and_detected_from_the_header(adapter):
    from core.adapters import registry

    assert adapter.format_id in {a.format_id for a in registry.adapters()}
    assert registry.detect(CLEAN).format_id == adapter.format_id


def test_detection_ignores_a_misleading_filename(adapter, tmp_path: Path):
    from core.adapters import registry

    decoy = tmp_path / "hdfc-bank-statement-august.csv"
    decoy.write_bytes(CLEAN.read_bytes())
    assert registry.detect(decoy).format_id == "razorpay-settlement-v2"


def test_sniff_needs_a_fee_column_under_either_of_its_two_names(adapter):
    api_spelling = (
        b"entity_id,type,debit,credit,amount,currency,fee,tax,on_hold,settled,"
        b"created_at,settled_at,settlement_id,settlement_utr,order_id,"
        b"order_receipt,payment_id,method\n"
    )
    export_spelling = api_spelling.replace(b",fee,", b",fee (exclusive tax),")
    no_fee = api_spelling.replace(b",fee,", b",")
    assert adapter.sniff(api_spelling) >= 0.6
    assert adapter.sniff(export_spelling) >= 0.6
    assert adapter.sniff(no_fee) == 0.0


def test_sniff_scores_a_bank_statement_header_at_zero(adapter):
    hdfc = b"Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
    assert adapter.sniff(hdfc) == 0.0


# --- clean parse, cell by cell ---------------------------------------------


def test_clean_fixture_parses_with_zero_quarantine(adapter):
    result = adapter.parse(CLEAN)
    assert result.quarantined == []
    # 12 data rows: 8 payments (each -> payment + fee + tax = 24 records),
    # 2 refunds and 2 adjustments (1 record each, both fee and tax being zero).
    assert result.record_count == 28
    assert len(result.row_hashes) == 28
    assert result.encoding == "utf-8"


def test_a_payment_row_becomes_three_legs_that_sum_to_the_credit(adapter):
    """Row 1: amount 10000.05, fee 236.00, tax 42.48, credit 9721.57.

    10000.05 is deliberately the float trap: `int(float("10000.05") * 100)` is
    1000004, so a float pipeline would book this settlement a paise light.
    """
    records = _by_id(adapter.parse(CLEAN))
    payment = records["pay_RZP0000000001"]
    fee = records["fee_pay_RZP0000000001"]
    tax = records["tax_pay_RZP0000000001"]

    assert payment.txn_type == "payment"
    assert payment.amount == 1_000_005
    assert fee.txn_type == "fee"
    assert fee.amount == -23_600
    assert tax.txn_type == "tax"
    assert tax.amount == -4_248
    assert payment.amount + fee.amount + tax.amount == 972_157  # the row's credit


def test_a_payment_row_carries_the_settlement_and_the_merchant_order_ref(adapter):
    records = _by_id(adapter.parse(CLEAN))
    payment = records["pay_RZP0000000001"]

    assert payment.settlement_id == "setl_RZ2608030001"
    assert payment.settled_at == date(2026, 8, 3)
    assert payment.captured_at == datetime(2026, 8, 1, 9, 14, 22)
    # ORD-004471 is `order_receipt`, the merchant's own key -- not the
    # `order_RZO000000001` that only Razorpay can resolve.
    assert payment.order_id == "ORD-004471"


def test_every_leg_of_a_row_shares_that_rows_order_and_settlement(adapter):
    records = _by_id(adapter.parse(CLEAN))
    for txn_id in ("pay_RZP0000000002", "fee_pay_RZP0000000002", "tax_pay_RZP0000000002"):
        assert records[txn_id].order_id == "ORD-004472"
        assert records[txn_id].settlement_id == "setl_RZ2608030001"
        assert records[txn_id].captured_at == datetime(2026, 8, 1, 11, 2, 5)


def test_the_large_row_converts_to_paise_exactly(adapter):
    """Row 2: 46556.54 / 1098.73 / 197.77, credit 45260.04."""
    records = _by_id(adapter.parse(CLEAN))
    assert records["pay_RZP0000000002"].amount == 4_655_654
    assert records["fee_pay_RZP0000000002"].amount == -109_873
    assert records["tax_pay_RZP0000000002"].amount == -19_777
    assert (
        records["pay_RZP0000000002"].amount
        + records["fee_pay_RZP0000000002"].amount
        + records["tax_pay_RZP0000000002"].amount
    ) == 4_526_004


def test_a_refund_row_is_one_negative_leg(adapter):
    records = _by_id(adapter.parse(CLEAN))
    refund = records["rfnd_RZR000000001"]
    assert refund.txn_type == "refund"
    assert refund.amount == -50_000
    assert refund.order_id == "ORD-004473"
    assert "fee_rfnd_RZR000000001" not in records  # zero fee is not a fee leg
    assert "tax_rfnd_RZR000000001" not in records


def test_an_adjustment_keeps_the_sign_of_the_column_it_landed_in(adapter):
    records = _by_id(adapter.parse(CLEAN))
    assert records["adj_RZA000000001"].txn_type == "adjustment"
    assert records["adj_RZA000000001"].amount == -12_000  # debit 120.00
    assert records["adj_RZA000000002"].amount == 25_000  # credit 250.00


def test_a_row_with_no_order_reference_yields_a_null_order_id(adapter):
    records = _by_id(adapter.parse(CLEAN))
    assert records["adj_RZA000000001"].order_id is None


def test_the_second_batch_settles_a_day_later(adapter):
    records = _by_id(adapter.parse(CLEAN))
    assert records["pay_RZP0000000008"].settlement_id == "setl_RZ2608040002"
    assert records["pay_RZP0000000008"].settled_at == date(2026, 8, 4)
    assert records["pay_RZP0000000008"].amount == 33_333
    assert records["fee_pay_RZP0000000008"].amount == -787
    assert records["tax_pay_RZP0000000008"].amount == -142


def test_every_settlement_batch_nets_to_the_sum_of_its_legs(adapter):
    """The whole point of splitting a row into legs: a batch's net is a plain
    sum. Hand-computed from the fixture, batch by batch.

    setl_RZ2608030001: 9721.57 + 45260.04 + 1215.19 + 873.96 - 500.00
                       + 14582.28 = 71153.04
    setl_RZ2608040002: -120.00 + 2430.37 - 899.00 + 7583.27 + 250.00
                       + 324.04 = 9568.68
    """
    totals: dict[str, int] = {}
    for record in adapter.parse(CLEAN).records:
        totals[record.settlement_id] = totals.get(record.settlement_id, 0) + record.amount
    assert totals == {"setl_RZ2608030001": 7_115_304, "setl_RZ2608040002": 956_868}


# --- quarantine matrix ------------------------------------------------------

#: Physical line number in `razorpay-settlement-dirty.csv` -> expected reason.
#: Six comment lines, header on line 7, data on lines 8..21.
EXPECTED_QUARANTINE = {
    9: QuarantineReason.BAD_DECIMAL,  # tax "4.255" -- sub-paise
    10: QuarantineReason.MISSING_VALUE,  # amount is empty
    11: QuarantineReason.TRUNCATED_ROW,  # line cut short at 8 fields
    12: QuarantineReason.BAD_DATE,  # created_at "05/08/2026 09:20:00"
    14: QuarantineReason.DUPLICATE_ROW,  # same entity_id as line 13
    15: QuarantineReason.UNSUPPORTED_ROW_TYPE,  # type "transfer"
    16: QuarantineReason.UNKNOWN_VALUE,  # type "wibble"
    17: QuarantineReason.ARITHMETIC_MISMATCH,  # credit 486.00, legs make 486.08
    18: QuarantineReason.AMBIGUOUS_DIRECTION,  # debit and credit both 100.00
    20: QuarantineReason.EXTRA_FIELDS,  # unquoted comma in order_receipt
}


def test_every_malformed_row_lands_with_the_right_reason(adapter):
    result = adapter.parse(DIRTY)
    actual = {q.row_number: q.reason for q in result.quarantined}
    assert actual == EXPECTED_QUARANTINE


def test_the_clean_rows_of_the_dirty_file_still_parse(adapter):
    """The load-bearing half. Ten broken rows must not cost the four good ones:
    lines 8, 13, 19 and 21 give three legs, three legs, one refund and three
    legs -- ten records."""
    result = adapter.parse(DIRTY)
    records = _by_id(result)
    assert result.record_count == 10
    assert records["pay_RZP0000000101"].amount == 100_000
    assert records["fee_pay_RZP0000000101"].amount == -2_360
    assert records["tax_pay_RZP0000000101"].amount == -425
    assert records["pay_RZP0000000106"].amount == 200_000
    assert records["rfnd_RZR000000101"].amount == -75_000
    assert records["pay_RZP0000000113"].amount == 64_000


def test_quarantine_carries_the_raw_line_and_a_detail_naming_the_value(adapter):
    quarantined = {q.row_number: q for q in adapter.parse(DIRTY).quarantined}

    bad_decimal = quarantined[9]
    assert bad_decimal.raw.startswith("pay_RZP0000000102,")
    assert "4.255" in bad_decimal.detail
    assert "tax" in bad_decimal.detail

    duplicate = quarantined[14]
    assert "line 13" in duplicate.detail

    mismatch = quarantined[17]
    assert "48608" in mismatch.detail and "48600" in mismatch.detail

    unsupported = quarantined[15]
    assert "transfer" in unsupported.detail


def test_nothing_is_dropped_silently(adapter):
    """14 data rows in, 14 accounted for: 4 parsed, 10 quarantined, 0 skipped."""
    result = adapter.parse(DIRTY)
    parsed_rows = 4
    assert parsed_rows + result.quarantine_count + result.skipped_rows == 14


def test_a_sub_paise_value_is_quarantined_and_never_rounded(adapter):
    """The row whose tax is 4.255 must not appear anywhere in the output --
    not as 4.25, not as 4.26, not at all."""
    result = adapter.parse(DIRTY)
    ids = {record.txn_id for record in result.records}
    assert "pay_RZP0000000102" not in ids
    assert "tax_pay_RZP0000000102" not in ids
    assert any(
        q.reason is QuarantineReason.BAD_DECIMAL and "4.255" in q.detail
        for q in result.quarantined
    )


# --- idempotency ------------------------------------------------------------


def test_the_file_hash_is_the_hash_of_the_bytes(adapter):
    import hashlib

    result = adapter.parse(CLEAN)
    assert result.file_sha256 == hashlib.sha256(CLEAN.read_bytes()).hexdigest()


def test_hashes_are_stable_across_repeated_parses(adapter):
    first = adapter.parse(CLEAN)
    second = RazorpaySettlementAdapter().parse(CLEAN)
    assert first.file_sha256 == second.file_sha256
    assert first.row_hashes == second.row_hashes


def test_row_hashes_are_unique_across_the_clean_fixture(adapter):
    result = adapter.parse(CLEAN)
    assert len(set(result.row_hashes)) == len(result.row_hashes)


def test_a_copy_of_the_file_hashes_identically(adapter, tmp_path: Path):
    copy = tmp_path / "renamed-export.csv"
    copy.write_bytes(CLEAN.read_bytes())
    assert adapter.parse(copy).file_sha256 == adapter.parse(CLEAN).file_sha256
    assert adapter.parse(copy).row_hashes == adapter.parse(CLEAN).row_hashes


# --- file-level hardening ---------------------------------------------------


def test_a_missing_required_column_is_a_file_level_quarantine_not_a_crash(
    adapter, tmp_path: Path
):
    path = tmp_path / "short-header.csv"
    path.write_text(
        "entity_id,type,amount\npay_1,payment,100.00\n", encoding="utf-8"
    )
    result = adapter.parse(path)
    assert result.records == []
    assert len(result.quarantined) == 1
    assert result.quarantined[0].reason is QuarantineReason.MISSING_HEADER_COLUMN
    assert "credit" in result.quarantined[0].detail


def test_an_undecodable_file_raises_the_file_level_error(adapter, tmp_path: Path):
    from core.adapters.base import UndecodableFileError

    path = tmp_path / "not-a-csv.csv"
    path.write_bytes(b"PK\x03\x04\x00\x00\x00\x00binary junk")
    with pytest.raises(UndecodableFileError):
        adapter.parse(path)


def test_a_bom_prefixed_export_parses_identically(adapter, tmp_path: Path):
    """Excel writes the BOM back on every CSV it touches, so a merchant who
    opened the export before uploading it sends a BOM'd file."""
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbf" + CLEAN.read_bytes())
    result = adapter.parse(path)
    assert result.encoding == "utf-8-sig"
    assert result.quarantined == []
    assert result.record_count == 28


# --- settlement-level fee and GST rows (documented extension) ---------------

_LEDGER_HEADER = (
    "entity_id,type,debit,credit,amount,currency,fee (exclusive tax),tax,"
    "on_hold,settled,created_at,settled_at,settlement_id,settlement_utr,"
    "order_id,order_receipt,payment_id,method\n"
)


def _ledger(rows: list[str]) -> str:
    return _LEDGER_HEADER + "\n".join(rows) + "\n"


def test_a_fee_row_becomes_one_fee_leg_carrying_its_own_id(adapter, tmp_path):
    path = tmp_path / "fee.csv"
    path.write_text(
        _ledger(
            [
                "fee_001003,fee,1000.75,0.00,1000.75,INR,0.00,0.00,N,Y,"
                "2026-01-05 00:00:00,2026-01-05,setl_00001,,,,,",
            ]
        ),
        encoding="utf-8",
    )

    result = adapter.parse(path)

    assert not result.quarantined
    (leg,) = result.records
    assert leg.txn_id == "fee_001003"
    assert leg.txn_type == "fee"
    assert leg.amount == -100_075
    assert leg.order_id is None
    assert leg.settlement_id == "setl_00001"


def test_a_tax_row_becomes_one_tax_leg(adapter, tmp_path):
    path = tmp_path / "tax.csv"
    path.write_text(
        _ledger(
            [
                "tax_001004,tax,180.13,0.00,180.13,INR,0.00,0.00,N,Y,"
                "2026-01-05 00:00:00,2026-01-05,setl_00001,,,,,",
            ]
        ),
        encoding="utf-8",
    )

    (leg,) = adapter.parse(path).records
    assert (leg.txn_id, leg.txn_type, leg.amount) == ("tax_001004", "tax", -18_013)


def test_the_two_extension_types_are_not_claimed_as_documented_razorpay_values():
    """The extension is recorded as an extension, not smuggled into the schema
    claim. `VALIDATION.md` reads this distinction off the module."""
    from core.adapters.razorpay_settlement import DOCUMENTED_TYPES, TYPE_MAP

    assert set(DOCUMENTED_TYPES) == {"payment", "refund", "transfer", "adjustment"}
    assert set(TYPE_MAP) - set(DOCUMENTED_TYPES) == {"fee", "tax", "dispute"}


def test_a_transfer_is_still_quarantined_after_the_extension(adapter, tmp_path):
    path = tmp_path / "transfer.csv"
    path.write_text(
        _ledger(
            [
                "trf_001,transfer,100.00,0.00,100.00,INR,0.00,0.00,N,Y,"
                "2026-01-05 00:00:00,2026-01-05,setl_00001,,,,,",
            ]
        ),
        encoding="utf-8",
    )

    result = adapter.parse(path)
    assert not result.records
    assert result.quarantined[0].reason is QuarantineReason.UNSUPPORTED_ROW_TYPE
