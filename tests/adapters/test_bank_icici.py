"""`bank-csv-icici-v1` against the hand-written fixtures.

Every expected value was computed by hand from the fixture text.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from core.adapters.bank_icici import ICICIStatementAdapter, extract_utr
from core.adapters.base import QuarantineReason

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "real-formats"
CLEAN = FIXTURES / "icici-statement-clean.csv"
DIRTY = FIXTURES / "icici-statement-dirty.csv"
LATIN1 = FIXTURES / "icici-statement-latin1.csv"


@pytest.fixture
def adapter() -> ICICIStatementAdapter:
    return ICICIStatementAdapter()


def _by_id(result) -> dict:
    return {record.line_id: record for record in result.records}


# --- identity and detection -------------------------------------------------


def test_format_id_names_the_bank(adapter):
    assert adapter.format_id == "bank-csv-icici-v1"
    assert adapter.format_version == "1.0"


def test_the_adapter_is_registered_and_detected_from_the_header(adapter):
    from core.adapters import registry

    assert adapter.format_id in {a.format_id for a in registry.adapters()}
    assert registry.detect(CLEAN).format_id == adapter.format_id


def test_an_hdfc_statement_does_not_detect_as_icici(adapter):
    hdfc = FIXTURES / "hdfc-statement-clean.csv"
    assert adapter.sniff(hdfc.read_bytes()[:8192]) == 0.0


def test_the_trailing_space_inside_inr_does_not_change_detection(adapter):
    padded = (
        b"S No.,Value Date,Transaction Date,Cheque Number,Transaction Remarks,"
        b"Withdrawal Amount (INR ),Deposit Amount (INR ),Balance (INR )\n"
    )
    tight = padded.replace(b"(INR )", b"(INR)")
    assert adapter.sniff(padded) == adapter.sniff(tight) >= 0.6


# --- clean parse, cell by cell ---------------------------------------------


def test_clean_fixture_parses_with_zero_quarantine(adapter):
    result = adapter.parse(CLEAN)
    assert result.quarantined == []
    assert result.record_count == 12
    assert len(result.row_hashes) == 12


def test_the_first_credit_line_cell_by_cell(adapter):
    """11 comment lines put the header on line 12 and the first data row on 13."""
    lines = _by_id(adapter.parse(CLEAN))
    first = lines["ICICI-00013"]
    assert first.txn_date == date(2026, 8, 1)
    assert first.narration == "NEFT-N260803ICIC0000001-RAZORPAY SOFTWARE PVT LTD"
    assert first.credit == 7_115_304
    assert first.debit is None
    assert first.balance == 57_115_304
    assert first.utr == "N260803ICIC0000001"


def test_txn_date_is_the_transaction_date_not_the_value_date(adapter):
    """Row 3 of the fixture exists for this assertion alone: its Value Date is
    02-08-2026 and its Transaction Date is 03-08-2026. Reading the wrong column
    shifts a line into the wrong day and, at a month boundary, the wrong
    statement period."""
    lines = _by_id(adapter.parse(CLEAN))
    assert lines["ICICI-00015"].txn_date == date(2026, 8, 3)


def test_a_cr_balance_is_positive_and_a_dr_balance_is_negative(adapter):
    """The DR/CR convention HDFC does not have. The last line is 8802.47 Dr:
    an overdraft. Dropping the suffix would book it as a credit of the same
    size -- the most expensive single mistake available in this adapter."""
    lines = _by_id(adapter.parse(CLEAN))
    assert lines["ICICI-00013"].balance == 57_115_304  # "571153.04 Cr"
    assert lines["ICICI-00024"].balance == -880_247  # "8802.47 Dr"


def test_the_running_balance_of_the_fixture_closes(adapter):
    records = adapter.parse(CLEAN).records
    balance = 50_000_000  # opening balance, implied by row 1
    for line in records:
        balance += (line.credit or 0) - (line.debit or 0)
        assert balance == line.balance, f"{line.line_id} breaks the running balance"
    assert balance == -880_247


def test_every_line_has_exactly_one_populated_side(adapter):
    for line in adapter.parse(CLEAN).records:
        assert (line.credit is None) != (line.debit is None)


# --- UTR extraction ---------------------------------------------------------


@pytest.mark.parametrize(
    "remarks,expected",
    [
        ("NEFT-N260803ICIC0000001-RAZORPAY SOFTWARE PVT LTD", "N260803ICIC0000001"),
        ("UPI/260802123456/Payment to Swiggy", "260802123456"),
        ("RTGS-ICICR52026080600123-SUPPLIER LTD", "ICICR52026080600123"),
        # The one that separates the two banks' rules: ICICI puts the reference
        # in the SECOND field, so this parses where the HDFC extractor returns
        # None on the same text.
        ("IMPS-260805123456-VENDOR PAYOUT", "260805123456"),
        ("EMI DEBIT LOAN00998877 UTR: ICIC260812000777", "ICIC260812000777"),
        ("ATM CASH WDL MUMBAI 0001234", None),
        ("CHEQUE PAID 000123", None),
        ("BANK CHARGES GST 18 PCT", None),
    ],
)
def test_utr_extraction_follows_iciciS_own_remark_grammar(remarks, expected):
    assert extract_utr(remarks) == expected


def test_the_two_bank_extractors_are_deliberately_different():
    """Not an accident to be tidied away later: HDFC's reference is trailing,
    ICICI's is the second field. One shared regex would have to be loose enough
    for both, which makes it wrong on each."""
    from core.adapters.bank_hdfc import extract_utr as hdfc_utr

    shared_text = "IMPS-260805123456-VENDOR PAYOUT"
    assert hdfc_utr(shared_text) is None
    assert extract_utr(shared_text) == "260805123456"


# --- quarantine matrix ------------------------------------------------------

#: Physical line number in `icici-statement-dirty.csv` -> expected reason.
#: Five comment lines, header on line 6, data on lines 7..18.
EXPECTED_QUARANTINE = {
    8: QuarantineReason.BAD_DECIMAL,  # deposit "9568.685" -- sub-paise
    9: QuarantineReason.MISSING_VALUE,  # Transaction Remarks is empty
    10: QuarantineReason.TRUNCATED_ROW,  # line cut short at 5 fields
    11: QuarantineReason.BAD_DATE,  # "03/08/2026", not dd-mm-yyyy
    13: QuarantineReason.DUPLICATE_ROW,  # byte-identical to line 12
    14: QuarantineReason.AMBIGUOUS_DIRECTION,  # both sides 250.00
    15: QuarantineReason.UNKNOWN_VALUE,  # "450.00 Dr" in an amount column
    16: QuarantineReason.EXTRA_FIELDS,  # unquoted comma in the remarks
}


def test_every_malformed_row_lands_with_the_right_reason(adapter):
    result = adapter.parse(DIRTY)
    assert {q.row_number: q.reason for q in result.quarantined} == EXPECTED_QUARANTINE


def test_a_cr_dr_suffix_on_an_amount_column_is_refused_not_absorbed(adapter):
    """Only the balance carries direction in this layout. A suffix on an amount
    means the file is not the layout it looks like, and reading it as 450.00
    would hide that."""
    quarantined = {q.row_number: q for q in adapter.parse(DIRTY).quarantined}
    detail = quarantined[15].detail
    assert "450.00 Dr" in detail
    assert "Balance (INR)" in detail


def test_the_clean_rows_of_the_dirty_file_still_parse(adapter):
    result = adapter.parse(DIRTY)
    lines = _by_id(result)
    assert result.record_count == 4
    assert set(lines) == {"ICICI-00007", "ICICI-00012", "ICICI-00017", "ICICI-00018"}
    assert lines["ICICI-00007"].credit == 7_115_304
    assert lines["ICICI-00012"].credit == 450_000
    assert lines["ICICI-00017"].debit == 67_525
    assert lines["ICICI-00018"].utr == "ICICR52026081100456"


def test_quarantine_details_name_the_column_and_the_value(adapter):
    quarantined = {q.row_number: q for q in adapter.parse(DIRTY).quarantined}
    assert "9568.685" in quarantined[8].detail
    assert "Transaction Remarks" in quarantined[9].detail
    assert "03/08/2026" in quarantined[11].detail
    assert "line 12" in quarantined[13].detail


def test_nothing_is_dropped_silently(adapter):
    result = adapter.parse(DIRTY)
    assert 4 + result.quarantine_count + result.skipped_rows == 12


# --- encodings --------------------------------------------------------------


def test_a_latin1_export_is_read_rather_than_rejected(adapter):
    """The committed latin-1 fixture is not valid UTF-8. Older net-banking
    backends really do ship this, and refusing it would put a merchant's whole
    statement in file-level quarantine over two accented characters."""
    with pytest.raises(UnicodeDecodeError):
        LATIN1.read_bytes().decode("utf-8")

    result = adapter.parse(LATIN1)
    assert result.encoding == "latin-1"
    assert result.quarantined == []
    assert result.record_count == 4
    narrations = [line.narration for line in result.records]
    assert "POS PURCHASE CAF\xc9 MOCHA BANDRA" in narrations
    assert "POS PURCHASE CR\xc8CHE FEES" in narrations


def test_the_latin1_fixture_still_converts_money_exactly(adapter):
    lines = _by_id(adapter.parse(LATIN1))
    assert {line.balance for line in lines.values()} == {
        57_115_304,
        57_070_304,
        56_950_304,
        57_907_172,
    }


# --- idempotency ------------------------------------------------------------


def test_hashes_are_stable_across_parses(adapter):
    import hashlib

    first = adapter.parse(CLEAN)
    second = ICICIStatementAdapter().parse(CLEAN)
    assert first.file_sha256 == hashlib.sha256(CLEAN.read_bytes()).hexdigest()
    assert first.row_hashes == second.row_hashes
    assert len(set(first.row_hashes)) == 12


def test_row_fingerprints_are_scoped_by_format(adapter):
    """An HDFC line and an ICICI line that happened to canonicalise identically
    must not collide in a dedup table: provenance is part of a row's identity."""
    from core.adapters.base import row_fingerprint
    from core.models import BankLine

    line = BankLine(
        line_id="X-1",
        txn_date="2026-08-01",
        narration="SAME",
        credit=100,
        debit=None,
        balance=100,
        utr=None,
    )
    assert row_fingerprint("bank-csv-hdfc-v1", line) != row_fingerprint(
        "bank-csv-icici-v1", line
    )
