"""The parse loop every CSV adapter shares, so no adapter reimplements safety.

All three phase-one formats are comma-separated text with a header row. What
differs between them is which columns exist, what the dates look like and how
direction is spelled -- and *those* differences are the whole point of having
one adapter per layout. What must not differ is the harness around them: read
the bytes, decode them, validate the header, walk the rows, quarantine what
cannot be converted, hash what can, and never raise past the caller.

So the harness lives here once and the layouts subclass it. A new adapter
supplies four things -- its columns, its identity, and `records_from_row` -- and
inherits quarantine, duplicate detection, hashing and encoding handling it
cannot accidentally get wrong. The alternative, three copies of this loop, is
three places for "never silently drop" to quietly stop being true.

`records_from_row` signals a bad row by raising `RowError(reason, detail)`.
That keeps each layout's mapping readable as a straight line of conversions
instead of a ladder of `if not ok: return None` -- and it means a mapping that
forgets to handle something raises `ValidationError` from the canonical model,
which this loop catches as `SCHEMA_VIOLATION` rather than letting it escape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import ValidationError

from core.adapters.base import (
    AdapterResult,
    CanonicalRecord,
    QuarantinedRow,
    QuarantineReason,
    RawRow,
    UndecodableFileError,
    header_confidence,
    iter_csv_rows,
    normalise_header,
    parse_paise,
    read_text,
    row_fingerprint,
    sha256_bytes,
    strip_comment_lines,
)


class RowError(Exception):
    """A row cannot become a record, and here is exactly why.

    Carries the machine-readable `reason` the quarantine screen groups on and a
    human `detail` naming the column and the offending value -- both, because
    "BAD_DECIMAL on row 41" without the value is a support ticket and
    "column 'tax' value '23.605' has sub-paise precision" is a fix.
    """

    def __init__(self, reason: QuarantineReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class CsvSourceAdapter(ABC):
    """Base for a comma-separated real-file layout."""

    #: Columns without which this is simply not the format. Missing any one of
    #: them scores 0.0 in `sniff` and is a file-level quarantine in `parse`.
    REQUIRED_COLUMNS: tuple[str, ...] = ()
    #: Columns that separate this layout from its nearest neighbour -- HDFC's
    #: `Narration` against ICICI's `Transaction Remarks`. They raise confidence
    #: above the shared floor, which is what stops two bank adapters tying.
    DISTINCTIVE_COLUMNS: tuple[str, ...] = ()

    format_id: str
    format_version: str

    # --- the contract -------------------------------------------------------

    def sniff(self, head: bytes) -> float:
        return header_confidence(head, self.REQUIRED_COLUMNS, self.DISTINCTIVE_COLUMNS)

    def parse(self, path: Path | str) -> AdapterResult:
        """Read the whole file. Raises only `UndecodableFileError`.

        Every other failure -- an unreadable header, a row that will not
        convert, a row seen twice -- comes back inside the result as
        quarantine. `UndecodableFileError` is the exception because there is no
        row to attach it to and no partial result to hand back; the caller
        records it as a file-level quarantine.
        """
        text, encoding, raw_bytes = read_text(path)
        file_sha256 = sha256_bytes(raw_bytes)
        body, comment_lines = strip_comment_lines(text)
        rows = iter_csv_rows(body, start_line=comment_lines + 1)

        records: list[CanonicalRecord] = []
        quarantined: list[QuarantinedRow] = []
        row_hashes: list[str] = []
        skipped = 0

        header = next(rows, None)
        if header is None:
            return self._result(
                records,
                [
                    QuarantinedRow(
                        row_number=comment_lines + 1,
                        raw="",
                        reason=QuarantineReason.MISSING_HEADER_COLUMN,
                        detail="the file has no header row at all",
                    )
                ],
                row_hashes,
                file_sha256,
                encoding,
                skipped,
            )

        columns = [normalise_header(cell) for cell in header.cells]
        missing = [
            column
            for column in self.REQUIRED_COLUMNS
            if normalise_header(column) not in set(columns)
        ]
        if missing:
            return self._result(
                records,
                [
                    QuarantinedRow(
                        row_number=header.row_number,
                        raw=header.raw,
                        reason=QuarantineReason.MISSING_HEADER_COLUMN,
                        detail=(
                            f"header is missing required column(s) {missing}; "
                            f"{self.format_id} needs {list(self.REQUIRED_COLUMNS)}"
                        ),
                    )
                ],
                row_hashes,
                file_sha256,
                encoding,
                skipped,
            )

        seen: dict[str, int] = {}
        for row in rows:
            if not row.cells or all(not cell.strip() for cell in row.cells):
                skipped += 1
                continue
            if len(row.cells) < len(columns):
                quarantined.append(
                    QuarantinedRow(
                        row_number=row.row_number,
                        raw=row.raw,
                        reason=QuarantineReason.TRUNCATED_ROW,
                        detail=(
                            f"row has {len(row.cells)} field(s), header has "
                            f"{len(columns)}; the line is cut short"
                        ),
                    )
                )
                continue
            if len(row.cells) > len(columns):
                quarantined.append(
                    QuarantinedRow(
                        row_number=row.row_number,
                        raw=row.raw,
                        reason=QuarantineReason.EXTRA_FIELDS,
                        detail=(
                            f"row has {len(row.cells)} field(s), header has "
                            f"{len(columns)}; most likely an unquoted comma "
                            f"inside a text column"
                        ),
                    )
                )
                continue

            cells = dict(zip(columns, row.cells, strict=True))
            # Before the duplicate check, deliberately. A layout that repeats a
            # key on its own continuation rows -- Shopify writes an order's
            # extra line items as further rows carrying the same `Name` -- would
            # otherwise have every continuation reported as a duplicate of the
            # order it belongs to.
            if self.is_skipped_row(cells, row):
                skipped += 1
                continue
            key = self.duplicate_key(cells, row)
            if key in seen:
                quarantined.append(
                    QuarantinedRow(
                        row_number=row.row_number,
                        raw=row.raw,
                        reason=QuarantineReason.DUPLICATE_ROW,
                        detail=f"identical to the row already read at line {seen[key]}",
                    )
                )
                continue
            seen[key] = row.row_number

            try:
                produced = self.records_from_row(cells, row)
            except RowError as error:
                quarantined.append(
                    QuarantinedRow(
                        row_number=row.row_number,
                        raw=row.raw,
                        reason=error.reason,
                        detail=error.detail,
                    )
                )
                continue
            except ValidationError as error:
                quarantined.append(
                    QuarantinedRow(
                        row_number=row.row_number,
                        raw=row.raw,
                        reason=QuarantineReason.SCHEMA_VIOLATION,
                        detail=f"canonical model rejected the row: {error.error_count()} error(s)",
                    )
                )
                continue

            for record in produced:
                records.append(record)
                row_hashes.append(row_fingerprint(self.format_id, record))

        return self._result(
            records, quarantined, row_hashes, file_sha256, encoding, skipped
        )

    def _result(
        self,
        records: list[CanonicalRecord],
        quarantined: list[QuarantinedRow],
        row_hashes: list[str],
        file_sha256: str,
        encoding: str,
        skipped: int,
    ) -> AdapterResult:
        return AdapterResult(
            format_id=self.format_id,
            format_version=self.format_version,
            records=records,
            quarantined=quarantined,
            file_sha256=file_sha256,
            row_hashes=row_hashes,
            encoding=encoding,
            skipped_rows=skipped,
        )

    # --- what a layout supplies --------------------------------------------

    def is_skipped_row(self, cells: dict[str, str], row: RawRow) -> bool:
        """Is this row structurally not a record at all? `False`, unless
        overridden.

        Distinct from quarantine, and the distinction matters. A quarantined
        row is one that SHOULD have become a record and could not -- somebody
        has to look at it. A skipped row is one the layout says is not a record
        in the first place: a trailing "Statement Summary" block, or a Shopify
        line-item continuation whose order was already emitted from the row
        above. Counting those as quarantine would bury the real defects in a
        review queue full of rows that are working exactly as intended.

        They are still counted, in `AdapterResult.skipped_rows`, so that
        `data rows == records + quarantined + skipped` always closes and
        "silently dropped" stays impossible.
        """
        return False

    def duplicate_key(self, cells: dict[str, str], row: RawRow) -> str:
        """What makes two rows "the same row". Raw text, unless overridden.

        For a bank statement the raw line is exactly right: two byte-identical
        lines including the running balance cannot both be real, because the
        balance would have moved. A layout with a genuine transaction id
        overrides this to use it, so a re-exported duplicate with different
        whitespace is still caught.
        """
        return row.raw

    @abstractmethod
    def records_from_row(
        self, cells: dict[str, str], row: RawRow
    ) -> list[CanonicalRecord]:
        """Turn one row into zero or more canonical records, or raise `RowError`."""

    # --- helpers for `records_from_row` ------------------------------------

    @staticmethod
    def required_text(cells: dict[str, str], column: str) -> str:
        value = (cells.get(normalise_header(column)) or "").strip()
        if not value:
            raise RowError(
                QuarantineReason.MISSING_VALUE,
                f"column {column!r} is required but empty",
            )
        return value

    @staticmethod
    def optional_text(cells: dict[str, str], column: str) -> str | None:
        """Absent means the empty string -- never the words "None" or "NULL".

        A cell holding the literal text "NULL" is not an absent value, it is an
        export tool's bug, and it comes back as the string "NULL" so that a
        downstream mismatch is traceable to the file rather than silently
        becoming a missing reference.
        """
        value = (cells.get(normalise_header(column)) or "").strip()
        return value or None

    @staticmethod
    def required_paise(cells: dict[str, str], column: str) -> int:
        raw = (cells.get(normalise_header(column)) or "").strip()
        if not raw:
            raise RowError(
                QuarantineReason.MISSING_VALUE,
                f"amount column {column!r} is required but empty",
            )
        try:
            return parse_paise(raw)
        except ValueError as error:
            raise RowError(
                QuarantineReason.BAD_DECIMAL, f"column {column!r}: {error}"
            ) from error

    @staticmethod
    def optional_paise(cells: dict[str, str], column: str) -> int | None:
        raw = (cells.get(normalise_header(column)) or "").strip()
        if not raw:
            return None
        try:
            return parse_paise(raw)
        except ValueError as error:
            raise RowError(
                QuarantineReason.BAD_DECIMAL, f"column {column!r}: {error}"
            ) from error


__all__ = ["CsvSourceAdapter", "RowError", "UndecodableFileError"]
