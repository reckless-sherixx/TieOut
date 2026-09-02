"""CSV and `truth.json` emission (Task A.4).

The wire format is `docs/CSV_SCHEMAS.md`, and it is Lane B's ingest contract:
column order, `YYYY-MM-DD` / `YYYY-MM-DDTHH:MM:SS`, empty string for absent,
integer paise with no `.` anywhere.

**Byte-identity is the point of this module.** Two runs at the same seed must
produce the same bytes on Linux and on Windows, so:

* every file is opened with an explicit newline translation -- `newline=""` plus
  `lineterminator="\\n"` for CSV, `newline="\\n"` for JSON. Without those,
  Python's text layer writes CRLF on Windows and the claim fails on the machine
  the demo is recorded on;
* nothing is written from a `set` or a `dict` whose order came from one. Every
  list is sorted by an explicit key, and `json.dump` uses `sort_keys=True`.

`truth.json` is built from the in-memory `Batch` objects, never by re-reading
the CSVs. That is CSV_SCHEMAS 5.1: a `missing_order_ref` row has had its
`order_id` blanked on purpose, and scraping the emitted rows would produce a
truth file that penalises a matcher for recovering the order correctly.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from pathlib import Path

from core.models import BankLine, Order, PSPTransaction

from .batches import OPENING_BALANCE, Batch
from .defects import InjectionResult
from .itc import INVOICE_COLUMNS, GstInvoice, build_invoice

ORDER_COLUMNS = (
    "order_id",
    "order_date",
    "customer_ref",
    "gross_amount",
    "currency",
    "status",
)
PSP_COLUMNS = (
    "txn_id",
    "txn_type",
    "order_id",
    "captured_at",
    "amount",
    "settlement_id",
    "settled_at",
)
BANK_COLUMNS = (
    "line_id",
    "txn_date",
    "narration",
    "credit",
    "debit",
    "balance",
    "utr",
)


# --- field rendering --------------------------------------------------------


def _date(value: date | None) -> str:
    return "" if value is None else f"{value:%Y-%m-%d}"


def _datetime(value: datetime | None) -> str:
    return "" if value is None else f"{value:%Y-%m-%dT%H:%M:%S}"


def _int(value: int | None) -> str:
    return "" if value is None else str(int(value))


def _text(value: str | None) -> str:
    return "" if value is None else value


# --- ordering ---------------------------------------------------------------


def _txn_sort_key(txn: PSPTransaction) -> int:
    """Allocation order. Every id is `<prefix>_<n>` from one shared allocator,
    so this groups each settlement's legs together and keeps a refund or a
    duplicate next to the batch it was injected into."""
    return int(txn.txn_id.rsplit("_", 1)[1])


def _bank_sort_key(line: BankLine) -> tuple[date, str]:
    """Statement order is chronological; `line_id` breaks ties (CSV_SCHEMAS 4)."""
    return (line.txn_date, line.line_id)


# --- writers ----------------------------------------------------------------


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)


def _write_json(path: Path, payload: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.write("\n")


def _order_row(order: Order) -> tuple[str, ...]:
    return (
        order.order_id,
        _date(order.order_date),
        order.customer_ref,
        _int(order.gross_amount),
        order.currency,
        order.status,
    )


def _psp_row(txn: PSPTransaction) -> tuple[str, ...]:
    return (
        txn.txn_id,
        txn.txn_type,
        _text(txn.order_id),
        _datetime(txn.captured_at),
        _int(txn.amount),
        _text(txn.settlement_id),
        _date(txn.settled_at),
    )


def _invoice_row(invoice: GstInvoice) -> tuple[str, ...]:
    return (
        invoice.invoice_no,
        invoice.period,
        _int(invoice.taxable_value),
        _int(invoice.gst_amount),
        invoice.gstin,
        _date(invoice.invoice_date),
    )


def _bank_row(line: BankLine) -> tuple[str, ...]:
    return (
        line.line_id,
        _date(line.txn_date),
        line.narration,
        _int(line.credit),
        _int(line.debit),
        _int(line.balance),
        _text(line.utr),
    )


def _chain_balances(lines: Sequence[BankLine]) -> None:
    """`balance` is a running chain from `OPENING_BALANCE`, in statement order."""
    balance = OPENING_BALANCE
    for line in lines:
        balance += (line.credit or 0) - (line.debit or 0)
        line.balance = balance


# --- truth ------------------------------------------------------------------


def build_truth(
    batches: Sequence[Batch],
    injections: Sequence[InjectionResult],
    *,
    seed: int,
    record_count: int,
) -> dict:
    """The `truth.json` payload (CSV_SCHEMAS 5).

    One linkage per bank line that has a true settlement. A `split_settlement`
    therefore contributes **two** entries naming the same settlement, the same
    PSP rows and the same orders -- which is the true answer for both lines, and
    the only shape a scorer can grade a split against.
    """
    linkages = []
    for batch in batches:
        linkage = batch.linkage
        psp_txn_ids = sorted(set(linkage.psp_txn_ids))
        order_ids = sorted(set(linkage.order_ids))
        for line in batch.all_bank_lines:
            linkages.append(
                {
                    "bank_line_id": line.line_id,
                    "settlement_id": batch.settlement_id,
                    "psp_txn_ids": psp_txn_ids,
                    "order_ids": order_ids,
                }
            )
    linkages.sort(key=lambda entry: entry["bank_line_id"])

    return {
        "seed": seed,
        "record_count": record_count,
        "linkages": linkages,
        "injected_defects": [
            {
                "defect_type": result.defect_type,
                "affected_ids": list(result.affected_ids),
                "resolvable": result.resolvable,
            }
            for result in injections
        ],
        "unresolvable_ids": sorted(
            {
                subject_id
                for result in injections
                if not result.resolvable
                for subject_id in result.affected_ids
            }
        ),
    }


# --- entry point ------------------------------------------------------------


def emit_dataset(
    batches: Sequence[Batch],
    injections: Sequence[InjectionResult],
    out_dir: Path | str,
    seed: int,
) -> None:
    """Write the five dataset files into `out_dir`.

    `orders.csv`, `psp.csv`, `bank.csv`, `psp_gst_invoice.csv` and `truth.json`.

    The GST invoice is built here rather than in `build_dataset` for one
    reason: `build_dataset` returns `(batches, injections)` and four callers --
    the CLI, the API's `generate_dataset`, and two test modules -- destructure
    that pair. The invoice is a pure function of finished batches plus the seed,
    so deriving it at emit time changes no signature, and the two labels it
    produces join the batch injectors' own before `truth.json` is written. One
    list of injected defects, as CSV_SCHEMAS 5 describes it, not two.
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

    for line in lines:
        if "," in line.narration or "\n" in line.narration:
            raise ValueError(
                f"{line.line_id}: a narration may not contain a comma or a newline "
                "-- CSV_SCHEMAS 1.4 keeps the fixtures quote-free and readable"
            )

    invoices, invoice_injections = build_invoice(batches, seed=seed)

    _write_csv(out / "orders.csv", ORDER_COLUMNS, [_order_row(o) for o in orders])
    _write_csv(out / "psp.csv", PSP_COLUMNS, [_psp_row(t) for t in txns])
    _write_csv(out / "bank.csv", BANK_COLUMNS, [_bank_row(line) for line in lines])
    _write_csv(
        out / "psp_gst_invoice.csv",
        INVOICE_COLUMNS,
        [_invoice_row(invoice) for invoice in invoices],
    )
    _write_json(
        out / "truth.json",
        build_truth(
            batches,
            [*injections, *invoice_injections],
            seed=seed,
            record_count=len(orders),
        ),
    )
