"""Ingest is the only place raw CSV text becomes typed money.

Every rule asserted here is from docs/CSV_SCHEMAS.md 1: amounts are integer
paise with no decimal point, an absent optional value is the empty string
(never "None"/"NULL"/"-"), and narration whitespace is data.
"""

from datetime import date, datetime
from pathlib import Path

import pytest

from core.ingest.reader import read_bank, read_orders, read_psp

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "tiny"

PSP_HEADER = (
    "txn_id,txn_type,order_id,captured_at,amount,settlement_id,settled_at\n"
)


def test_reads_fixture_without_loss():
    assert len(read_orders(FIX / "orders.csv")) > 0
    assert len(read_psp(FIX / "psp.csv")) > 0
    assert len(read_bank(FIX / "bank.csv")) > 0


def test_reads_every_fixture_row():
    assert len(read_orders(FIX / "orders.csv")) == 12
    assert len(read_psp(FIX / "psp.csv")) == 27
    assert len(read_bank(FIX / "bank.csv")) == 6


def test_amounts_parse_as_int_paise():
    for t in read_psp(FIX / "psp.csv"):
        assert isinstance(t.amount, int)
    for o in read_orders(FIX / "orders.csv"):
        assert isinstance(o.gross_amount, int)
    for b in read_bank(FIX / "bank.csv"):
        assert isinstance(b.balance, int)


def test_signed_psp_amounts_survive_ingest():
    by_id = {t.txn_id: t for t in read_psp(FIX / "psp.csv")}
    assert by_id["pay_1001"].amount == 1_250_000
    assert by_id["fee_1001"].amount == -116_395
    assert by_id["rfnd_2001"].amount == -890_000


def test_empty_optional_fields_become_none():
    by_id = {t.txn_id: t for t in read_psp(FIX / "psp.csv")}
    # the missing_order_ref defect: pay_1104 carries an empty order_id
    assert by_id["pay_1104"].order_id is None
    # the duplicate sits outside every settlement
    assert by_id["pay_1105"].settlement_id is None
    assert by_id["pay_1105"].settled_at is None
    # settlement-level legs carry an empty order_id by design
    assert by_id["fee_1001"].order_id is None


def test_bank_credit_and_debit_are_unsigned_and_optional():
    lines = {b.line_id: b for b in read_bank(FIX / "bank.csv")}
    assert lines["BL-0001"].credit == 4_794_654
    assert lines["BL-0001"].debit is None
    assert lines["BL-0003"].utr is None


def test_narration_whitespace_is_preserved_verbatim():
    lines = {b.line_id: b for b in read_bank(FIX / "bank.csv")}
    assert lines["BL-0003"].narration == "RZPX*ACME  RET PL"
    assert lines["BL-0005"].narration == "NEFT CR   PAYOUT"


def test_dates_and_datetimes_are_typed():
    orders = {o.order_id: o for o in read_orders(FIX / "orders.csv")}
    assert orders["ORD-004471"].order_date == date(2026, 6, 1)
    by_id = {t.txn_id: t for t in read_psp(FIX / "psp.csv")}
    assert by_id["pay_1001"].captured_at == datetime(2026, 6, 1, 9, 41, 12)
    assert by_id["pay_1001"].settled_at == date(2026, 6, 3)


def test_rejects_decimal_amounts(tmp_path):
    bad = tmp_path / "psp.csv"
    bad.write_text(
        PSP_HEADER
        + "pay_1,payment,ORD-1,2026-08-01T10:00:00,100.50,setl_A,2026-08-03\n"
    )
    with pytest.raises(ValueError, match="integer paise"):
        read_psp(bad)


def test_decimal_error_names_the_row_and_the_column(tmp_path):
    bad = tmp_path / "psp.csv"
    bad.write_text(
        PSP_HEADER
        + "pay_1,payment,ORD-1,2026-08-01T10:00:00,100000,setl_A,2026-08-03\n"
        + "pay_2,payment,ORD-2,2026-08-01T10:00:00,100.50,setl_A,2026-08-03\n"
    )
    with pytest.raises(ValueError) as exc:
        read_psp(bad)
    message = str(exc.value)
    assert "amount" in message
    assert "3" in message  # the file line number, header included
    assert "pay_2" in message


def test_rejects_decimal_amounts_in_orders_and_bank(tmp_path):
    bad_orders = tmp_path / "orders.csv"
    bad_orders.write_text(
        "order_id,order_date,customer_ref,gross_amount,currency,status\n"
        "ORD-1,2026-06-01,CUST-1,1250.00,INR,paid\n"
    )
    with pytest.raises(ValueError, match="integer paise"):
        read_orders(bad_orders)

    bad_bank = tmp_path / "bank.csv"
    bad_bank.write_text(
        "line_id,txn_date,narration,credit,debit,balance,utr\n"
        "BL-1,2026-06-03,NEFT CR,4794.65,,14794654,\n"
    )
    with pytest.raises(ValueError, match="integer paise"):
        read_bank(bad_bank)


def test_rejects_non_numeric_amount(tmp_path):
    bad = tmp_path / "psp.csv"
    bad.write_text(
        PSP_HEADER
        + "pay_1,payment,ORD-1,2026-08-01T10:00:00,NULL,setl_A,2026-08-03\n"
    )
    with pytest.raises(ValueError, match="integer paise"):
        read_psp(bad)


def test_rejects_a_missing_required_column(tmp_path):
    bad = tmp_path / "psp.csv"
    bad.write_text(
        "txn_id,txn_type,order_id,captured_at,settlement_id,settled_at\n"
        "pay_1,payment,ORD-1,2026-08-01T10:00:00,setl_A,2026-08-03\n"
    )
    with pytest.raises(ValueError, match="amount"):
        read_psp(bad)


def test_a_required_amount_may_not_be_empty(tmp_path):
    bad = tmp_path / "bank.csv"
    bad.write_text(
        "line_id,txn_date,narration,credit,debit,balance,utr\n"
        "BL-1,2026-06-03,NEFT CR,4794654,,,\n"
    )
    with pytest.raises(ValueError, match="balance"):
        read_bank(bad)


def test_crlf_line_endings_parse_identically(tmp_path):
    """.gitattributes pins fixture CSVs to LF, but Git may still check them out
    as CRLF on Windows. newline="" + csv.DictReader must handle both."""
    crlf = tmp_path / "psp.csv"
    crlf.write_bytes(
        (
            PSP_HEADER
            + "pay_1,payment,ORD-1,2026-08-01T10:00:00,100000,setl_A,2026-08-03\n"
        )
        .replace("\n", "\r\n")
        .encode("utf-8")
    )
    txns = read_psp(crlf)
    assert len(txns) == 1
    assert txns[0].amount == 100_000
    assert txns[0].settlement_id == "setl_A"
