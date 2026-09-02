"""Delhivery-style COD remittance CSV -> canonical PSP legs. The differentiator.

**The mapping decision, first, because it is the point of this module.** A COD
remittance is one bank credit covering many delivered cash-on-delivery orders,
minus the courier's freight, COD handling fee, RTO charges and the GST on
those. That is the *same shape* as a PSP settlement: many payments, netted
against deductions, arriving as one credit. So this adapter does not describe a
new shape to the engine -- it emits the shape the engine already solves. Each
COD order becomes a `payment` `PSPTransaction` leg at the amount collected;
each deduction becomes a signed-negative `fee` leg (or `tax`, for the GST); and
every leg from one remittance carries the SAME `settlement_id`, derived from
the remittance reference. The existing matcher then reconciles the remittance
against the bank credit through the same T0 path a Razorpay settlement takes,
with no engine change, no new tier, and no new model field. A test in
`tests/adapters/test_cod_remittance.py` runs `core.matcher.engine.run_match`
over exactly these legs and one hand-built bank line and asserts the T0 match.

That is the whole argument that the engine generalises beyond its own
generator, and it is worth stating what would have been the easy alternative:
a "COD mode" in the matcher, or a `CODRemittance` model, either of which would
have made the claim untestable by making COD a special case of nothing.

**`settlement_id` is `setl_cod` plus the remittance reference, alphanumerics
only.** `DLV/REM/26081401` becomes `setl_codDLVREM26081401`. Three constraints
meet in that string and all three matter:

* `core/models.py` documents `settlement_id` as a `setl_`-prefixed identifier,
  so this is the canonical namespace and not Razorpay's private one;
* `core/canonicalize/narration.py` finds a settlement reference with
  ``\\b(setl_[A-Za-z0-9]+)\\b`` -- **no underscore after the prefix** -- so
  `setl_cod_DLVREM...` would not match and a bank narration carrying it would
  silently fail to join. `setl_cod` is one token on purpose;
* the `cod` marker survives into every downstream screen, so a reviewer
  looking at a settlement id can see where it came from.

**Schema provenance: FROM KNOWLEDGE, and the weakest on this branch.** Courier
portals publish no export schema -- not Delhivery, not any of them -- and every
seller account seems to get a slightly different column set. So the column
names below are a *default*, not a contract, and `column_map` is a plain dict a
caller overrides:

    CODRemittanceAdapter(column_map={**DEFAULT_COLUMN_MAP, "cod_amount": "COD Collected"})

That is deliberately configuration rather than code. A courier file that spells
freight `Forward Charges` is not a new format and must not need a new class;
the *shape* -- a remittance reference, per-order collections, itemised
deductions -- is what this adapter knows, and the shape is what is stable.

**Row types.** A `COD` row is one delivered order. A `DEDUCTION` row is a
remittance-level charge with no waybill and no order behind it -- a monthly RTO
reversal, say -- and it emits deduction legs alone. If the file has no row-type
column at all, every row is read as `COD`, because a courier file without that
column is a per-order file. Any other value is `UNKNOWN_VALUE`: guessing what
`PARTIAL` means would put a number the engine reads as a collection into a
batch where it means something else.

**A zero is not a leg.** A COD row that collected nothing but was charged for
its return emits fee legs and no payment leg, exactly as a zero-fee settlement
row emits no fee leg in `razorpay_settlement.py`. A row that carries neither a
collection nor a deduction moves no money at all and is quarantined as
`AMBIGUOUS_DIRECTION` rather than emitted as nothing, because "emitted as
nothing" and "silently dropped" are the same thing from the outside.

**`captured_at` has no clock behind it.** The canonical `PSPTransaction`
carries a datetime and this file carries only dates, so the delivery date is
combined with midnight -- a value read off the file, never `datetime.now()`,
which `core/` may not call at all. A `DEDUCTION` row has no delivery date, so
it takes the remittance date; the deduction was levied against the remittance,
not against a delivery.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, time

from core.adapters.base import (
    CanonicalRecord,
    QuarantineReason,
    RawRow,
    normalise_header,
    parse_date_exact,
)
from core.adapters.csv_source import CsvSourceAdapter, RowError
from core.models import PSPTransaction

#: The default column mapping: canonical role -> the column name a Delhivery
#: remittance is *expected* to use. Override the values, never the keys.
DEFAULT_COLUMN_MAP: dict[str, str] = {
    "remittance_ref": "Remittance Ref",
    "utr": "UTR",
    "remittance_date": "Remittance Date",
    "waybill": "Waybill",
    "order_id": "Order ID",
    "row_type": "Row Type",
    "delivery_date": "Delivery Date",
    "cod_amount": "COD Amount",
    "freight": "Freight Charge",
    "cod_fee": "COD Handling Fee",
    "rto": "RTO Charge",
    "gst": "GST",
}

#: The roles without which there is no remittance to read. `row_type`,
#: `delivery_date` and the individual deduction columns are all optional: a
#: courier that does not charge RTO simply has no RTO column, and demanding one
#: would reject a perfectly good file.
REQUIRED_ROLES = (
    "remittance_ref",
    "remittance_date",
    "waybill",
    "order_id",
    "cod_amount",
)
DISTINCTIVE_ROLES = ("waybill", "freight", "cod_fee", "rto")

#: The deduction roles, each with the canonical leg type it becomes and the
#: `txn_id` prefix that keeps two deductions on one waybill apart. GST is a
#: `tax` leg rather than a `fee` leg because the engine's ITC reconciliation
#: reads `tax` legs and nothing else -- a courier's GST is input tax credit in
#: exactly the way a PSP's GST on MDR is.
DEDUCTION_ROLES: tuple[tuple[str, str, str], ...] = (
    ("freight", "fee", "frt"),
    ("cod_fee", "fee", "cdf"),
    ("rto", "fee", "rto"),
    ("gst", "tax", "gst"),
)

#: Courier portals write `DD-MM-YYYY`, and the ISO layout turns up whenever a
#: file has been through a spreadsheet. Both are unambiguous; nothing that is
#: not is accepted.
DATE_FORMATS = ("%d-%m-%Y", "%Y-%m-%d")

ROW_TYPE_COD = "COD"
ROW_TYPE_DEDUCTION = "DEDUCTION"

#: What may follow `setl_` and still be found by
#: `core/canonicalize/narration.py`. Underscores may not; see the module
#: docstring.
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")

SETTLEMENT_PREFIX = "setl_cod"


def settlement_id_for(remittance_ref: str) -> str:
    """`DLV/REM/26081401` -> `setl_codDLVREM26081401`.

    Deterministic, and derived only from the reference the file carries, so two
    files describing the same remittance produce the same settlement id and the
    engine sees one batch rather than two.
    """
    return f"{SETTLEMENT_PREFIX}{_NON_ALNUM.sub('', remittance_ref)}"


class CODRemittanceAdapter(CsvSourceAdapter):
    """One remittance, many COD orders, one shared `settlement_id`."""

    format_id = "cod-remittance-delhivery-v1"
    format_version = "1.0"

    def __init__(self, column_map: Mapping[str, str] | None = None) -> None:
        unknown = set(column_map or {}) - set(DEFAULT_COLUMN_MAP)
        if unknown:
            raise ValueError(
                f"unknown column role(s) {sorted(unknown)}; the overridable roles "
                f"are {sorted(DEFAULT_COLUMN_MAP)}. Override the column NAMES, not "
                f"the roles -- a new role would be a new mapping and belongs in code"
            )
        self.column_map = {**DEFAULT_COLUMN_MAP, **(column_map or {})}
        # Instance attributes, shadowing the class-level tuples the base uses.
        # They have to be derived rather than declared because the whole point
        # of `column_map` is that the names are not known until construction.
        self.REQUIRED_COLUMNS = tuple(self.column_map[role] for role in REQUIRED_ROLES)
        self.DISTINCTIVE_COLUMNS = tuple(
            self.column_map[role] for role in DISTINCTIVE_ROLES
        )

    # --- column access by role, never by literal name -----------------------

    def _column(self, role: str) -> str:
        return self.column_map[role]

    def _has(self, cells: Mapping[str, str], role: str) -> bool:
        return normalise_header(self._column(role)) in cells

    def _text(self, cells: Mapping[str, str], role: str) -> str | None:
        value = (cells.get(normalise_header(self._column(role))) or "").strip()
        return value or None

    def _deduction_paise(self, cells: dict[str, str], role: str) -> int:
        """A deduction column that is absent is zero; one that is present and
        unreadable is a quarantine. Absent and unreadable are different facts."""
        if not self._has(cells, role):
            return 0
        return self.optional_paise(cells, self._column(role)) or 0

    def duplicate_key(self, cells: dict[str, str], row: RawRow) -> str:
        """The waybill -- the courier's own per-shipment id.

        Stronger than the raw line: one shipment cannot be remitted twice, and
        a portal that re-exported the row with a different delivery timestamp
        has still sent the same money twice. Remittance-level deduction rows
        carry no waybill, so they fall back to the raw line.
        """
        waybill = self._text(cells, "waybill")
        return f"waybill:{waybill}" if waybill else row.raw

    def records_from_row(
        self, cells: dict[str, str], row: RawRow
    ) -> list[CanonicalRecord]:
        remittance_ref = self.required_text(cells, self._column("remittance_ref"))
        settlement_id = settlement_id_for(remittance_ref)

        try:
            remitted_on = parse_date_exact(
                self.required_text(cells, self._column("remittance_date")), DATE_FORMATS
            )
        except ValueError as error:
            raise RowError(
                QuarantineReason.BAD_DATE,
                f"column {self._column('remittance_date')!r}: {error}",
            ) from error

        row_type = ROW_TYPE_COD
        if self._has(cells, "row_type"):
            row_type = (self._text(cells, "row_type") or ROW_TYPE_COD).upper()
        if row_type not in (ROW_TYPE_COD, ROW_TYPE_DEDUCTION):
            raise RowError(
                QuarantineReason.UNKNOWN_VALUE,
                f"column {self._column('row_type')!r} value {row_type!r} is not "
                f"{ROW_TYPE_COD!r} or {ROW_TYPE_DEDUCTION!r}; what a courier means by "
                f"it is not something this adapter will guess at, because a "
                f"misread collection lands in a batch as money that never arrived",
            )

        is_deduction_row = row_type == ROW_TYPE_DEDUCTION
        waybill = self._text(cells, "waybill")
        order_id = self._text(cells, "order_id")
        if not is_deduction_row:
            if waybill is None:
                raise RowError(
                    QuarantineReason.MISSING_VALUE,
                    f"column {self._column('waybill')!r} is required on a "
                    f"{ROW_TYPE_COD} row but empty",
                )
            if order_id is None:
                raise RowError(
                    QuarantineReason.MISSING_VALUE,
                    f"column {self._column('order_id')!r} is required on a "
                    f"{ROW_TYPE_COD} row but empty; a collection with no order "
                    f"reference is money the merchant cannot attribute",
                )

        collected = 0
        if not is_deduction_row:
            collected = self.required_paise(cells, self._column("cod_amount"))
            if collected < 0:
                raise RowError(
                    QuarantineReason.AMBIGUOUS_DIRECTION,
                    f"column {self._column('cod_amount')!r} is {collected} paise; a "
                    f"COD collection is what the courier took from the customer and "
                    f"cannot be negative -- a return is an RTO charge, not a "
                    f"negative collection",
                )

        deductions = [
            (role, leg_type, prefix, self._deduction_paise(cells, role))
            for role, leg_type, prefix in DEDUCTION_ROLES
        ]
        for role, _, _, amount in deductions:
            if amount < 0:
                raise RowError(
                    QuarantineReason.AMBIGUOUS_DIRECTION,
                    f"column {self._column(role)!r} is {amount} paise; deductions "
                    f"are written positive and signed negative on the leg, so a "
                    f"negative here would be added back to the remittance",
                )

        if not collected and not any(amount for _, _, _, amount in deductions):
            raise RowError(
                QuarantineReason.AMBIGUOUS_DIRECTION,
                "the row carries neither a collection nor a deduction, so it moves "
                "no money; emitting nothing for it would be indistinguishable from "
                "dropping it",
            )

        # A `DEDUCTION` row was levied against the remittance, not against a
        # delivery, so it has no delivery date and takes the remittance date.
        captured_on = remitted_on
        if self._has(cells, "delivery_date") and not is_deduction_row:
            raw_delivery = self._text(cells, "delivery_date")
            if raw_delivery is not None:
                try:
                    captured_on = parse_date_exact(raw_delivery, DATE_FORMATS)
                except ValueError as error:
                    raise RowError(
                        QuarantineReason.BAD_DATE,
                        f"column {self._column('delivery_date')!r}: {error}",
                    ) from error
        # Midnight on a date READ FROM THE FILE. `core/` may not call a clock,
        # and this is the only place a time could have crept in.
        captured_at = datetime.combine(captured_on, time.min)

        #: What names the legs of this row. A `DEDUCTION` row has no waybill,
        #: so it is identified by its remittance and its physical line -- which
        #: is stable for a given file and is the only thing that distinguishes
        #: two remittance-level charges of the same kind.
        subject = waybill or f"{_NON_ALNUM.sub('', remittance_ref)}L{row.row_number}"

        def leg(txn_id: str, txn_type: str, amount: int) -> PSPTransaction:
            return PSPTransaction(
                txn_id=txn_id,
                txn_type=txn_type,
                order_id=order_id,
                captured_at=captured_at,
                amount=amount,
                settlement_id=settlement_id,
                settled_at=remitted_on,
            )

        records: list[CanonicalRecord] = []
        if collected:
            records.append(leg(f"cod_{subject}", "payment", collected))
        for _, leg_type, prefix, amount in deductions:
            if amount:
                records.append(leg(f"{prefix}_{subject}", leg_type, -amount))
        return records
