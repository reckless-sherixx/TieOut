"""`--export-as razorpay`: the real-format writers, cell by cell.

Three claims live here, and the round-trip proof in
`tests/round_trip/test_round_trip.py` rests on all three:

* **the exported files are what each adapter's whitelist says they are** --
  Razorpay's column names and timestamp layout, HDFC's `dd/MM/yy` and its two
  amount columns, Shopify's header subset and its offset-bearing `Created at`;
* **`--export-as` ADDS files and changes nothing**, so the committed fixtures
  and every number measured on them stay valid;
* **byte-identity**, extended to the exported files, because a determinism
  claim that covered four of seven files would be worth nothing.

Expected cells are written out by hand from the schemas, never read back out of
the writer -- an assertion generated from the code under test asserts that the
code equals itself.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

import pytest

from core.generator.emit import emit_dataset
from core.generator.export import (
    EXPORTED_FILES,
    HDFC_FILE,
    RAZORPAY_FILE,
    SHOPIFY_FILE,
    ExportError,
    export_dataset,
    rupees,
)
from core.generator.pipeline import build_dataset
from core.models import BankLine, Order, PSPTransaction

CANONICAL_FILES = (
    "orders.csv",
    "psp.csv",
    "bank.csv",
    "psp_gst_invoice.csv",
    "truth.json",
)


@pytest.fixture
def exported(tmp_path) -> Path:
    batches, injections = build_dataset(seed=42, count=50)
    emit_dataset(batches, injections, out_dir=tmp_path, seed=42)
    export_dataset(batches, tmp_path)
    return tmp_path


def _rows(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# --- paise -> rupee decimals, exactly ---------------------------------------


@pytest.mark.parametrize(
    ("paise", "text"),
    [
        (0, "0.00"),
        (1, "0.01"),
        (100, "1.00"),
        (4_122_412, "41224.12"),
        (-880_247, "-8802.47"),
        (-1, "-0.01"),
        (10_000_000_000, "100000000.00"),
    ],
)
def test_every_paise_value_has_an_exact_rupee_rendering(paise, text):
    assert rupees(paise) == text


def test_the_rendering_never_loses_a_paise_across_a_wide_range():
    """The direction that has to be exact. `parse_paise` is its inverse and the
    round trip asserts the pair; this asserts the half that is arithmetic."""
    from core.adapters.base import parse_paise

    for paise in (0, 1, 99, 100, 101, 12_345_678, -12_345_678, 2**40):
        assert parse_paise(rupees(paise)) == paise


# --- the export adds files and changes nothing ------------------------------


def test_exporting_leaves_the_canonical_files_byte_identical(tmp_path):
    plain, with_export = tmp_path / "plain", tmp_path / "exported"
    batches, injections = build_dataset(seed=42, count=50)
    emit_dataset(batches, injections, out_dir=plain, seed=42)

    batches, injections = build_dataset(seed=42, count=50)
    emit_dataset(batches, injections, out_dir=with_export, seed=42)
    export_dataset(batches, with_export)

    for name in CANONICAL_FILES:
        assert (plain / name).read_bytes() == (with_export / name).read_bytes(), name


def test_the_export_writes_exactly_three_new_files(exported):
    assert sorted(path.name for path in exported.iterdir()) == sorted(
        [*CANONICAL_FILES, *EXPORTED_FILES]
    )


def test_the_same_seed_exports_byte_identical_real_format_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for out in (a, b):
        batches, injections = build_dataset(seed=42, count=100)
        emit_dataset(batches, injections, out_dir=out, seed=42)
        export_dataset(batches, out)
    for name in (*CANONICAL_FILES, *EXPORTED_FILES):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_the_exported_files_use_lf_line_endings(exported):
    for name in EXPORTED_FILES:
        raw = (exported / name).read_bytes()
        assert b"\r\n" not in raw, name
        assert raw.endswith(b"\n"), name


def test_exporting_twice_over_the_same_batches_is_idempotent(exported):
    """`export_dataset` re-chains the bank balances, so a second call over
    batches `emit_dataset` has already walked must not shift them."""
    before = {name: (exported / name).read_bytes() for name in EXPORTED_FILES}
    batches, injections = build_dataset(seed=42, count=50)
    export_dataset(batches, exported)
    for name in EXPORTED_FILES:
        assert (exported / name).read_bytes() == before[name], name


# --- the Razorpay settlement report -----------------------------------------


def test_the_settlement_report_carries_razorpays_own_column_names(exported):
    with open(exported / RAZORPAY_FILE, newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    assert header == [
        "entity_id",
        "type",
        "debit",
        "credit",
        "amount",
        "currency",
        "fee (exclusive tax)",
        "tax",
        "on_hold",
        "settled",
        "created_at",
        "settled_at",
        "settlement_id",
        "settlement_utr",
        "order_id",
        "order_receipt",
        "payment_id",
        "method",
    ]


def test_one_canonical_leg_is_one_report_row(exported):
    canonical = _rows(exported / "psp.csv")
    report = _rows(exported / RAZORPAY_FILE)
    assert len(report) == len(canonical)
    assert [row["entity_id"] for row in report] == [
        row["txn_id"] for row in canonical
    ]


def test_a_credit_leg_writes_its_magnitude_to_credit_and_a_debit_leg_to_debit(
    exported,
):
    canonical = {row["txn_id"]: row for row in _rows(exported / "psp.csv")}
    for row in _rows(exported / RAZORPAY_FILE):
        amount = int(canonical[row["entity_id"]]["amount"])
        assert row["amount"] == rupees(abs(amount))
        if amount > 0:
            assert (row["credit"], row["debit"]) == (rupees(amount), "0.00")
        else:
            assert (row["credit"], row["debit"]) == ("0.00", rupees(-amount))


def test_the_report_row_balances_against_itself_on_every_row(exported):
    """`amount - fee - tax == credit - debit`, which is the identity the adapter
    refuses a row for breaking."""
    from core.adapters.base import parse_paise

    for row in _rows(exported / RAZORPAY_FILE):
        amount = parse_paise(row["amount"])
        signed = amount if parse_paise(row["credit"]) else -amount
        legs = signed - parse_paise(row["fee (exclusive tax)"]) - parse_paise(row["tax"])
        assert legs == parse_paise(row["credit"]) - parse_paise(row["debit"])


def test_the_merchants_order_reference_goes_to_order_receipt(exported):
    canonical = {row["txn_id"]: row for row in _rows(exported / "psp.csv")}
    for row in _rows(exported / RAZORPAY_FILE):
        assert row["order_receipt"] == canonical[row["entity_id"]]["order_id"]
        assert row["order_id"] == ""


def test_timestamps_use_the_layout_the_adapter_whitelists(exported):
    from core.adapters.razorpay_settlement import TIMESTAMP_FORMATS

    for row in _rows(exported / RAZORPAY_FILE):
        assert datetime.strptime(row["created_at"], TIMESTAMP_FORMATS[0])


def test_a_reserve_leg_fails_the_export_rather_than_becoming_an_adjustment():
    """The layout has no row type for a reserve, and the neighbouring type means
    something else. `PSPTransaction.txn_type` carries `reserve`, the generator
    emits none today, and the day it does this must stop rather than approximate.
    """
    from core.generator.export import _razorpay_row

    leg = PSPTransaction(
        txn_id="rsv_000001",
        txn_type="reserve",
        order_id=None,
        captured_at=datetime(2026, 1, 5),
        amount=-50_000,
        settlement_id="setl_00001",
        settled_at=date(2026, 1, 5),
    )
    with pytest.raises(ExportError, match="no row type"):
        _razorpay_row(leg)


def test_a_zero_amount_leg_fails_the_export(exported):
    from core.generator.export import _razorpay_row

    leg = PSPTransaction(
        txn_id="adj_000001",
        txn_type="adjustment",
        order_id=None,
        captured_at=datetime(2026, 1, 5),
        amount=0,
        settlement_id="setl_00001",
        settled_at=date(2026, 1, 5),
    )
    with pytest.raises(ExportError, match="amount of zero"):
        _razorpay_row(leg)


# --- the HDFC statement -----------------------------------------------------


def test_the_statement_carries_hdfcs_own_column_names(exported):
    with open(exported / HDFC_FILE, newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    assert header == [
        "Date",
        "Narration",
        "Chq./Ref.No.",
        "Value Dt",
        "Withdrawal Amt.",
        "Deposit Amt.",
        "Closing Balance",
    ]


def test_statement_dates_are_two_digit_years(exported):
    for row in _rows(exported / HDFC_FILE):
        assert datetime.strptime(row["Date"], "%d/%m/%y")
        assert row["Value Dt"] == row["Date"]


def test_the_narration_is_written_verbatim(exported):
    """The obfuscated references, the doubled spaces and the trap's
    byte-identical narration are the dataset's defects. An export that tidied
    them would be measuring a different dataset."""
    canonical = {row["line_id"]: row for row in _rows(exported / "bank.csv")}
    for row in _rows(exported / HDFC_FILE):
        assert row["Narration"] == canonical[row["Chq./Ref.No."]]["narration"]


def test_the_canonical_line_id_is_the_statements_reference(exported):
    canonical = [row["line_id"] for row in _rows(exported / "bank.csv")]
    assert [row["Chq./Ref.No."] for row in _rows(exported / HDFC_FILE)] == canonical


def test_the_unused_amount_column_is_written_as_zero_not_blank(exported):
    for row in _rows(exported / HDFC_FILE):
        assert row["Withdrawal Amt."] and row["Deposit Amt."]
        assert (row["Withdrawal Amt."] == "0.00") != (row["Deposit Amt."] == "0.00")


def test_the_balance_chain_survives_the_export(exported):
    canonical = {row["line_id"]: row for row in _rows(exported / "bank.csv")}
    for row in _rows(exported / HDFC_FILE):
        assert row["Closing Balance"] == rupees(
            int(canonical[row["Chq./Ref.No."]]["balance"])
        )


def test_a_line_with_no_direction_fails_the_export():
    from core.generator.export import _hdfc_row

    line = BankLine(
        line_id="BL-0002",
        txn_date=date(2026, 1, 5),
        narration="NEFT CR PAYOUT",
        credit=None,
        debit=None,
        balance=10_000_000,
        utr=None,
    )
    with pytest.raises(ExportError, match="direction"):
        _hdfc_row(line)


def test_a_narration_carrying_a_comma_fails_the_export():
    from core.generator.export import _hdfc_row

    line = BankLine(
        line_id="BL-0002",
        txn_date=date(2026, 1, 5),
        narration="NEFT CR, RAZORPAY",
        credit=100,
        debit=None,
        balance=10_000_000,
        utr=None,
    )
    with pytest.raises(ExportError, match="comma"):
        _hdfc_row(line)


# --- the Shopify order export -----------------------------------------------


def test_the_order_export_is_a_subset_of_shopifys_documented_header(exported):
    from core.adapters.orders_shopify import ShopifyOrdersAdapter

    with open(exported / SHOPIFY_FILE, newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    assert set(ShopifyOrdersAdapter.REQUIRED_COLUMNS) <= set(header)
    assert set(ShopifyOrdersAdapter.DISTINCTIVE_COLUMNS) <= set(header)


def test_created_at_carries_the_offset_and_the_order_date_unconverted(exported):
    canonical = {row["order_id"]: row for row in _rows(exported / "orders.csv")}
    for row in _rows(exported / SHOPIFY_FILE):
        assert row["Created at"].endswith(" +0530")
        assert row["Created at"].startswith(canonical[row["Name"]]["order_date"])


def test_every_canonical_status_has_a_shopify_spelling(exported):
    from core.generator.export import STATUS_TO_FINANCIAL_STATUS
    from core.adapters.orders_shopify import STATUS_MAP
    from core.models import Order as OrderModel

    canonical_statuses = set(
        OrderModel.model_fields["status"].annotation.__args__  # type: ignore[union-attr]
    )
    assert set(STATUS_TO_FINANCIAL_STATUS) == canonical_statuses
    # And it is the exact inverse of the adapter's forward map.
    assert {v: k for k, v in STATUS_TO_FINANCIAL_STATUS.items()} == STATUS_MAP


def test_a_cancelled_order_is_written_as_voided():
    from core.generator.export import _shopify_row

    order = Order(
        order_id="ORD-004472",
        order_date=date(2026, 1, 4),
        customer_ref="CUST-35013",
        gross_amount=42_405_00,
        currency="INR",
        status="cancelled",
    )
    row = dict(zip(("Name", "Email", "Financial Status", "Paid at"), _shopify_row(order)))
    assert row["Financial Status"] == "voided"
    # No money ever moved, so there is no paid-at timestamp to write.
    assert row["Paid at"] == ""
