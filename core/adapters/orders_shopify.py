"""Shopify order export (`orders_export.csv`) -> canonical `Order`.

**Schema provenance: VERIFIED.** The column list was fetched from Shopify's own
documentation on 2026-08-30
(<https://help.shopify.com/en/manual/fulfillment/managing-orders/exporting-orders>)
and reads, in export order:

    Name, Phone, Email, Financial Status, Paid at, Fulfillment Status,
    Fulfilled at, Accepts Marketing, Currency, Subtotal, Shipping, Taxes,
    Total, Discount Code, Discount Amount, Shipping Method, Created at,
    Lineitem quantity, Lineitem name, Lineitem price, Lineitem compare-at
    price, Lineitem SKU, Lineitem requires shipping, Lineitem taxable,
    Lineitem fulfillment status, Billing Name, ... Shipping Name, ..., Notes,
    Note Attributes, Canceled at, Payment Method, Payment Reference
    (deprecated), Payment References, Refunded Amount, Vendor, Outstanding
    Balance, Employee, Location, Device ID, Id, Tags, Risk Level, Source,
    Lineitem discount, Tax # Name, Tax # Value, Payment ID, Payment terms,
    Next payment due at

Only six of those are required here. An export is a merchant's file and they
trim it; requiring seventy columns would reject real files for carrying less
than everything, and the six below are the ones without which there is no
`Order` to build.

**The `Financial Status` values are VERIFIED too**, against
`OrderDisplayFinancialStatus` in the Shopify Admin GraphQL API: `AUTHORIZED`,
`EXPIRED`, `PAID`, `PARTIALLY_PAID`, `PARTIALLY_REFUNDED`, `PENDING`,
`REFUNDED`, `VOIDED` -- eight, exactly. That the CSV writes them lower-cased
with underscores is from knowledge rather than documented, which is why the
comparison here is case-folded: it makes the uncertainty free.

**Mapping the status, and the two different ways it can fail.** Four of the
eight have a counterpart in `Order.status`:

    paid                -> paid
    refunded            -> refunded
    partially_refunded  -> partially_refunded
    voided              -> cancelled

`voided` is the only rename, and it is the right one: a voided order is an
authorisation released before capture, which is a cancelled sale and not a
refunded one -- no money ever moved.

The other four -- `authorized`, `expired`, `partially_paid`, `pending` -- are
real Shopify statuses that the canonical schema does not carry, so they are
`UNSUPPORTED_ROW_TYPE`, exactly as a Razorpay Route transfer is. A value that
is not a Shopify status at all is `UNKNOWN_VALUE`. The two are separated
because their fixes differ: the first says "the engine has nowhere to put
this", the second says "your file has something in it we have never seen".
Neither is ever a guess, because guessing here mislabels a sale.

**One order can span several rows.** Shopify writes an order's extra line items
as further rows that repeat `Name` and leave every order-level column empty.
Those are skipped, not quarantined and not deduplicated: they are line items of
an order this adapter already emitted from the row above, and the canonical
`Order` carries an order total rather than a basket. They are counted in
`AdapterResult.skipped_rows` so nothing is dropped without a number attached.

**The order date is taken in the offset the file writes, not in UTC.** Shopify
stamps `Created at` as `2026-08-01 09:15:32 +0530`. Converting that to UTC
would move an order placed at half past midnight IST into the previous day, and
a merchant's sales register -- the thing this reconciles against -- is kept in
their own local days. So the offset is parsed, validated and then deliberately
not applied.
"""

from __future__ import annotations

from core.adapters.base import (
    CanonicalRecord,
    QuarantineReason,
    RawRow,
    normalise_header,
    parse_datetime_exact,
)
from core.adapters.csv_source import CsvSourceAdapter, RowError
from core.models import Order

#: `Created at` / `Paid at`. Shopify writes a UTC offset; a file exported
#: through a spreadsheet frequently loses it, so the offset-less layout is
#: accepted too. Nothing else is: a bare `03/08/2026` is ambiguous between two
#: continents and is quarantined rather than guessed at.
TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)

#: The four `OrderDisplayFinancialStatus` values with a canonical counterpart.
STATUS_MAP = {
    "paid": "paid",
    "refunded": "refunded",
    "partially_refunded": "partially_refunded",
    "voided": "cancelled",
}

#: The four that Shopify defines and `Order.status` does not carry. Visible,
#: countable, never bent onto a neighbour.
UNSUPPORTED_STATUSES = frozenset(
    {"authorized", "expired", "partially_paid", "pending"}
)


class ShopifyOrdersAdapter(CsvSourceAdapter):
    """The standard Shopify order export: one order, one or more rows."""

    format_id = "orders-csv-shopify-v1"
    format_version = "1.0"

    REQUIRED_COLUMNS = (
        "Name",
        "Email",
        "Financial Status",
        "Currency",
        "Total",
        "Created at",
    )
    #: What separates this from every other order export. `Lineitem quantity`
    #: and `Lineitem name` in particular are the shape of Shopify's one-row-per
    #: -line-item layout, which is the thing a look-alike export would not have.
    DISTINCTIVE_COLUMNS = (
        "Lineitem quantity",
        "Lineitem name",
        "Fulfillment Status",
        "Paid at",
        "Subtotal",
    )

    #: The order-level columns a line-item continuation row leaves empty.
    _ORDER_LEVEL_COLUMNS = ("Financial Status", "Total", "Created at")

    def is_skipped_row(self, cells: dict[str, str], row: RawRow) -> bool:
        """A further line item of the order emitted from the row above.

        Recognised by shape rather than by remembering the previous `Name`:
        `Name` present and every order-level column empty. Shape is what the
        file actually guarantees -- Shopify sorts an export by order, but a
        merchant who sorted the file in a spreadsheet has not changed what a
        continuation row looks like, and a state machine that assumed adjacency
        would start emitting half-orders.
        """
        if not (cells.get(normalise_header("Name")) or "").strip():
            return False
        return all(
            not (cells.get(normalise_header(column)) or "").strip()
            for column in self._ORDER_LEVEL_COLUMNS
        )

    def duplicate_key(self, cells: dict[str, str], row: RawRow) -> str:
        """`Name` -- Shopify's order number, which is the merchant's own key.

        Stronger than the raw line: the same order exported twice with a
        different `Fulfilled at` is still the same order, and an order number
        appearing twice in one export is a defect however the rest of the row
        is spelled.
        """
        name = (cells.get(normalise_header("Name")) or "").strip()
        return f"name:{name}" if name else row.raw

    def records_from_row(
        self, cells: dict[str, str], row: RawRow
    ) -> list[CanonicalRecord]:
        order_id = self.required_text(cells, "Name")
        customer_ref = self.required_text(cells, "Email")

        raw_status = self.required_text(cells, "Financial Status").strip().lower()
        if raw_status in UNSUPPORTED_STATUSES:
            raise RowError(
                QuarantineReason.UNSUPPORTED_ROW_TYPE,
                f"Financial Status {raw_status!r} is a real Shopify status with no "
                f"counterpart in Order.status ({sorted(STATUS_MAP.values())}); the "
                f"row is kept here rather than dropped or bent onto a neighbour",
            )
        if raw_status not in STATUS_MAP:
            raise RowError(
                QuarantineReason.UNKNOWN_VALUE,
                f"column 'Financial Status' value {raw_status!r} is not one of the "
                f"eight OrderDisplayFinancialStatus values "
                f"{sorted(set(STATUS_MAP) | UNSUPPORTED_STATUSES)}",
            )
        status = STATUS_MAP[raw_status]

        currency = self.required_text(cells, "Currency").upper()
        if currency != "INR":
            raise RowError(
                QuarantineReason.UNKNOWN_VALUE,
                f"column 'Currency' value {currency!r}: the canonical schema is INR-only",
            )

        gross = self.required_paise(cells, "Total")
        if gross < 0:
            raise RowError(
                QuarantineReason.AMBIGUOUS_DIRECTION,
                f"column 'Total' is {gross} paise while 'Financial Status' is "
                f"{raw_status!r}: the status says money came in and the amount says "
                f"it went out, so what this row describes cannot be read. A refund "
                f"is a Financial Status, not a negative total",
            )

        try:
            created_at = parse_datetime_exact(
                self.required_text(cells, "Created at"), TIMESTAMP_FORMATS
            )
        except ValueError as error:
            raise RowError(
                QuarantineReason.BAD_DATE, f"column 'Created at': {error}"
            ) from error

        return [
            Order(
                order_id=order_id,
                # `.date()` on the timestamp as written. See the module
                # docstring: the offset is parsed and deliberately not applied,
                # because a merchant's sales register is kept in local days.
                order_date=created_at.date(),
                customer_ref=customer_ref,
                gross_amount=gross,
                currency="INR",
                status=status,
            )
        ]
