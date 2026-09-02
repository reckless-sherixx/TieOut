"""`bank-csv-hdfc-v1` against the hand-written fixtures.

Every expected value was computed by hand from the fixture text.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from core.adapters.bank_hdfc import HDFCStatementAdapter, extract_utr
from core.adapters.base import QuarantineReason

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "real-formats"
CLEAN = FIXTURES / "hdfc-statement-clean.csv"
DIRTY = FIXTURES / "hdfc-statement-dirty.csv"


@pytest.fixture
def adapter() -> HDFCStatementAdapter:
    return HDFCStatementAdapter()


def _by_id(result) -> dict:
    return {record.line_id: record for record in result.records}


# --- identity and detection -------------------------------------------------


def test_format_id_names_the_bank(adapter):
    assert adapter.format_id == "bank-csv-hdfc-v1"
    assert adapter.format_version == "1.0"


def test_the_adapter_is_registered_and_detected_from_the_header(adapter):
    from core.adapters import registry

    assert adapter.format_id in {a.format_id for a in registry.adapters()}
    assert registry.detect(CLEAN).format_id == adapter.format_id


def test_an_icici_statement_does_not_detect_as_hdfc(adapter):
    from core.adapters import registry

    icici = FIXTURES / "icici-statement-clean.csv"
    assert registry.detect(icici).format_id != adapter.format_id
    assert adapter.sniff(icici.read_bytes()[:8192]) == 0.0


def test_a_razorpay_report_does_not_detect_as_hdfc(adapter):
    report = FIXTURES / "razorpay-settlement-clean.csv"
    assert adapter.sniff(report.read_bytes()[:8192]) == 0.0


# --- clean parse, cell by cell ---------------------------------------------


def test_clean_fixture_parses_with_zero_quarantine(adapter):
    result = adapter.parse(CLEAN)
    assert result.quarantined == []
    assert result.record_count == 12
    assert len(result.row_hashes) == 12


def test_the_first_credit_line_cell_by_cell(adapter):
    """Line 9 of the file: 8 comment lines, header on 9... no -- 8 comment
    lines put the header on line 9 and the first data row on line 10."""
    lines = _by_id(adapter.parse(CLEAN))
    first = lines["HDFC-00010"]
    assert first.txn_date == date(2026, 8, 1)
    assert first.narration == "NEFT CR-RAZORPAY SOFTWARE PVT LTD-N260803HDFC0000001"
    assert first.credit == 7_115_304
    assert first.debit is None
    assert first.balance == 32_115_304
    assert first.utr == "N260803HDFC0000001"


def test_a_debit_line_populates_debit_and_leaves_credit_null(adapter):
    lines = _by_id(adapter.parse(CLEAN))
    second = lines["HDFC-00011"]
    assert second.txn_date == date(2026, 8, 2)
    assert second.debit == 45_000
    assert second.credit is None
    assert second.balance == 32_070_304


def test_the_unused_side_is_a_literal_zero_and_reads_as_absent(adapter):
    """HDFC writes 0.00 rather than leaving the cell blank, so "absent" here is
    an exact zero. Every line has exactly one populated side."""
    for line in adapter.parse(CLEAN).records:
        assert (line.credit is None) != (line.debit is None)
        assert line.credit != 0 and line.debit != 0


def test_grouped_thousands_and_a_negative_balance_convert_exactly(adapter):
    """Last line: withdrawal "2,00,000.00" in Indian grouping, closing balance
    "-8,802.47" -- an overdraft, which is a real state and stays signed."""
    lines = _by_id(adapter.parse(CLEAN))
    last = lines["HDFC-00021"]
    assert last.txn_date == date(2026, 8, 12)
    assert last.debit == 20_000_000
    assert last.balance == -880_247


def test_the_running_balance_of_the_fixture_closes(adapter):
    """Not an adapter rule -- a check that the hand-written fixture is coherent,
    which is what makes the cell-level assertions above worth anything."""
    records = adapter.parse(CLEAN).records
    balance = 25_000_000  # opening balance, implied by row 1
    for line in records:
        balance += (line.credit or 0) - (line.debit or 0)
        assert balance == line.balance, f"{line.line_id} breaks the running balance"
    assert balance == -880_247


def test_narration_whitespace_is_not_normalised_here(adapter, tmp_path: Path):
    """Doubled spaces inside a narration are data; the canonicaliser downstream
    owns normalising them and has its own tests about exactly that."""
    path = tmp_path / "spaced.csv"
    path.write_text(
        "Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        "01/08/26,NEFT  CR-RAZORPAY   SOFTWARE-N260803HDFC0000001,X,01/08/26,0.00,100.00,100.00\n",
        encoding="utf-8",
    )
    (line,) = adapter.parse(path).records
    assert line.narration == "NEFT  CR-RAZORPAY   SOFTWARE-N260803HDFC0000001"


# --- UTR extraction ---------------------------------------------------------


@pytest.mark.parametrize(
    "narration,expected",
    [
        ("NEFT CR-RAZORPAY SOFTWARE PVT LTD-N260803HDFC0000001", "N260803HDFC0000001"),
        ("UPI-SWIGGY-swiggy@axis-YBL260802000012345", "YBL260802000012345"),
        ("RTGS DR-HDFC0000456-SUPPLIER LTD-UTR: HDFCR52026080600123", "HDFCR52026080600123"),
        # Not an instrument narration at all: no reference to lift.
        ("ATM WDL 0001234 MUMBAI ANDHERI", None),
        ("BANK CHARGES GST", None),
        ("CHQ PAID 000123", None),
        # An instrument narration whose reference is NOT in trailing position.
        # Returning None here is the deliberate choice: the alternative is a
        # regex loose enough to return "VENDOR PAYOUT" somewhere else.
        ("IMPS-260805123456-VENDOR PAYOUT", None),
        # A trailing word with no digit is a word, not a reference.
        ("NEFT CR-HDFC0000123-RAZORPAY SOFTWARE", None),
    ],
)
def test_utr_extraction_is_narrow_and_predictable(narration, expected):
    assert extract_utr(narration) == expected


def test_the_fixture_yields_utrs_only_where_hdfc_puts_one(adapter):
    lines = _by_id(adapter.parse(CLEAN))
    assert lines["HDFC-00010"].utr == "N260803HDFC0000001"
    assert lines["HDFC-00013"].utr is None  # ATM withdrawal
    assert lines["HDFC-00014"].utr is None  # IMPS, reference mid-narration
    assert lines["HDFC-00015"].utr == "HDFCR52026080600123"  # explicit UTR marker
    assert sum(1 for line in lines.values() if line.utr is None) == 5


# --- quarantine matrix ------------------------------------------------------

#: Physical line number in `hdfc-statement-dirty.csv` -> expected reason.
#: Five comment lines, header on line 6, data on lines 7..18.
EXPECTED_QUARANTINE = {
    8: QuarantineReason.BAD_DECIMAL,  # deposit "1234.567" -- sub-paise
    9: QuarantineReason.MISSING_VALUE,  # narration is empty
    10: QuarantineReason.TRUNCATED_ROW,  # line cut short at 3 fields
    11: QuarantineReason.BAD_DATE,  # ISO "2026-08-05", not dd/MM/yy
    13: QuarantineReason.DUPLICATE_ROW,  # byte-identical to line 12
    14: QuarantineReason.AMBIGUOUS_DIRECTION,  # both sides 250.00
    15: QuarantineReason.AMBIGUOUS_DIRECTION,  # both sides 0.00
    16: QuarantineReason.EXTRA_FIELDS,  # unquoted comma in the narration
}


def test_every_malformed_row_lands_with_the_right_reason(adapter):
    result = adapter.parse(DIRTY)
    assert {q.row_number: q.reason for q in result.quarantined} == EXPECTED_QUARANTINE


def test_the_clean_rows_of_the_dirty_file_still_parse(adapter):
    result = adapter.parse(DIRTY)
    lines = _by_id(result)
    assert result.record_count == 4
    assert set(lines) == {"HDFC-00007", "HDFC-00012", "HDFC-00017", "HDFC-00018"}
    assert lines["HDFC-00007"].credit == 7_115_304
    assert lines["HDFC-00012"].credit == 450_000
    assert lines["HDFC-00017"].debit == 67_525
    assert lines["HDFC-00018"].utr == "HDFCR52026081100456"


def test_quarantine_details_name_the_column_and_the_value(adapter):
    quarantined = {q.row_number: q for q in adapter.parse(DIRTY).quarantined}
    assert "1234.567" in quarantined[8].detail
    assert "Deposit Amt." in quarantined[8].detail
    assert "Narration" in quarantined[9].detail
    assert "3 field" in quarantined[10].detail
    assert "2026-08-05" in quarantined[11].detail
    assert "line 12" in quarantined[13].detail
    assert quarantined[16].raw.startswith("09/08/26,POS PURCHASE MERCHANT")


def test_nothing_is_dropped_silently(adapter):
    """12 data rows in, 12 accounted for."""
    result = adapter.parse(DIRTY)
    assert 4 + result.quarantine_count + result.skipped_rows == 12


def test_the_sub_paise_deposit_is_quarantined_and_never_rounded(adapter):
    result = adapter.parse(DIRTY)
    assert all(line.credit != 123_456 and line.credit != 123_457 for line in result.records)
    assert "HDFC-00008" not in _by_id(result)


# --- idempotency ------------------------------------------------------------


def test_hashes_are_stable_across_parses(adapter):
    import hashlib

    first = adapter.parse(CLEAN)
    second = HDFCStatementAdapter().parse(CLEAN)
    assert first.file_sha256 == hashlib.sha256(CLEAN.read_bytes()).hexdigest()
    assert first.row_hashes == second.row_hashes
    assert len(set(first.row_hashes)) == 12


def test_the_dirty_file_hashes_differently_from_the_clean_one(adapter):
    assert adapter.parse(CLEAN).file_sha256 != adapter.parse(DIRTY).file_sha256


# --- file-level hardening ---------------------------------------------------


def test_a_header_missing_a_money_column_is_a_file_level_quarantine(
    adapter, tmp_path: Path
):
    path = tmp_path / "short.csv"
    path.write_text("Date,Narration,Closing Balance\n01/08/26,X,100.00\n", encoding="utf-8")
    result = adapter.parse(path)
    assert result.records == []
    assert result.quarantined[0].reason is QuarantineReason.MISSING_HEADER_COLUMN
    assert "Withdrawal Amt." in result.quarantined[0].detail


def test_a_bom_prefixed_statement_parses_identically(adapter, tmp_path: Path):
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbf" + CLEAN.read_bytes())
    result = adapter.parse(path)
    assert result.encoding == "utf-8-sig"
    assert result.quarantined == []
    assert result.record_count == 12


def test_a_binary_file_raises_the_file_level_error_not_a_traceback(
    adapter, tmp_path: Path
):
    from core.adapters.base import UndecodableFileError

    path = tmp_path / "statement.csv"
    path.write_bytes(b"PK\x03\x04\x00\x00\x00\x00\x08\x00")
    with pytest.raises(UndecodableFileError):
        adapter.parse(path)


# --- carried line identity --------------------------------------------------
#
# The all-or-nothing rule: a statement that gives every line its own distinct
# `Chq./Ref.No.` names its lines; anything less and the whole file is
# positional. Both halves are asserted, and the committed fixture is the
# negative case -- it repeats `0000000000`, which is what a real export does.

_HEADER = (
    "Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,"
    "Closing Balance\n"
)


def _statement(refs: list[str]) -> str:
    rows = []
    balance = 100000
    for index, ref in enumerate(refs, start=1):
        balance += 100
        rows.append(
            f"0{index}/08/26,NEFT CR-RAZORPAY SOFTWARE PVT LTD,{ref},"
            f"0{index}/08/26,0.00,100.00,{balance}.00"
        )
    return _HEADER + "\n".join(rows) + "\n"


def test_a_statement_that_names_every_line_gives_those_names_to_the_records(
    adapter, tmp_path
):
    path = tmp_path / "named.csv"
    path.write_text(_statement(["BL-0002", "BL-0003", "BL-0139"]), encoding="utf-8")

    result = adapter.parse(path)

    assert not result.quarantined
    assert [line.line_id for line in result.records] == ["BL-0002", "BL-0003", "BL-0139"]


def test_one_blank_reference_makes_the_whole_file_positional(adapter, tmp_path):
    path = tmp_path / "one-blank.csv"
    path.write_text(_statement(["BL-0002", "", "BL-0139"]), encoding="utf-8")

    result = adapter.parse(path)

    assert [line.line_id for line in result.records] == [
        "HDFC-00002",
        "HDFC-00003",
        "HDFC-00004",
    ]


def test_one_repeated_reference_makes_the_whole_file_positional(adapter, tmp_path):
    path = tmp_path / "repeat.csv"
    path.write_text(_statement(["BL-0002", "BL-0002", "BL-0139"]), encoding="utf-8")

    result = adapter.parse(path)

    assert [line.line_id for line in result.records] == [
        "HDFC-00002",
        "HDFC-00003",
        "HDFC-00004",
    ]


def test_a_real_looking_statement_keeps_positional_ids(adapter):
    """The committed fixture repeats `0000000000`, so nothing changes for it."""
    result = HDFCStatementAdapter().parse(CLEAN)

    assert all(line.line_id.startswith("HDFC-") for line in result.records)


def test_the_scan_leaves_no_state_behind_between_files(adapter, tmp_path):
    named = tmp_path / "named.csv"
    named.write_text(_statement(["BL-0002", "BL-0003"]), encoding="utf-8")

    assert [line.line_id for line in adapter.parse(named).records] == [
        "BL-0002",
        "BL-0003",
    ]
    assert all(
        line.line_id.startswith("HDFC-") for line in adapter.parse(CLEAN).records
    )
