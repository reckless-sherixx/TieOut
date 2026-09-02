"""`--export-as razorpay`: the same batches, written in the real file formats.

This module is the other half of the validation argument. `core/generator/` is
the only place ground truth exists; `core/adapters/` is the path a merchant's
own file takes. On its own, each proves something narrow -- the engine scores
well on data the generator made, and the adapters read hand-written files whose
answers nobody knows. Joining them is what produces a measured accuracy number
over real formats: emit the generated batches AS a Razorpay settlement export,
an HDFC statement and a Shopify order export, ingest those through the adapter
registry, run the engine, and score against the very same `truth.json`. Ground
truth stays in the loop and the adapters enter it.

**Every mapping here is the exact inverse of the adapter that reads it**, and
each one is written next to the rule it inverts:

* rupee decimals <- integer paise. Exact in this direction by construction --
  every paise value is `<rupees>.<2 digits>` and nothing rounds. `parse_paise`
  is the inverse and the round trip asserts it.
* `dd/MM/yy` <- `date`, `YYYY-MM-DD HH:MM:SS` <- `datetime`, exactly the
  layouts each adapter whitelists. No layout is written that its reader would
  have to guess at.
* one canonical leg <- one report row, and one report row -> one canonical leg.
  The bijection is the point: a mapping that folds two records into one row
  cannot give both of them back, and ground truth is keyed on their ids.

**A value this layer cannot represent is an error, never an approximation.**
`ExportError` names the record and the field. Silently writing something
close -- a rounded amount, a leg type bent onto its neighbour -- would produce
a file that ingests cleanly and reconciles to the wrong number, which is the
one failure this whole project exists to make impossible.

**One canonical field has no home in the HDFC layout: `BankLine.utr`.** An HDFC
statement has exactly one reference column, `Chq./Ref.No.`, and the canonical
`line_id` has to occupy it -- ground truth is keyed on line ids, and a
statement that does not name its lines cannot be scored against that truth.
The narration is written verbatim (the obfuscated-reference and trap narrations
must survive byte for byte, and appending a UTR to the trap's narration would
destroy the trap itself), so there is nowhere else to put it. The round trip
therefore reads back `utr=None` on every line, and
`tests/round_trip/test_round_trip.py` asserts that this costs no metric rather
than assuming it. It is recorded in `VALIDATION.md` as the one representational
loss of the export.

Byte-identity holds here exactly as it does in `emit.py`, and for the same
reasons: explicit newline handling, and every list sorted by an explicit key --
the same keys `emit.py` uses, so the exported files and the canonical CSVs are
in the same order row for row.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from core.models import BankLine, Order, PSPTransaction

from .batches import Batch
from .emit import _bank_sort_key, _chain_balances, _txn_sort_key

#: The one `--export-as` value this build understands. A second PSP would be a
#: second value here and a second set of writers, never a flag on these.
EXPORT_FORMATS = ("razorpay",)

#: File names written into the dataset directory alongside the canonical CSVs.
RAZORPAY_FILE = "razorpay_settlement.csv"
HDFC_FILE = "hdfc_statement.csv"
SHOPIFY_FILE = "shopify_orders.csv"

EXPORTED_FILES = (RAZORPAY_FILE, HDFC_FILE, SHOPIFY_FILE)


class ExportError(Exception):
    """A canonical value has no representation in the target format.

    Raised rather than approximated. See the module docstring.
    """


# --- exact rendering --------------------------------------------------------


def rupees(paise: int) -> str:
    """Integer paise -> the rupee-decimal string a real export writes.

    Exact, and integer arithmetic only: no `float`, and no `Decimal` either,
    because `divmod` on the magnitude is the whole operation. Always two
    decimal places, because a bank writing `71153.4` for 71153.40 is not a
    thing and a reader that accepted it would be accepting ambiguity.

    The sign is placed on the whole value rather than on the rupee part, so
    -1 paise is `-0.01` and not `0.-01`.
    """
    sign = "-" if paise < 0 else ""
    whole, fraction = divmod(abs(int(paise)), 100)
    return f"{sign}{whole}.{fraction:02d}"


def _hdfc_date(value: date) -> str:
    """`dd/MM/yy` -- `core.adapters.bank_hdfc.DATE_FORMATS`, and only that."""
    return f"{value:%d/%m/%y}"


def _razorpay_timestamp(value: datetime) -> str:
    """`YYYY-MM-DD HH:MM:SS` -- the first of the adapter's TIMESTAMP_FORMATS."""
    return f"{value:%Y-%m-%d %H:%M:%S}"


def _razorpay_settled_at(value: date | None) -> str:
    return "" if value is None else f"{value:%Y-%m-%d}"


#: Shopify writes an offset on `Created at`. IST, because the register this
#: reconciles is an Indian merchant's and the adapter deliberately does not
#: convert -- so the date read back is the date written.
SHOPIFY_OFFSET = "+0530"


def _shopify_timestamp(value: date) -> str:
    return f"{value:%Y-%m-%d} 00:00:00 {SHOPIFY_OFFSET}"


# --- razorpay settlement report --------------------------------------------

#: The per-transaction recon layout, in Razorpay's own column order. The fee
#: column is written under its dashboard spelling; the adapter accepts both.
RAZORPAY_COLUMNS = (
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
)

#: Canonical `txn_type` -> report `type`. The inverse of the adapter's
#: `TYPE_MAP`, and it is total over `PSPTransaction.txn_type` except for
#: `reserve`, which the report has no row type for at all -- so a `reserve` leg
#: is an `ExportError` rather than an `adjustment` that reconciles to the wrong
#: story. The generator emits none today; the check is there for the day it
#: does.
TXN_TYPE_TO_REPORT_TYPE = {
    "payment": "payment",
    "refund": "refund",
    "adjustment": "adjustment",
    "chargeback": "dispute",
    "fee": "fee",
    "tax": "tax",
}


def _razorpay_row(txn: PSPTransaction) -> tuple[str, ...]:
    report_type = TXN_TYPE_TO_REPORT_TYPE.get(txn.txn_type)
    if report_type is None:
        raise ExportError(
            f"{txn.txn_id}: txn_type {txn.txn_type!r} has no row type in the "
            f"razorpay-settlement-v2 layout, and bending it onto a neighbouring "
            f"type would put a number the engine reads one way into a row that "
            f"means another"
        )
    if txn.amount == 0:
        raise ExportError(
            f"{txn.txn_id}: an amount of zero cannot be written to this layout -- "
            f"direction is carried by which of 'debit' and 'credit' is non-zero, "
            f"so a zero leg reads back as AMBIGUOUS_DIRECTION"
        )

    magnitude = abs(txn.amount)
    credited = txn.amount > 0
    return (
        txn.txn_id,
        report_type,
        rupees(0 if credited else magnitude),
        rupees(magnitude if credited else 0),
        rupees(magnitude),
        "INR",
        # Fee and GST are settlement-level legs in this dataset and are written
        # as their own rows; a per-transaction row therefore carries no fee of
        # its own. The columns are still written, and the adapter still parses
        # them and still checks `amount - fee - tax == credit - debit`.
        rupees(0),
        rupees(0),
        "N",
        "Y" if txn.settlement_id else "N",
        _razorpay_timestamp(txn.captured_at),
        _razorpay_settled_at(txn.settled_at),
        txn.settlement_id or "",
        "",
        # `order_receipt` is the merchant's own reference and is what the
        # adapter prefers, so the canonical `order_id` goes there. Razorpay's
        # own `order_id` is left empty: this dataset has no Razorpay-side order
        # key, and inventing one would put a value in a column that no reader
        # could look up.
        "",
        txn.order_id or "",
        txn.txn_id if report_type == "payment" else "",
        "netbanking" if report_type == "payment" else "",
    )


# --- HDFC statement ---------------------------------------------------------

HDFC_COLUMNS = (
    "Date",
    "Narration",
    "Chq./Ref.No.",
    "Value Dt",
    "Withdrawal Amt.",
    "Deposit Amt.",
    "Closing Balance",
)


def _hdfc_row(line: BankLine) -> tuple[str, ...]:
    if "," in line.narration or "\n" in line.narration:
        raise ExportError(
            f"{line.line_id}: a narration carrying a comma or a newline cannot be "
            f"written verbatim into a CSV narration column"
        )
    credit = line.credit or 0
    debit = line.debit or 0
    if credit and debit:
        raise ExportError(
            f"{line.line_id}: both credit and debit are set; HDFC writes 0.00 in "
            f"the unused column and has no way to say both"
        )
    if not credit and not debit:
        raise ExportError(
            f"{line.line_id}: neither credit nor debit is set, so the line's "
            f"direction cannot be written"
        )
    return (
        _hdfc_date(line.txn_date),
        # Verbatim. Doubled spaces, the trap's byte-identical narration and
        # every obfuscated reference survive the export unchanged, because the
        # defect IS the narration and an export that tidied it would be
        # measuring a different dataset.
        line.narration,
        # The canonical line id. See the module docstring: this column is the
        # only reference an HDFC statement has, and ground truth is keyed on
        # line ids, so the id gets it and `utr` is the one value that is lost.
        line.line_id,
        _hdfc_date(line.txn_date),
        rupees(debit),
        rupees(credit),
        rupees(line.balance),
    )


# --- Shopify order export ---------------------------------------------------

#: A column subset of the verified export header, in the documented order. A
#: merchant's export is trimmed; the adapter's own tests cover the full
#: seventy-column header.
SHOPIFY_COLUMNS = (
    "Name",
    "Email",
    "Financial Status",
    "Paid at",
    "Fulfillment Status",
    "Currency",
    "Subtotal",
    "Shipping",
    "Taxes",
    "Total",
    "Created at",
    "Lineitem quantity",
    "Lineitem name",
    "Lineitem price",
)

#: Canonical `Order.status` -> Shopify `Financial Status`. The exact inverse of
#: the adapter's `STATUS_MAP`, `cancelled -> voided` included: a cancelled sale
#: is an authorisation released before capture, which Shopify spells `voided`.
STATUS_TO_FINANCIAL_STATUS = {
    "paid": "paid",
    "refunded": "refunded",
    "partially_refunded": "partially_refunded",
    "cancelled": "voided",
}


def _shopify_row(order: Order) -> tuple[str, ...]:
    financial_status = STATUS_TO_FINANCIAL_STATUS.get(order.status)
    if financial_status is None:
        raise ExportError(
            f"{order.order_id}: status {order.status!r} has no Shopify "
            f"Financial Status; the eight documented values do not include it"
        )
    if order.currency != "INR":
        raise ExportError(
            f"{order.order_id}: currency {order.currency!r} -- this export is INR"
        )
    total = rupees(order.gross_amount)
    voided = financial_status == "voided"
    return (
        order.order_id,
        order.customer_ref,
        financial_status,
        "" if voided else _shopify_timestamp(order.order_date),
        "unfulfilled" if voided else "fulfilled",
        "INR",
        total,
        rupees(0),
        rupees(0),
        total,
        _shopify_timestamp(order.order_date),
        "1",
        f"Order {order.order_id}",
        total,
    )


# --- writing ----------------------------------------------------------------


def _write_csv(
    path: Path, columns: Sequence[str], rows: Iterable[Sequence[str]]
) -> None:
    """Byte-identical on every platform: explicit newline handling, both ends.

    The same two arguments `emit.py` passes, for the same reason -- without them
    Python's text layer writes CRLF on Windows and the determinism claim fails
    on the machine the demo is recorded on.
    """
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)


def export_dataset(batches: Sequence[Batch], out_dir: Path | str) -> tuple[Path, ...]:
    """Write the three real-format files into `out_dir`. Returns their paths.

    Ordering is `emit.py`'s, key for key, so row *n* of `psp.csv` and row *n* of
    `razorpay_settlement.csv` are the same leg. That is not decoration: a
    difference in input order is a difference the matcher could in principle
    see, and the round-trip claim is that the two paths differ in NOTHING the
    engine reads.

    The bank balances are re-chained here rather than assumed, so this function
    is correct whether or not `emit_dataset` has already run over these batches.
    Chaining is idempotent -- it recomputes from `OPENING_BALANCE` every time.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    orders = sorted(
        (order for batch in batches for order in batch.orders),
        key=lambda order: order.order_id,
    )
    txns = sorted(
        (txn for batch in batches for txn in batch.psp_txns), key=_txn_sort_key
    )
    lines = sorted(
        (line for batch in batches for line in batch.all_bank_lines),
        key=_bank_sort_key,
    )
    _chain_balances(lines)

    _write_csv(
        out / RAZORPAY_FILE, RAZORPAY_COLUMNS, [_razorpay_row(t) for t in txns]
    )
    _write_csv(out / HDFC_FILE, HDFC_COLUMNS, [_hdfc_row(line) for line in lines])
    _write_csv(out / SHOPIFY_FILE, SHOPIFY_COLUMNS, [_shopify_row(o) for o in orders])
    return tuple(out / name for name in EXPORTED_FILES)


# --- `--dirty`: file-level damage that quarantine has to isolate -------------
#
# The dirty fixtures established what a real upload arrives carrying: a
# sub-paise decimal, a line cut short, a byte-order mark, a narration that is
# not UTF-8. Those prove the adapters quarantine correctly. What they cannot
# prove is the thing a merchant actually cares about -- that damage in a file
# is CONTAINED: that four bad rows out of forty thousand cost four rows and not
# the run.
#
# So `--dirty` injects exactly that mess into the exported files and the test
# asserts two things at once: the injected rows are quarantined, and every
# metric is identical to the clean round trip's.
#
# **The second assertion is true by construction, and saying how is the point.**
# Every injection here is either a file-level encoding change (a BOM, a latin-1
# byte) or a row APPENDED to the end of a file -- never an edit to a row the
# dataset contains. There is no such thing as a spare row in this dataset:
# every order, every leg and every bank line is named in `truth.json`, so
# damaging one would change what the run could match and the metrics would
# move, correctly. Appending gives quarantine something to catch that no
# linkage depends on, which is the only way to isolate the question "does
# damage stay where it is" from the question "does damage cost accuracy".
#
# Appending also keeps every surviving row's identity intact: the Razorpay legs
# are keyed on `entity_id`, the Shopify orders on `Name`, and the HDFC lines on
# `Chq./Ref.No.`, so no id shifts when a row lands at the end. The appended
# statement rows carry their own distinct references for the same reason -- a
# blank one would push the whole file back to positional line ids.


@dataclass(frozen=True)
class DirtyInjection:
    """One piece of file-level damage, and what it must come back as."""

    file: str
    #: `QuarantineReason` name, or `None` for damage that must cost nothing at
    #: all -- a BOM is a fact about the bytes, not a defect in a row.
    reason: str | None
    detail: str


#: The row appended to the settlement report: `tax` carries sub-paise, which has
#: no integer-paise answer and is refused rather than rounded.
_DIRTY_RAZORPAY_ROW = (
    "pay_dirty000001,payment,0.00,95.75,100.00,INR,0.00,4.255,N,Y,"
    "2026-01-05 09:00:00,2026-01-05,setl_dirty,,,ORD-DIRTY01,pay_dirty000001,"
    "netbanking"
)

#: A statement line cut short at 3 of 7 fields -- the export interrupted
#: mid-write, which is what a truncated download looks like.
_DIRTY_HDFC_TRUNCATED = "05/01/26,NEFT CR RAZORPAY SOFTWARE PVT LTD,BL-DIRTY-1"

#: A statement line whose narration is not UTF-8 (a latin-1 `é`), and which is
#: independently undecidable: both amount columns are zero, so the line moves no
#: money. The encoding makes the FILE latin-1; the zeros are what put this row
#: in quarantine, so the two facts stay separable.
_DIRTY_HDFC_LATIN1 = (
    "06/01/26,NEFT CR CAFÉ RETAIL PVT LTD,BL-DIRTY-2,06/01/26,0.00,0.00,0.00"
)

#: Every injection, in the order `dirty_export` applies them.
DIRTY_INJECTIONS: tuple[DirtyInjection, ...] = (
    DirtyInjection(
        file=RAZORPAY_FILE,
        reason="BAD_DECIMAL",
        detail="appended settlement row whose 'tax' is 4.255 -- sub-paise",
    ),
    DirtyInjection(
        file=HDFC_FILE,
        reason="TRUNCATED_ROW",
        detail="appended statement line cut short at 3 of 7 fields",
    ),
    DirtyInjection(
        file=HDFC_FILE,
        reason="AMBIGUOUS_DIRECTION",
        detail=(
            "appended statement line with a latin-1 narration and no direction; "
            "the encoding makes the file latin-1, the zeroed amounts quarantine "
            "the row"
        ),
    ),
    DirtyInjection(
        file=SHOPIFY_FILE,
        reason=None,
        detail=(
            "a UTF-8 byte-order mark on the order export: it must change the "
            "file hash and nothing else"
        ),
    ),
)


def dirty_export(out_dir: Path | str) -> tuple[DirtyInjection, ...]:
    """Damage the exported files in place. Returns what was injected.

    Runs after `export_dataset` over the same directory. Every injection is
    listed in `DIRTY_INJECTIONS`, which is what the test asserts against -- the
    list and the damage are one definition, so an injection cannot be added
    without the expectation moving with it.
    """
    out = Path(out_dir)

    report = out / RAZORPAY_FILE
    report.write_text(
        report.read_text(encoding="utf-8") + _DIRTY_RAZORPAY_ROW + "\n",
        encoding="utf-8",
        newline="",
    )

    statement = out / HDFC_FILE
    # Written as latin-1 bytes, not utf-8: the point of the `é` is that the file
    # does NOT decode as UTF-8 and the adapter has to fall back.
    statement.write_text(
        statement.read_text(encoding="utf-8")
        + _DIRTY_HDFC_TRUNCATED
        + "\n"
        + _DIRTY_HDFC_LATIN1
        + "\n",
        encoding="latin-1",
        newline="",
    )

    orders = out / SHOPIFY_FILE
    orders.write_bytes(b"\xef\xbb\xbf" + orders.read_bytes())

    return DIRTY_INJECTIONS
