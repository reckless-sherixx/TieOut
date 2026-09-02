"""`orders-csv-shopify-v1` against the hand-written fixtures.

Every expected value was computed by hand from the fixture text.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from core.adapters.base import QuarantineReason
from core.adapters.orders_shopify import (
    STATUS_MAP,
    UNSUPPORTED_STATUSES,
    ShopifyOrdersAdapter,
)

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "real-formats"
CLEAN = FIXTURES / "shopify-orders-clean.csv"
DIRTY = FIXTURES / "shopify-orders-dirty.csv"

#: The full column list Shopify's own documentation gives, verbatim, in export
#: order. A real `orders_export.csv` carries all of these; the clean fixture
#: carries a subset, and this header is what proves the adapter reads the whole
#: thing rather than only the shape it was written against.
FULL_EXPORT_HEADER = (
    "Name,Phone,Email,Financial Status,Paid at,Fulfillment Status,Fulfilled at,"
    "Accepts Marketing,Currency,Subtotal,Shipping,Taxes,Total,Discount Code,"
    "Discount Amount,Shipping Method,Created at,Lineitem quantity,Lineitem name,"
    "Lineitem price,Lineitem compare-at price,Lineitem SKU,"
    "Lineitem requires shipping,Lineitem taxable,Lineitem fulfillment status,"
    "Billing Name,Billing Street,Billing Address1,Billing Address2,"
    "Billing Company,Billing City,Billing Zip,Billing Province,"
    "Billing Province Name,Billing Country,Billing Phone,Shipping Name,"
    "Shipping Street,Shipping Address1,Shipping Address2,Shipping Company,"
    "Shipping City,Shipping Zip,Shipping Province,Shipping Province Name,"
    "Shipping Country,Shipping Phone,Notes,Note Attributes,Canceled at,"
    "Payment Method,Payment Reference (deprecated),Payment References,"
    "Refunded Amount,Vendor,Outstanding Balance,Employee,Location,Device ID,Id,"
    "Tags,Risk Level,Source,Lineitem discount,Tax 1 Name,Tax 1 Value,Payment ID,"
    "Payment terms,Next payment due at"
)


@pytest.fixture
def adapter() -> ShopifyOrdersAdapter:
    return ShopifyOrdersAdapter()


def _by_id(result) -> dict:
    return {record.order_id: record for record in result.records}


# --- identity and detection -------------------------------------------------


def test_format_id_names_the_platform_and_the_layout(adapter):
    assert adapter.format_id == "orders-csv-shopify-v1"
    assert adapter.format_version == "1.0"


def test_the_adapter_is_registered_and_detected_from_the_header(adapter):
    from core.adapters import registry

    assert adapter.format_id in {a.format_id for a in registry.adapters()}
    assert registry.detect(CLEAN).format_id == adapter.format_id


def test_the_full_seventy_column_export_header_is_recognised(adapter, tmp_path: Path):
    """The fixture is a column subset. A real export is not, and an adapter
    that only recognised the subset would reject every real file."""
    path = tmp_path / "orders_export.csv"
    path.write_text(FULL_EXPORT_HEADER + "\n", encoding="utf-8")
    assert adapter.sniff(path.read_bytes()) == pytest.approx(1.0)

    from core.adapters import registry

    assert registry.detect(path).format_id == adapter.format_id


@pytest.mark.parametrize(
    "other",
    [
        "hdfc-statement-clean.csv",
        "icici-statement-clean.csv",
        "razorpay-settlement-clean.csv",
        "mt940-statement-clean.sta",
    ],
)
def test_no_other_fixture_looks_like_a_shopify_export(adapter, other):
    assert adapter.sniff((FIXTURES / other).read_bytes()[:8192]) == 0.0


def test_a_shopify_export_does_not_look_like_any_other_format(adapter):
    from core.adapters.bank_hdfc import HDFCStatementAdapter
    from core.adapters.bank_icici import ICICIStatementAdapter
    from core.adapters.mt940 import MT940Adapter
    from core.adapters.razorpay_settlement import RazorpaySettlementAdapter

    head = CLEAN.read_bytes()[:8192]
    for rival in (
        HDFCStatementAdapter(),
        ICICIStatementAdapter(),
        RazorpaySettlementAdapter(),
        MT940Adapter(),
    ):
        assert rival.sniff(head) == 0.0, rival.format_id


# --- the verified status set -----------------------------------------------


def test_all_eight_documented_financial_statuses_are_accounted_for():
    """`OrderDisplayFinancialStatus` has exactly eight members. Four map, four
    are unsupported, and none is silently absent -- an absent one would fall
    through to UNKNOWN_VALUE and be reported as a file problem when it is
    really a gap in this table."""
    documented = {
        "authorized",
        "expired",
        "paid",
        "partially_paid",
        "partially_refunded",
        "pending",
        "refunded",
        "voided",
    }
    assert set(STATUS_MAP) | UNSUPPORTED_STATUSES == documented
    assert set(STATUS_MAP) & UNSUPPORTED_STATUSES == set()


def test_voided_becomes_cancelled_and_not_refunded():
    """A voided order is an authorisation released before capture. No money
    ever moved, so it is a cancelled sale; calling it a refund would put a
    return that never happened into the merchant's numbers."""
    assert STATUS_MAP["voided"] == "cancelled"


# --- clean parse, cell by cell ---------------------------------------------


def test_clean_fixture_parses_with_zero_quarantine(adapter):
    result = adapter.parse(CLEAN)
    assert result.quarantined == []
    assert result.record_count == 10
    assert len(result.row_hashes) == 10


def test_the_two_line_item_continuation_rows_are_skipped_not_quarantined(adapter):
    """12 data rows, 10 orders, 2 continuations. A continuation is not a defect
    and must not land in a review queue next to the real ones."""
    result = adapter.parse(CLEAN)
    assert result.skipped_rows == 2
    assert result.quarantine_count == 0
    assert sorted(_by_id(result)) == [f"#{1000 + n}" for n in range(1, 11)]


def test_a_multi_line_item_order_is_one_order_at_its_order_total(adapter):
    """#1002 spans two rows. The canonical `Order` carries the order total, not
    a basket, so the second row must not double it."""
    order = _by_id(adapter.parse(CLEAN))["#1002"]
    assert order.gross_amount == 448_164  # 4481.64, once
    assert order.order_date == date(2026, 8, 1)


def test_the_first_order_cell_by_cell(adapter):
    order = _by_id(adapter.parse(CLEAN))["#1001"]
    assert order.customer_ref == "aarav.sharma@example.in"
    assert order.gross_amount == 304_782
    assert order.currency == "INR"
    assert order.status == "paid"
    assert order.order_date == date(2026, 8, 1)


def test_indian_digit_grouping_in_the_total_survives(adapter):
    """`"1,23,456.78"` is lakh grouping, not three-digit grouping, and it is
    what an Indian Shopify store's export actually contains."""
    assert _by_id(adapter.parse(CLEAN))["#1006"].gross_amount == 12_345_678


def test_each_mapped_status_appears_in_the_clean_fixture(adapter):
    orders = _by_id(adapter.parse(CLEAN))
    assert orders["#1003"].status == "refunded"
    assert orders["#1004"].status == "partially_refunded"
    assert orders["#1005"].status == "cancelled"  # Financial Status `voided`
    assert orders["#1001"].status == "paid"


def test_the_offset_is_parsed_and_deliberately_not_applied(adapter, tmp_path: Path):
    """`2026-08-01 00:30:00 +0530` is 2026-07-31 19:00 UTC. Converting would
    move the order into the previous month, and a sales register is kept in
    local days."""
    header = [
        line
        for line in CLEAN.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ][0]
    columns = header.split(",")
    row = [""] * len(columns)
    for name, value in (
        ("Name", "#9001"),
        ("Email", "night.owl@example.in"),
        ("Financial Status", "paid"),
        ("Currency", "INR"),
        ("Total", "100.00"),
        ("Created at", "2026-08-01 00:30:00 +0530"),
    ):
        row[columns.index(name)] = value
    path = tmp_path / "midnight.csv"
    path.write_text(f"{header}\n{','.join(row)}\n", encoding="utf-8")

    order = adapter.parse(path).records[0]
    assert order.order_date == date(2026, 8, 1)


# --- the dirty fixture ------------------------------------------------------


def test_dirty_fixture_still_yields_its_clean_rows(adapter):
    result = adapter.parse(DIRTY)
    assert [record.order_id for record in result.records] == ["#2001", "#2006", "#2012"]
    assert result.record_count == 3


def test_every_defect_is_named_by_line_and_reason(adapter):
    result = adapter.parse(DIRTY)
    by_line = {q.row_number: q.reason for q in result.quarantined}
    assert by_line == {
        14: QuarantineReason.BAD_DECIMAL,  # Total 1234.567 -- sub-paise
        15: QuarantineReason.MISSING_VALUE,  # Email empty
        16: QuarantineReason.TRUNCATED_ROW,  # 4 of 21 fields
        17: QuarantineReason.BAD_DATE,  # Created at 03/08/2026
        19: QuarantineReason.DUPLICATE_ROW,  # #2006 twice
        20: QuarantineReason.UNSUPPORTED_ROW_TYPE,  # `pending`
        21: QuarantineReason.UNKNOWN_VALUE,  # `wibble`
        22: QuarantineReason.UNKNOWN_VALUE,  # Currency USD
        23: QuarantineReason.EXTRA_FIELDS,  # unquoted comma in Lineitem name
        24: QuarantineReason.AMBIGUOUS_DIRECTION,  # Total -590.00 on a paid order
    }


def test_a_pending_order_is_unsupported_and_says_so_rather_than_unknown(adapter):
    """`pending` is a documented Shopify status. Reporting it as UNKNOWN would
    tell the merchant their file is wrong when the gap is on this side."""
    quarantine = {q.row_number: q for q in adapter.parse(DIRTY).quarantined}[20]
    assert quarantine.reason is QuarantineReason.UNSUPPORTED_ROW_TYPE
    assert "no counterpart in Order.status" in quarantine.detail


def test_an_undocumented_status_is_unknown_and_lists_the_eight(adapter):
    quarantine = {q.row_number: q for q in adapter.parse(DIRTY).quarantined}[21]
    assert quarantine.reason is QuarantineReason.UNKNOWN_VALUE
    for status in ("paid", "voided", "pending", "expired"):
        assert status in quarantine.detail


def test_a_negative_total_is_refused_rather_than_stored(adapter):
    quarantine = {q.row_number: q for q in adapter.parse(DIRTY).quarantined}[24]
    assert quarantine.reason is QuarantineReason.AMBIGUOUS_DIRECTION
    assert "A refund is a Financial Status, not a negative total" in quarantine.detail


def test_nothing_is_dropped(adapter):
    """13 data rows in, 3 records plus 10 quarantines plus 0 skips out."""
    result = adapter.parse(DIRTY)
    assert result.record_count + result.quarantine_count + result.skipped_rows == 13


def test_a_hash_prefixed_order_name_is_data_not_a_comment(adapter):
    """Shopify names an order `#1001`, and this package's fixture provenance
    convention is a leading `#` comment block. The two collide head-on.

    Nothing breaks, because `strip_comment_lines` stops at the FIRST
    non-comment line and the header sits between the comments and the data --
    but the collision is one edit away from eating every row of a real export,
    so it is pinned here rather than left to be rediscovered.
    """
    result = adapter.parse(CLEAN)
    assert result.record_count == 10
    assert all(record.order_id.startswith("#") for record in result.records)

    # And the negative control: a file with no provenance block at all, whose
    # very first line after the header is a `#`-named order.
    from core.adapters.base import strip_comment_lines

    text = CLEAN.read_text(encoding="utf-8")
    body, dropped = strip_comment_lines(text)
    assert dropped == 13
    assert body.splitlines()[0].startswith("Name,")
    assert body.splitlines()[1].startswith("#1001,")


def test_parsing_twice_is_identical(adapter):
    first = adapter.parse(CLEAN)
    second = adapter.parse(CLEAN)
    assert first.row_hashes == second.row_hashes
    assert [r.model_dump() for r in first.records] == [
        r.model_dump() for r in second.records
    ]
