"""Razorpay settlement report, per-transaction layout -> canonical PSP legs.

**Schema provenance.** The column set and semantics here follow Razorpay's
published settlement-recon schema: `entity_id`, `type`, `debit`, `credit`,
`amount`, `currency`, `fee`, `tax`, `on_hold`, `settled`, `created_at`,
`settled_at`, `settlement_id`, `settlement_utr`, `order_id`, `order_receipt`,
`payment_id`, `method`. That field list was verified against
https://razorpay.com/docs/api/settlements/fetch-recon/ (see ADAPTERS-REPORT.md).
Two things about the *downloaded report* rather than the API response could NOT
be verified against live documentation and are implemented from knowledge:

* the fee column is spelled `fee (exclusive tax)` in the dashboard export where
  the API calls it `fee`. Both spellings are accepted here for that reason.
* the export writes rupee decimals and human timestamps where the API returns
  integer subunits and unix epochs. This adapter reads the export.

Both are flagged in ADAPTERS-REPORT.md as needing verification against a real
downloaded report. They are the kind of thing that is cheap to correct and
expensive to have quietly wrong, so they are stated rather than assumed.

**The mapping, and why it has this shape.** The canonical schema carries fees
and GST as their own signed `PSPTransaction` legs, because that is what lets
the matcher reconstruct a settlement's net as a plain sum. A settlement report
row is the opposite shape: one row carrying a gross amount with its fee and tax
folded into adjacent columns. So one row becomes up to three legs --

    payment  +amount        (gross, positive)
    fee      -fee           (only when non-zero)
    tax      -tax           (only when non-zero)

-- whose sum is the row's `credit - debit`. That identity is *checked*, not
assumed: a row where it does not hold is internally inconsistent and goes to
quarantine whole, because a half-trusted settlement row silently changes what
the engine thinks a batch nets to.

**`order_id` comes from `order_receipt` when there is one.** The canonical
`order_id` is the merchant's own order reference -- the key their sales
register is on. Razorpay's `order_id` (`order_Jsxxxxxxxxxxxx`) is Razorpay's
key for the same thing, and a merchant cannot look it up. `order_receipt` is
the field the merchant supplied at order creation, so it is preferred, with the
Razorpay id as the fallback when no receipt was set.

**A settlement-level MDR or GST line is its own row, and that is an
extension.** Razorpay's published recon `type` enum is `payment`, `refund`,
`transfer`, `adjustment`; a per-transaction row's fee and GST live in its `fee`
and `tax` COLUMNS, which is what the three-legs mapping above reads. But the
canonical schema also carries a settlement's MDR and GST as legs **with their
own identifiers**, and a transaction row's fee column has no identifier to give
them -- the adapter has to invent one (`fee_<entity_id>`), and an invented
identifier is exactly what cannot survive a round trip. So `type` values `fee`
and `tax` are accepted as well: one row, one canonical leg, `entity_id` being
that leg's identity rather than a name this reader made up. Those two values are
**FROM KNOWLEDGE and are not in Razorpay's documented enum** -- a genuine
Razorpay export will never contain them, so accepting them costs a real file
nothing. `VALIDATION.md` records this as an extension rather than a schema
claim. The per-transaction fee/tax columns are unchanged and still parsed on
every row.

**Route transfers are quarantined, not mapped.** `type == "transfer"` is a real
row describing money moved to a linked account, and the canonical
`PSPTransaction.txn_type` has no member for it. Mapping it onto `adjustment`
would put a number the engine would treat as a deduction into a batch where it
means something else. It is quarantined as `UNSUPPORTED_ROW_TYPE`, which is
visible, countable, and honest about the gap.

`on_hold` is read but not used for typing: in the recon report it is an
attribute of transfers, and transfers do not reach the mapping.
"""

from __future__ import annotations

from core.adapters.base import (
    CanonicalRecord,
    QuarantineReason,
    RawRow,
    parse_date_exact,
    parse_datetime_exact,
)
from core.adapters.csv_source import CsvSourceAdapter, RowError
from core.models import PSPTransaction

#: `created_at` in the downloaded report. The API's unix epoch is deliberately
#: NOT accepted: a ten-digit integer is also a plausible reference number, and
#: a parser that takes both cannot tell a timestamp from a typo.
TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
)

#: `settled_at`. Same layouts, plus date-only, because a settlement date with no
#: time is what the report writes once the batch has closed.
SETTLED_AT_FORMATS = TIMESTAMP_FORMATS + ("%Y-%m-%d", "%d-%m-%Y")

#: Report `type` -> canonical `PSPTransaction.txn_type`. `transfer` is absent on
#: purpose; see the module docstring.
TYPE_MAP = {
    "payment": "payment",
    "refund": "refund",
    "adjustment": "adjustment",
    "dispute": "chargeback",
    # Documented extension, FROM KNOWLEDGE, not in Razorpay's recon `type`
    # enum: a settlement-level MDR or GST line, carrying its own identifier.
    "fee": "fee",
    "tax": "tax",
}

#: The `type` values Razorpay's published recon schema actually defines. Kept
#: separate from `TYPE_MAP` so the two claims stay distinguishable: this tuple
#: is VERIFIED against the docs, `TYPE_MAP` is what this adapter accepts.
DOCUMENTED_TYPES: tuple[str, ...] = ("payment", "refund", "transfer", "adjustment")

#: Row types this adapter recognises but the canonical schema does not carry.
UNSUPPORTED_TYPES = {"transfer"}

#: The dashboard export and the API disagree about this column's name. Accepting
#: both is not a flag -- it is one column with two spellings, and picking the
#: wrong one would make a real report look like it was missing its fees.
FEE_COLUMNS = ("fee (exclusive tax)", "fee")


class RazorpaySettlementAdapter(CsvSourceAdapter):
    """The per-transaction settlement report: one row per settled transaction.

    The *combined* report -- one row per settlement batch, with no per-payment
    breakdown -- is a different layout and would be a different adapter with its
    own `format_id`, never a flag on this one. A flag here would mean one class
    with two mutually exclusive column sets, two sniff shapes and two mappings,
    and every future reader would have to hold both in their head to change
    either.
    """

    format_id = "razorpay-settlement-v2"
    format_version = "2.0-per-transaction"

    REQUIRED_COLUMNS = (
        "entity_id",
        "type",
        "debit",
        "credit",
        "amount",
        "currency",
        "tax",
        "created_at",
        "settlement_id",
    )
    DISTINCTIVE_COLUMNS = (
        "settlement_utr",
        "order_receipt",
        "settled_at",
        "on_hold",
    )

    def sniff(self, head: bytes) -> float:
        """Required columns plus a fee column under either of its two names.

        Delegating to the base implementation would mean putting one of the two
        fee spellings in `REQUIRED_COLUMNS`, which would score a real report
        from the other source at 0.0.
        """
        from core.adapters.base import header_cells

        base_score = super().sniff(head)
        if base_score == 0.0:
            return 0.0
        cells = set(header_cells(head))
        if not any(self._normalised(name) in cells for name in FEE_COLUMNS):
            return 0.0
        return base_score

    @staticmethod
    def _normalised(name: str) -> str:
        from core.adapters.base import normalise_header

        return normalise_header(name)

    def duplicate_key(self, cells: dict[str, str], row: RawRow) -> str:
        """`entity_id` -- the report's own transaction id.

        Stronger than the raw line: a report re-exported with different
        whitespace or column padding still produces the same key, and one
        payment appearing twice in one report is a duplicate however it is
        spelled. Falls back to the raw line when the id is blank, so an
        id-less row is still deduplicated on something rather than on "".
        """
        entity_id = (cells.get("entity_id") or "").strip()
        return f"entity_id:{entity_id}" if entity_id else row.raw

    def _fee_paise(self, cells: dict[str, str]) -> int:
        for name in FEE_COLUMNS:
            if self._normalised(name) in cells:
                return self.required_paise(cells, name)
        raise RowError(
            QuarantineReason.MISSING_VALUE,
            f"no fee column found; expected one of {list(FEE_COLUMNS)}",
        )

    def records_from_row(
        self, cells: dict[str, str], row: RawRow
    ) -> list[CanonicalRecord]:
        entity_id = self.required_text(cells, "entity_id")
        raw_type = self.required_text(cells, "type").strip().lower()

        if raw_type in UNSUPPORTED_TYPES:
            raise RowError(
                QuarantineReason.UNSUPPORTED_ROW_TYPE,
                f"type {raw_type!r} has no counterpart in the canonical schema "
                f"(PSPTransaction.txn_type); the row is kept here rather than dropped",
            )
        if raw_type not in TYPE_MAP:
            raise RowError(
                QuarantineReason.UNKNOWN_VALUE,
                f"column 'type' value {raw_type!r} is not one of "
                f"{sorted(TYPE_MAP) + sorted(UNSUPPORTED_TYPES)}",
            )
        txn_type = TYPE_MAP[raw_type]

        currency = self.required_text(cells, "currency").upper()
        if currency != "INR":
            raise RowError(
                QuarantineReason.UNKNOWN_VALUE,
                f"column 'currency' value {currency!r}: the canonical schema is INR-only",
            )

        amount = self.required_paise(cells, "amount")
        fee = self._fee_paise(cells)
        tax = self.required_paise(cells, "tax")
        debit = self.required_paise(cells, "debit")
        credit = self.required_paise(cells, "credit")

        if debit and credit:
            raise RowError(
                QuarantineReason.AMBIGUOUS_DIRECTION,
                f"both 'debit' ({debit} paise) and 'credit' ({credit} paise) are "
                f"non-zero, so the direction of the row is undecidable",
            )
        if not debit and not credit:
            raise RowError(
                QuarantineReason.AMBIGUOUS_DIRECTION,
                "'debit' and 'credit' are both zero, so the row moves no money "
                "and its direction cannot be read",
            )

        #: Positive when the report credited the merchant, negative when it
        #: debited them -- the same signed convention `PSPTransaction.amount`
        #: uses, so a settlement's net stays a plain sum of its legs.
        principal = amount if credit else -amount

        legs_total = principal - fee - tax
        net = credit - debit
        if legs_total != net:
            raise RowError(
                QuarantineReason.ARITHMETIC_MISMATCH,
                f"amount - fee - tax = {legs_total} paise but credit - debit = "
                f"{net} paise; the row does not balance against itself",
            )

        try:
            captured_at = parse_datetime_exact(
                self.required_text(cells, "created_at"), TIMESTAMP_FORMATS
            )
        except ValueError as error:
            raise RowError(
                QuarantineReason.BAD_DATE, f"column 'created_at': {error}"
            ) from error

        settled_at_raw = self.optional_text(cells, "settled_at")
        settled_at = None
        if settled_at_raw is not None:
            try:
                settled_at = parse_date_exact(settled_at_raw, SETTLED_AT_FORMATS)
            except ValueError as error:
                raise RowError(
                    QuarantineReason.BAD_DATE, f"column 'settled_at': {error}"
                ) from error

        settlement_id = self.optional_text(cells, "settlement_id")
        order_id = self.optional_text(cells, "order_receipt") or self.optional_text(
            cells, "order_id"
        )

        def leg(txn_id: str, kind: str, value: int) -> PSPTransaction:
            return PSPTransaction(
                txn_id=txn_id,
                txn_type=kind,
                order_id=order_id,
                captured_at=captured_at,
                amount=value,
                settlement_id=settlement_id,
                settled_at=settled_at,
            )

        records: list[CanonicalRecord] = [leg(entity_id, txn_type, principal)]
        # A zero fee is not a fee leg. Emitting one would put a row into the
        # ledger that says nothing and inflate every per-leg count downstream.
        if fee:
            records.append(leg(f"fee_{entity_id}", "fee", -fee))
        if tax:
            records.append(leg(f"tax_{entity_id}", "tax", -tax))
        return records
