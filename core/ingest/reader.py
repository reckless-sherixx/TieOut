"""Strict CSV ingest for the three input sources (docs/CSV_SCHEMAS.md).

Three rules, all of them load-bearing:

1. **Money is an integer number of paise.** A `.` in an amount column is a hard
   `ValueError` naming the file, the line, the column and the row's id -- never
   a silent `float()` coercion. A silent coercion here would put a float into
   `Money` and quietly corrupt every downstream total.
2. **An absent optional value is the empty string** and becomes `None`. It is
   never the literal text `None`, `NULL`, `nan` or `-`.
3. **Parsing is `csv.DictReader` over a handle opened with `newline=""`.**
   `.gitattributes` pins the fixture CSVs to LF, but Git may still check them
   out as CRLF on Windows; `newline=""` handles both. Never `str.split(",")` --
   and `skipinitialspace` stays at its default `False` because the doubled
   spaces inside `narration` are data.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

from core.models import BankLine, Order, PSPTransaction
from core.money import Money

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


def _read_rows(path: Path | str, required: tuple[str, ...]) -> Iterator[tuple[int, dict[str, str]]]:
    """Yield `(file_line_number, row)` pairs, header validated up front.

    The line number counts the header as line 1, so it is the number a human
    sees in an editor -- which is the whole point of putting it in the error.
    """
    path = Path(path)
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        missing = [column for column in required if column not in header]
        if missing:
            raise ValueError(
                f"{path}: missing required column(s) {missing}; "
                f"expected header {list(required)}, got {header}"
            )
        for line_no, row in enumerate(reader, start=2):
            yield line_no, row


def _text(row: dict[str, str], column: str) -> str:
    return (row.get(column) or "").strip()


def _optional_text(row: dict[str, str], column: str) -> str | None:
    """The empty string is how the wire format spells "absent" (schemas 1.3)."""
    value = _text(row, column)
    return value or None


def _raw_narration(row: dict[str, str]) -> str:
    """Narration is NOT stripped: doubled and tripled spaces are part of the
    data and the canonicaliser is what normalises them."""
    return row.get("narration") or ""


def _paise(
    row: dict[str, str],
    column: str,
    *,
    path: Path | str,
    line_no: int,
    row_id: str,
    required: bool,
) -> Money | None:
    raw = _text(row, column)
    if not raw:
        if required:
            raise ValueError(
                f"{path} line {line_no} ({row_id}): column {column!r} is required "
                f"and must be an integer paise amount, but it is empty"
            )
        return None
    if "." in raw or not _is_integer_literal(raw):
        raise ValueError(
            f"{path} line {line_no} ({row_id}): column {column!r} value {raw!r} "
            f"is not integer paise -- amounts are written as an integer number "
            f"of paise with no decimal point, separator or currency symbol"
        )
    return int(raw)


def _is_integer_literal(raw: str) -> bool:
    body = raw[1:] if raw[0] in "+-" else raw
    return bool(body) and body.isdigit()


def read_orders(path: Path | str) -> list[Order]:
    """Read `orders.csv` -- the sales register, the reference spine."""
    orders: list[Order] = []
    for line_no, row in _read_rows(path, ORDER_COLUMNS):
        row_id = _text(row, "order_id")
        orders.append(
            Order(
                order_id=row_id,
                order_date=_text(row, "order_date"),
                customer_ref=_text(row, "customer_ref"),
                gross_amount=_paise(
                    row,
                    "gross_amount",
                    path=path,
                    line_no=line_no,
                    row_id=row_id,
                    required=True,
                ),
                currency=_text(row, "currency"),
                status=_text(row, "status"),
            )
        )
    return orders


def read_psp(path: Path | str) -> list[PSPTransaction]:
    """Read `psp.csv` -- amounts are SIGNED from the merchant's point of view,
    so the reconstructed net of a settlement is a plain sum (schemas 3.1)."""
    txns: list[PSPTransaction] = []
    for line_no, row in _read_rows(path, PSP_COLUMNS):
        row_id = _text(row, "txn_id")
        txns.append(
            PSPTransaction(
                txn_id=row_id,
                txn_type=_text(row, "txn_type"),
                order_id=_optional_text(row, "order_id"),
                captured_at=_text(row, "captured_at"),
                amount=_paise(
                    row,
                    "amount",
                    path=path,
                    line_no=line_no,
                    row_id=row_id,
                    required=True,
                ),
                settlement_id=_optional_text(row, "settlement_id"),
                settled_at=_optional_text(row, "settled_at"),
            )
        )
    return txns


def read_bank(path: Path | str) -> list[BankLine]:
    """Read `bank.csv` -- `credit` and `debit` are UNSIGNED magnitudes and
    direction is carried by which column is populated (schemas 4). Do not
    conflate this with the signed convention in `psp.csv`."""
    lines: list[BankLine] = []
    for line_no, row in _read_rows(path, BANK_COLUMNS):
        row_id = _text(row, "line_id")
        lines.append(
            BankLine(
                line_id=row_id,
                txn_date=_text(row, "txn_date"),
                narration=_raw_narration(row),
                credit=_paise(
                    row, "credit", path=path, line_no=line_no, row_id=row_id, required=False
                ),
                debit=_paise(
                    row, "debit", path=path, line_no=line_no, row_id=row_id, required=False
                ),
                balance=_paise(
                    row, "balance", path=path, line_no=line_no, row_id=row_id, required=True
                ),
                utr=_optional_text(row, "utr"),
            )
        )
    return lines
