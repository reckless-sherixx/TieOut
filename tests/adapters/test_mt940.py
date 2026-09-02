"""`mt940-v1` against the hand-written fixtures.

Every expected value was computed by hand from the fixture text, and the
fixtures state their own arithmetic in their comment headers so the two can be
compared without running anything.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from core.adapters.base import QuarantineReason
from core.adapters.mt940 import MT940Adapter, extract_utr, parse_mt940_amount

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "real-formats"
CLEAN = FIXTURES / "mt940-statement-clean.sta"
DIRTY = FIXTURES / "mt940-statement-dirty.sta"
UNBALANCED = FIXTURES / "mt940-unbalanced.sta"


@pytest.fixture
def adapter() -> MT940Adapter:
    return MT940Adapter()


def _by_id(result) -> dict:
    return {record.line_id: record for record in result.records}


# --- the decimal comma ------------------------------------------------------


def test_the_decimal_separator_is_a_comma_not_a_grouping_separator():
    """The single most dangerous confusion in this format.

    `342614,53` is 342,614.53 rupees. An HDFC CSV writing `342,614.53` means
    the same number with the comma doing the opposite job, and handing MT940
    text to the CSV amount parser turns three lakh rupees into three crore.
    """
    assert parse_mt940_amount("342614,53") == 34_261_453
    assert parse_mt940_amount("450,00") == 45_000
    assert parse_mt940_amount("1000,") == 100_000
    assert parse_mt940_amount("7") == 700


@pytest.mark.parametrize(
    "raw",
    [
        "342614.53",  # a decimal POINT: this is not MT940
        "1,234,56",  # two commas
        "1234,567",  # sub-paise
        "",
        "abc",
        ",50",
        "-100,00",
    ],
)
def test_amounts_that_are_not_exact_mt940_amounts_are_refused(raw):
    with pytest.raises(ValueError):
        parse_mt940_amount(raw)


def test_sub_paise_is_refused_by_name_not_rounded():
    with pytest.raises(ValueError, match="sub-paise"):
        parse_mt940_amount("1234,567")


# --- identity and detection -------------------------------------------------


def test_format_id_names_the_standard_not_a_bank(adapter):
    """MT940 is a published standard, so one adapter covers every bank that
    emits it -- the opposite of the one-adapter-per-layout rule that governs
    the undocumented CSV exports."""
    assert adapter.format_id == "mt940-v1"
    assert adapter.format_version == "1.0"


def test_the_adapter_is_registered_and_detected_from_its_tags(adapter):
    from core.adapters import registry

    assert adapter.format_id in {a.format_id for a in registry.adapters()}
    assert registry.detect(CLEAN).format_id == adapter.format_id
    assert adapter.sniff(CLEAN.read_bytes()[:8192]) == pytest.approx(1.0)


def test_a_csv_export_scores_zero_and_an_mt940_scores_zero_on_the_csv_adapters(adapter):
    from core.adapters.bank_hdfc import HDFCStatementAdapter
    from core.adapters.bank_icici import ICICIStatementAdapter
    from core.adapters.razorpay_settlement import RazorpaySettlementAdapter

    head = CLEAN.read_bytes()[:8192]
    for csv_adapter in (
        HDFCStatementAdapter(),
        ICICIStatementAdapter(),
        RazorpaySettlementAdapter(),
    ):
        assert csv_adapter.sniff(head) == 0.0
        csv_head = (FIXTURES / "hdfc-statement-clean.csv").read_bytes()[:8192]
        assert adapter.sniff(csv_head) == 0.0


# --- the clean statement, cell by cell -------------------------------------


def test_clean_fixture_parses_with_zero_quarantine(adapter):
    result = adapter.parse(CLEAN)
    assert result.quarantined == []
    assert result.record_count == 8
    assert len(result.row_hashes) == 8
    assert result.skipped_rows == 0


def test_the_first_credit_line_cell_by_cell(adapter):
    lines = _by_id(adapter.parse(CLEAN))
    first = lines["MT940-00020"]
    assert first.txn_date == date(2026, 8, 14)
    assert first.narration == "NEFT CR-RAZORPAY SOFTWARE PVT LTD-N260803HDFC00001"
    assert first.credit == 7_115_304
    assert first.debit is None
    # opening 25000000 + 7115304
    assert first.balance == 32_115_304
    assert first.utr == "HDFCR52026081401"


def test_the_funds_code_is_not_part_of_the_mark(adapter):
    """`CR71153,04` is mark C plus funds code R, and `D450,00` is mark D with
    no funds code. Reading the mark as a fixed width gets one of the two
    wrong, so both spellings appear in the fixture and both are asserted."""
    lines = _by_id(adapter.parse(CLEAN))
    assert lines["MT940-00020"].credit == 7_115_304  # C + funds code R
    assert lines["MT940-00022"].debit == 45_000  # D, no funds code
    assert lines["MT940-00022"].credit is None


def test_a_wrapped_narration_is_rejoined_across_its_continuation_line(adapter):
    lines = _by_id(adapter.parse(CLEAN))
    wrapped = lines["MT940-00028"]
    assert wrapped.narration == (
        "IMPS-260816123456-VENDOR PAYOUT AGAINST INVOICE INV-2026-0881 "
        "RAISED BY SUPPLIER LTD ON 12 AUGUST 2026 UTR: HDFCR52026081605"
    )
    assert wrapped.debit == 2_550_050


def test_a_line_with_no_bank_reference_has_no_utr(adapter):
    """`:61:` without `//` carries only the sender's own reference, which is
    not a UTR. A null is the correct answer and a guess is not."""
    lines = _by_id(adapter.parse(CLEAN))
    assert lines["MT940-00026"].utr is None  # ATM withdrawal, no `//` reference
    assert lines["MT940-00033"].utr is None  # cheque paid


def test_every_balance_is_the_running_chain(adapter):
    """The per-line balance is derived, not read: MT940 prints only the opening
    and closing figures. Walking the emitted balances is what proves the
    derivation, and it is the reason a broken line costs the whole statement."""
    records = adapter.parse(CLEAN).records
    opening = 25_000_000
    running = opening
    for record in records:
        running += (record.credit or 0) - (record.debit or 0)
        assert record.balance == running, record.line_id
    assert running == 34_261_453  # the :62F: the fixture declares


def test_the_closing_balance_the_file_declares_is_the_last_running_balance(adapter):
    assert adapter.parse(CLEAN).records[-1].balance == 34_261_453


# --- the chain that does not close -----------------------------------------


def test_a_statement_whose_chain_does_not_close_is_quarantined_whole(adapter):
    result = adapter.parse(UNBALANCED)
    assert result.records == []
    assert result.row_hashes == []
    assert [q.reason for q in result.quarantined] == [
        QuarantineReason.ARITHMETIC_MISMATCH
    ]
    # The two readable lines were not emitted, and they are counted so that
    # nothing is silently dropped.
    assert result.skipped_rows == 2


def test_the_unbalanced_quarantine_names_both_figures(adapter):
    """A reason that says "does not balance" is a support ticket. A reason that
    says which two numbers disagree, and by how much, is a fix."""
    detail = adapter.parse(UNBALANCED).quarantined[0].detail
    assert "6500000" in detail, detail  # what the chain computes
    assert "6510000" in detail, detail  # what :62F: declares
    assert "10000" in detail, detail  # the difference


def test_the_unbalanced_file_is_a_file_level_quarantine(adapter):
    """It holds exactly one statement, so statement scope IS file scope here.
    The record points at the `:62F:` line, which is where a human looks."""
    result = adapter.parse(UNBALANCED)
    quarantine = result.quarantined[0]
    assert quarantine.raw.startswith(":62F:")
    assert result.record_count == 0


# --- the dirty statement ----------------------------------------------------


def test_dirty_fixture_still_yields_the_clean_statements_lines(adapter):
    """Statement 1 is a write-off; statement 2 is untouched by it. That
    separation is the whole reason statements are scoped independently."""
    result = adapter.parse(DIRTY)
    assert result.record_count == 2
    assert [record.line_id for record in result.records] == [
        "MT940-00033",
        "MT940-00035",
    ]
    assert result.records[0].credit == 500_000
    assert result.records[0].balance == 10_500_000
    assert result.records[1].debit == 120_050
    assert result.records[1].balance == 10_379_950


def test_every_defect_in_the_broken_statement_is_named_by_line_and_reason(adapter):
    result = adapter.parse(DIRTY)
    by_line = {q.row_number: q.reason for q in result.quarantined}
    assert by_line == {
        16: QuarantineReason.BAD_DECIMAL,  # 1234,567 -- sub-paise
        18: QuarantineReason.MISSING_VALUE,  # no :86:, no customer reference
        19: QuarantineReason.TRUNCATED_ROW,  # `:61:26080`
        20: QuarantineReason.BAD_DATE,  # value date 269901
        24: QuarantineReason.DUPLICATE_ROW,  # byte-identical to lines 22-23
        26: QuarantineReason.UNKNOWN_VALUE,  # mark `E` is not C/D/RC/RD
        12: QuarantineReason.ARITHMETIC_MISMATCH,  # the statement-level record
    }


def test_the_statement_level_record_says_why_the_readable_lines_were_dropped(adapter):
    result = adapter.parse(DIRTY)
    statement_level = [
        q
        for q in result.quarantined
        if q.reason is QuarantineReason.ARITHMETIC_MISMATCH
    ]
    assert len(statement_level) == 1
    detail = statement_level[0].detail
    assert "6 of 7" in detail, detail
    assert "cannot be verified" in detail
    # One line of statement 1 parsed and was still not emitted. It is counted.
    assert result.skipped_rows == 1


def test_nothing_from_the_broken_statement_reaches_the_records(adapter):
    result = adapter.parse(DIRTY)
    assert all(record.txn_date >= date(2026, 8, 20) for record in result.records)


# --- UTR extraction ---------------------------------------------------------


def test_the_bank_reference_after_the_double_slash_is_the_utr():
    assert extract_utr("HDFCR52026081401", "anything") == "HDFCR52026081401"


def test_an_explicit_marker_in_the_narration_is_used_when_there_is_no_bank_reference():
    assert extract_utr(None, "IMPS PAYOUT UTR: HDFCR52026081605") == "HDFCR52026081605"


@pytest.mark.parametrize(
    "bank_reference,narration",
    [
        (None, "ATM WDL 0001234 MUMBAI ANDHERI"),
        ("", "CHQ PAID 000123"),
        ("SHORT", "no marker here"),
        ("NOREFERENCEATALL", "no digits in that reference"),
    ],
)
def test_a_reference_that_is_not_one_yields_none_rather_than_a_guess(
    bank_reference, narration
):
    assert extract_utr(bank_reference, narration) is None


# --- hardening --------------------------------------------------------------


def test_an_empty_file_is_a_quarantine_record_not_a_traceback(adapter, tmp_path: Path):
    path = tmp_path / "empty.sta"
    path.write_bytes(b"")
    result = adapter.parse(path)
    assert result.records == []
    assert result.quarantined[0].reason is QuarantineReason.MISSING_HEADER_COLUMN


def test_a_statement_missing_its_closing_balance_emits_nothing(adapter, tmp_path: Path):
    path = tmp_path / "no-closing.sta"
    path.write_text(
        ":20:STMT1\n:25:ACC\n:28C:1/1\n:60F:C260801INR1000,00\n"
        ":61:2608010801C100,00NTRFREF1//BANKREF000001\n:86:A CREDIT\n",
        encoding="utf-8",
    )
    result = adapter.parse(path)
    assert result.records == []
    assert result.quarantined[0].reason is QuarantineReason.MISSING_VALUE
    assert ":62F:" in result.quarantined[0].detail
    assert result.skipped_rows == 1


def test_a_non_inr_statement_is_quarantined_rather_than_converted(
    adapter, tmp_path: Path
):
    path = tmp_path / "usd.sta"
    path.write_text(
        ":20:STMT1\n:25:ACC\n:28C:1/1\n:60F:C260801USD1000,00\n"
        ":61:2608010801C100,00NTRFREF1//BANKREF000001\n:86:A CREDIT\n"
        ":62F:C260801USD1100,00\n",
        encoding="utf-8",
    )
    result = adapter.parse(path)
    assert result.records == []
    assert result.quarantined[0].reason is QuarantineReason.UNKNOWN_VALUE
    assert "INR-only" in result.quarantined[0].detail


def test_a_reversal_mark_inverts_the_sign(adapter, tmp_path: Path):
    """`RC` reverses a credit, so it is a debit. A reversal read as a plain
    credit doubles the very error it was posted to correct, and the balance
    chain is what catches the mistake: this statement only closes if RC and RD
    are signed the way the standard says."""
    path = tmp_path / "reversals.sta"
    path.write_text(
        ":20:STMTREV\n:25:ACC\n:28C:1/1\n:60F:C260801INR1000,00\n"
        ":61:2608010801RC100,00NTRFREVERSEDCREDIT//BANKREF000001\n"
        ":86:REVERSAL OF A CREDIT\n"
        ":61:2608010801RD250,00NTRFREVERSEDDEBIT//BANKREF000002\n"
        ":86:REVERSAL OF A DEBIT\n"
        ":62F:C260801INR1150,00\n",
        encoding="utf-8",
    )
    result = adapter.parse(path)
    assert result.quarantined == []
    assert result.records[0].debit == 10_000
    assert result.records[0].credit is None
    assert result.records[1].credit == 25_000
    assert result.records[1].balance == 115_000


def test_an_overdrawn_opening_balance_is_signed_not_refused(adapter, tmp_path: Path):
    path = tmp_path / "overdrawn.sta"
    path.write_text(
        ":20:STMTOD\n:25:ACC\n:28C:1/1\n:60F:D260801INR500,00\n"
        ":61:2608010801C100,00NTRFREF1//BANKREF000001\n:86:A CREDIT\n"
        ":62F:D260801INR400,00\n",
        encoding="utf-8",
    )
    result = adapter.parse(path)
    assert result.quarantined == []
    assert result.records[0].balance == -40_000


def test_parsing_twice_is_identical(adapter):
    first = adapter.parse(CLEAN)
    second = adapter.parse(CLEAN)
    assert first.row_hashes == second.row_hashes
    assert first.file_sha256 == second.file_sha256
    assert [r.model_dump() for r in first.records] == [
        r.model_dump() for r in second.records
    ]
