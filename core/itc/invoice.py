"""`psp_gst_invoice.csv`: the row shape, and a strict reader for it.

One definition of the document, used by both ends. `core/generator/itc.py`
builds `GstInvoice` rows and the emitter writes them; `read_invoice` parses them
back. The wire format is `docs/CSV_SCHEMAS.md` §4.5.

**Why the reader is here and not in `core/ingest/reader.py`.** That module is
the ingest path for the three *reconciliation sources* -- every one of its
readers feeds `run_match`, and every one of its files is mandatory. This file
feeds nothing but the ITC report and is optional: a dataset generated before
this capability existed still ingests, matches and scores. Putting an optional,
non-matching input behind the same door as the three required ones would mean
either a fourth mandatory file or an `Optional` special case inside the ingest
contract, and the ITC package owns the document either way.

The strictness rules are `core/ingest/reader.py`'s, deliberately, because they
are the project's rules and not that module's: money is an integer number of
paise, a `.` in an amount column is a hard error naming the file, the line and
the column, and the header is validated before a single row is read.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from core.money import Money

#: The file the generator writes and this module reads.
INVOICE_FILENAME = "psp_gst_invoice.csv"

INVOICE_COLUMNS = (
    "invoice_no",
    "period",
    "taxable_value",
    "gst_amount",
    "gstin",
    "invoice_date",
)


@dataclass(frozen=True)
class GstInvoice:
    """One period's PSP tax invoice.

    `taxable_value` is the total MDR charged in the period and `gst_amount` is
    the GST charged on it, both positive integer paise. They are what the PSP
    *claims*; what the settlements actually bore is the engine's business, and
    the difference between the two is the entire report.
    """

    invoice_no: str
    period: str  # "2026-07"
    taxable_value: Money
    gst_amount: Money
    gstin: str
    invoice_date: date


def _rows(path: Path | str) -> Iterator[tuple[int, dict[str, str]]]:
    """`(file_line_number, row)`, header validated up front.

    The line number counts the header as line 1, which is the number a human
    sees in an editor -- the whole point of putting it in the error.
    """
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        header = tuple(reader.fieldnames or ())
        if header != INVOICE_COLUMNS:
            raise ValueError(
                f"{path}: header is {list(header)}; {INVOICE_FILENAME} must be "
                f"exactly {list(INVOICE_COLUMNS)} in that order "
                f"(itc invoice wire format 4.5)"
            )
        for number, row in enumerate(reader, start=2):
            yield number, row


def _paise(value: str, *, path: Path | str, line: int, column: str, row_id: str) -> Money:
    if "." in value:
        raise ValueError(
            f"{path}:{line} {column}={value!r} on {row_id}: money is an integer "
            f"number of paise and may not contain a '.' (money wire format 1.1)"
        )
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"{path}:{line} {column}={value!r} on {row_id}: not an integer"
        ) from exc


def read_invoice(path: Path | str) -> list[GstInvoice]:
    """Parse `psp_gst_invoice.csv`. Raises if the file is absent or malformed.

    Order is the file's, which the generator writes in calendar order. Nothing
    downstream depends on that -- `reconcile()` sorts its own periods -- but a
    reader that silently reordered would make a diff of two datasets unreadable.
    """
    invoices: list[GstInvoice] = []
    for line, row in _rows(path):
        invoice_no = row["invoice_no"]
        invoices.append(
            GstInvoice(
                invoice_no=invoice_no,
                period=row["period"],
                taxable_value=_paise(
                    row["taxable_value"],
                    path=path,
                    line=line,
                    column="taxable_value",
                    row_id=invoice_no,
                ),
                gst_amount=_paise(
                    row["gst_amount"],
                    path=path,
                    line=line,
                    column="gst_amount",
                    row_id=invoice_no,
                ),
                gstin=row["gstin"],
                invoice_date=date.fromisoformat(row["invoice_date"]),
            )
        )
    return invoices


def load_invoice(directory: Path | str) -> list[GstInvoice] | None:
    """The invoice rows for a dataset directory, or **`None` when it has none**.

    The one place the file's optionality is spelled out, and `None` rather than
    `[]` is the whole point of the signature. Three states have to stay
    distinguishable and only two of them are about periods:

    * **no file** -- the dataset was generated before this capability, or the
      operator never supplied the PSP's invoice. Nothing can be said about input
      tax credit either way, and the caller reports zero. `None`.
    * **a file covering no periods** -- a document that claims nothing over
      months that carry settlements. That is a finding, not an absence, and
      every period reads `no_invoice`. `[]`.
    * **a file missing one period's row** -- the `missing_gst_invoice` defect.
      That period reads `no_invoice` and the rest reconcile.

    Collapsing the first into the second is the trap: it would make a run over a
    dataset with no invoice report the whole month's GST "at risk", which is a
    claim about the operator's tax position derived from a document they simply
    did not provide. A malformed file still raises -- absent and broken are also
    different facts and must not read the same.
    """
    path = Path(directory) / INVOICE_FILENAME
    if not path.exists():
        return None
    return read_invoice(path)
