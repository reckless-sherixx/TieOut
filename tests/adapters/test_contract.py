"""The adapter contract: money, encodings, quarantine, hashes, registry.

These are the format-independent guarantees. Per-adapter behaviour lives in
`test_razorpay_settlement.py`, `test_bank_hdfc.py` and `test_bank_icici.py`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.adapters.base import (
    AdapterResult,
    FormatDetectionError,
    QuarantinedRow,
    QuarantineReason,
    UndecodableFileError,
    decode_bytes,
    parse_paise,
    read_text,
    row_fingerprint,
    sha256_bytes,
)

# --- exact decimal money ----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("46556.54", 4_655_654),
        ("0.00", 0),
        ("0", 0),
        ("1", 100),
        ("1.5", 150),
        ("1.50", 150),
        ("-12.34", -1_234),
        ("+12.34", 1_234),
        ("1,23,456.78", 12_345_678),  # Indian digit grouping, as banks export it
        ("1,234.05", 123_405),
        (" 99.99 ", 9_999),
        ("₹250.00", 25_000),
        ("1234.5", 123_450),
    ],
)
def test_parse_paise_is_exact(raw, expected):
    assert parse_paise(raw) == expected


def test_parse_paise_never_uses_float():
    """The float route is wrong on values that are perfectly ordinary rupees.

    "10000.05" is a settlement amount a merchant could see any day. The nearest
    double to it, times 100, is 1000004.9999999999 -- so the obvious
    `int(float(x) * 100)` pipeline books it as ₹10,000.04 and the run is a paise
    short before matching even starts. This is not a rounding preference; it is
    a wrong number. `parse_paise` goes through `Decimal` and gets 1000005.
    """
    assert int(float("10000.05") * 100) == 1_000_004  # the trap, asserted to exist
    assert parse_paise("10000.05") == 1_000_005


def test_parse_paise_refuses_the_sub_paise_float_trap():
    """"1.005" is the other half: floats make it 100.49999999999999, so a float
    pipeline silently books 100 paise. There is no exact answer, so this layer
    returns none -- the row goes to quarantine where a human decides."""
    assert float("1.005") * 100 == pytest.approx(100.5, abs=1e-9)
    assert float("1.005") * 100 != 100.5
    with pytest.raises(ValueError):
        parse_paise("1.005")


@pytest.mark.parametrize(
    "raw",
    [
        "46556.545",  # third decimal place -- sub-paise, cannot be exact
        "1.999",
        "",
        "   ",
        "abc",
        "12.3x",
        "1..2",
        "-",
        "NULL",
        "1.2.3",
        "1e3",  # exponent notation is not how a bank writes rupees
        "nan",
        "Infinity",
    ],
)
def test_parse_paise_refuses_inexact_or_junk(raw):
    with pytest.raises(ValueError):
        parse_paise(raw)


def test_parse_paise_refuses_rather_than_rounds():
    """The load-bearing one. 46556.545 rupees is 4655654.5 paise. There is no
    integer answer, so there must be no answer -- rounding it silently loses
    half a paise per row across a 40,000-row export."""
    with pytest.raises(ValueError) as excinfo:
        parse_paise("46556.545")
    assert "46556.545" in str(excinfo.value)


def test_parse_paise_keeps_full_precision_on_large_values():
    assert parse_paise("99999999.99") == 9_999_999_999


# --- encodings --------------------------------------------------------------


def test_decode_bytes_handles_plain_utf8():
    text, encoding = decode_bytes("Narration,Amt\nUPI/₹\n".encode("utf-8"))
    assert encoding == "utf-8"
    assert text.startswith("Narration")


def test_decode_bytes_strips_utf8_bom():
    payload = "﻿Date,Narration\n".encode("utf-8")
    text, encoding = decode_bytes(payload)
    assert encoding == "utf-8-sig"
    assert text.startswith("Date,")
    assert "﻿" not in text


def test_decode_bytes_falls_back_to_latin1():
    payload = "Date,Narration\n01/08/26,CAF\xc9 PURCHASE\n".encode("latin-1")
    text, encoding = decode_bytes(payload)
    assert encoding == "latin-1"
    assert "CAF\xc9" in text


def test_decode_bytes_raises_on_undecodable_input():
    with pytest.raises(UndecodableFileError):
        decode_bytes(b"\x00\x00\x00\x00PK\x03\x04\x00\x00\x00\x00")


def test_read_text_reports_the_file_level_failure_rather_than_crashing(tmp_path: Path):
    path = tmp_path / "broken.csv"
    path.write_bytes(b"\x00\x00\x00\x00PK\x03\x04\x00\x00\x00\x00")
    with pytest.raises(UndecodableFileError):
        read_text(path)


# --- hashes / idempotency ---------------------------------------------------


def test_sha256_bytes_matches_hashlib():
    assert sha256_bytes(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_row_fingerprint_is_stable_and_format_scoped():
    from core.models import BankLine

    line = BankLine(
        line_id="HDFC-00002",
        txn_date="2026-08-01",
        narration="NEFT CR-RAZORPAY",
        credit=100,
        debit=None,
        balance=100,
        utr="N26080100001",
    )
    a = row_fingerprint("bank-csv-hdfc-v1", line)
    b = row_fingerprint("bank-csv-hdfc-v1", line)
    c = row_fingerprint("bank-csv-icici-v1", line)
    assert a == b
    assert a != c
    assert len(a) == 64


# --- quarantine record ------------------------------------------------------


def test_quarantined_row_carries_raw_reason_and_row_number():
    q = QuarantinedRow(
        row_number=41,
        raw="01/08/26,JUNK,,,,",
        reason=QuarantineReason.BAD_DECIMAL,
        detail="column 'Deposit Amt.' value '12.3x'",
    )
    assert q.row_number == 41
    assert q.raw.startswith("01/08/26")
    assert q.reason is QuarantineReason.BAD_DECIMAL
    assert "12.3x" in q.detail


def test_quarantine_reasons_are_stable_strings():
    assert QuarantineReason.BAD_DECIMAL.value == "BAD_DECIMAL"
    assert QuarantineReason.UNDECODABLE_FILE.value == "UNDECODABLE_FILE"


def test_adapter_result_counts_and_hash_parity():
    result = AdapterResult(
        format_id="bank-csv-hdfc-v1",
        format_version="1.0",
        records=[],
        quarantined=[],
        file_sha256="0" * 64,
        row_hashes=[],
        encoding="utf-8",
    )
    assert result.record_count == 0
    assert result.quarantine_count == 0
    with pytest.raises(ValueError):
        AdapterResult(
            format_id="x",
            format_version="1.0",
            records=[],
            quarantined=[],
            file_sha256="0" * 64,
            row_hashes=["a" * 64],
            encoding="utf-8",
        )


# --- header shape helpers ---------------------------------------------------


def test_header_confidence_is_zero_without_every_required_column():
    from core.adapters.base import header_confidence

    head = b"Date,Narration,Closing Balance\n"
    assert header_confidence(head, ("date", "narration", "deposit amt."), ()) == 0.0


def test_header_confidence_folds_whitespace_but_not_punctuation():
    from core.adapters.base import header_confidence, normalise_header

    assert normalise_header("Deposit Amount (INR )") == normalise_header(
        "deposit amount (inr)"
    )
    assert normalise_header("Chq./Ref.No.") != normalise_header("Chq/Ref No")

    spaced = b"Deposit Amount (INR ),Balance (INR)\n"
    assert header_confidence(spaced, ("Deposit Amount (INR)",), ()) == pytest.approx(0.7)
    assert header_confidence(spaced, ("Deposit Amount INR",), ()) == 0.0


def test_header_confidence_rewards_distinctive_columns():
    from core.adapters.base import header_confidence

    head = b"Date,Narration,Chq./Ref.No.\n"
    low = header_confidence(head, ("Date",), ("Narration", "Chq./Ref.No.", "Value Dt"))
    high = header_confidence(head, ("Date",), ("Narration", "Chq./Ref.No."))
    assert low < high == pytest.approx(1.0)


def test_leading_comment_lines_are_provenance_and_do_not_shift_the_header():
    from core.adapters.base import header_cells

    head = b"# hand-written from the published schema\n# see ADAPTERS-REPORT.md\nDate,Narration\n"
    assert header_cells(head) == ["date", "narration"]


# --- registry: the detection algorithm --------------------------------------
#
# Exercised against synthetic adapters, deliberately. What is under test here is
# "highest score wins, refuse below threshold, refuse on a tie" -- not whether
# HDFC's header looks like HDFC, which is that adapter's own test to own.


class _Stub:
    def __init__(self, format_id: str, score: float) -> None:
        self.format_id = format_id
        self.format_version = "1.0"
        self._score = score

    def sniff(self, head: bytes) -> float:
        return self._score

    def parse(self, path: Path) -> AdapterResult:  # pragma: no cover
        raise AssertionError("parse must not run during detection")


def _file(tmp_path: Path, name: str = "anything.csv") -> Path:
    path = tmp_path / name
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    return path


def test_detect_returns_the_highest_confidence_adapter(tmp_path: Path):
    from core.adapters import registry

    chosen = registry.detect(
        _file(tmp_path),
        adapters=[_Stub("low", 0.65), _Stub("high", 0.95), _Stub("mid", 0.7)],
    )
    assert chosen.format_id == "high"


def test_detect_refuses_below_the_threshold_and_names_every_candidate(tmp_path: Path):
    from core.adapters import registry

    with pytest.raises(FormatDetectionError) as excinfo:
        registry.detect(
            _file(tmp_path), adapters=[_Stub("alpha", 0.4), _Stub("beta", 0.55)]
        )
    message = str(excinfo.value)
    assert "alpha=0.40" in message
    assert "beta=0.55" in message
    assert "header" in message


def test_detect_refuses_a_tie(tmp_path: Path):
    from core.adapters import registry

    with pytest.raises(FormatDetectionError) as excinfo:
        registry.detect(
            _file(tmp_path), adapters=[_Stub("twin-a", 0.9), _Stub("twin-b", 0.9)]
        )
    message = str(excinfo.value)
    assert "tie" in message.lower()
    assert "twin-a" in message
    assert "twin-b" in message


def test_detect_ignores_the_filename_entirely(tmp_path: Path):
    """The stub scores on nothing at all, so if a name could sway detection this
    is where it would show. It cannot: `sniff` has no path parameter."""
    from core.adapters import registry

    misleading = _file(tmp_path, "hdfc-statement-august.csv")
    assert registry.detect(misleading, adapters=[_Stub("bank-csv-icici-v1", 0.9)]).format_id == (
        "bank-csv-icici-v1"
    )


def test_detect_on_an_undecodable_file_raises_the_file_level_error(tmp_path: Path):
    from core.adapters import registry

    path = tmp_path / "binary.csv"
    path.write_bytes(b"\x00\x00\x00\x00PK\x03\x04\x00\x00\x00\x00")
    with pytest.raises(UndecodableFileError):
        registry.detect(path, adapters=[_Stub("anything", 0.99)])


def test_detect_refuses_an_empty_registry(tmp_path: Path):
    from core.adapters import registry

    with pytest.raises(FormatDetectionError):
        registry.detect(_file(tmp_path), adapters=[])
