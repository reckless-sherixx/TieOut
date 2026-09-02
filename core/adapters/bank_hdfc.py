"""HDFC Bank account-statement export -> canonical `BankLine`.

**Schema provenance.** Header
`Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing
Balance`, dates as `dd/MM/yy`. The column set was corroborated against
descriptions of HDFC statement exports found in search (see
ADAPTERS-REPORT.md); it was NOT read off an official HDFC schema document,
because no such document is published. Treat the column names as high
confidence and the two-digit year as the item most worth checking against a
real export.

**Why this is its own adapter and not an option on a shared one.** HDFC and
ICICI differ in three ways that all reach into the mapping: the narration
column is called something else, the dates are punctuated and ordered
differently, and direction is spelled differently. A single adapter carrying
those as flags would have three independent switches, eight nominal
combinations, and only two that are real -- and every reader would have to
work out which two. One class per layout costs a file and buys a mapping you
can read top to bottom.

**Direction.** HDFC uses two columns and writes `0.00` in the one that does not
apply, so "unused" is a zero rather than a blank. An exact zero on one side is
therefore read as absent; a non-zero value on both sides is `AMBIGUOUS_DIRECTION`
rather than a guess, and zero on both sides is the same -- a statement line that
moves no money is a defect worth a human's attention, not a `BankLine` with two
`None`s the matcher would have to special-case forever.

**Balance may be negative.** An overdrawn account is a real state, and the
canonical `BankLine.balance` is signed, so `-1234.56` passes straight through.

**Line identity comes from the file when the file carries one.** A statement
line has no natural key, so this adapter's default is positional --
`HDFC-<physical line number>`. That is deterministic, and it is also *invented
by the reader*, which means it cannot survive a round trip: ingesting an export
of a dataset does not give back the dataset's own line ids. So there is one
exception, and it is all-or-nothing. If **every** data row carries a distinct,
non-empty `Chq./Ref.No.`, the statement is giving each line its own reference
and that reference IS the line's identity. If any row leaves it blank, or two
rows share one -- which is what a real HDFC export looks like, where the column
holds `0000000000` on everything but the NEFT lines -- the whole file falls
back to positional. All-or-nothing rather than per-row, because a file whose
ids were half references and half positions would be a file where nobody can
say what an id means.

**UTR extraction is deliberately narrow.** `BankLine.utr` is `str | None` and
the canonicaliser downstream is what mines a messy narration properly. This
adapter lifts a reference only where HDFC's own narration grammar puts one --
the trailing token of an `NEFT`/`RTGS`/`IMPS`/`UPI` narration, or a token
following an explicit `UTR` marker -- and returns `None` otherwise. A greedier
regex here would invent UTRs out of invoice numbers and hand the matcher
confident nonsense, which is worse than a null.
"""

from __future__ import annotations

import re

from pathlib import Path

from core.adapters.base import (
    AdapterResult,
    CanonicalRecord,
    QuarantineReason,
    RawRow,
    iter_csv_rows,
    normalise_header,
    parse_date_exact,
    read_text,
    strip_comment_lines,
)
from core.adapters.csv_source import CsvSourceAdapter, RowError
from core.models import BankLine

#: HDFC writes a two-digit year: `01/08/26`. Only this one layout is accepted --
#: adding `%d/%m/%Y` as a courtesy would make `01/08/26` and `01/08/2026`
#: equally acceptable in one file, and a statement that mixes them is a
#: statement whose dates nobody has checked.
DATE_FORMATS = ("%d/%m/%y",)

#: The instrument prefixes whose HDFC narration ends in a bank reference.
_REFERENCE_PREFIXES = ("NEFT", "RTGS", "IMPS", "UPI", "MMT")

#: A bank reference: 10-22 characters, alphanumeric, containing at least one
#: digit. The digit requirement is what keeps `NEFT CR-HDFC0000123-RAZORPAY
#: SOFTWARE` from yielding "SOFTWARE".
_REFERENCE_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9]{10,22}$")

#: An explicit marker, which beats position when present.
_UTR_MARKER_RE = re.compile(r"\bUTR[:\s-]*([A-Za-z0-9]{10,22})\b", re.IGNORECASE)


def extract_utr(narration: str) -> str | None:
    """Lift a UTR out of an HDFC narration, or return `None`. Never guess."""
    marked = _UTR_MARKER_RE.search(narration or "")
    if marked:
        return marked.group(1)
    text = (narration or "").strip()
    if not text.upper().startswith(_REFERENCE_PREFIXES):
        return None
    tail = re.split(r"[-/]", text)[-1].strip()
    return tail if _REFERENCE_RE.match(tail) else None


class HDFCStatementAdapter(CsvSourceAdapter):
    """One row per statement line; `line_id` is carried or synthesised."""

    format_id = "bank-csv-hdfc-v1"
    format_version = "1.0"

    #: The column a carried line identity would live in. See the module
    #: docstring for the all-or-nothing rule that decides whether it is used.
    REFERENCE_COLUMN = "Chq./Ref.No."

    REQUIRED_COLUMNS = (
        "Date",
        "Narration",
        "Withdrawal Amt.",
        "Deposit Amt.",
        "Closing Balance",
    )
    #: `Chq./Ref.No.` and `Value Dt` are HDFC's alone -- ICICI spells the same
    #: two ideas `Cheque Number` and `Value Date`. They carry the separation
    #: between the two bank adapters, which is why they are distinctive rather
    #: than required: an export that omits them is still recognisably HDFC.
    DISTINCTIVE_COLUMNS = ("Chq./Ref.No.", "Value Dt")

    #: Row number -> the reference that row carries, or `None` when this file
    #: does not give every line a distinct one. Set for the duration of one
    #: `parse` and cleared afterwards, so no state survives between files.
    _carried_ids: dict[int, str] | None = None

    def parse(self, path: Path | str) -> AdapterResult:
        """Pre-scan for carried line identities, then parse as usual.

        The scan is a second read of the file rather than a hook inside the
        shared loop, because the question it answers -- "does EVERY row carry a
        distinct reference" -- is a property of the whole file and cannot be
        decided while the first row is being converted. `UndecodableFileError`
        surfaces here exactly as it would from the parse below it.
        """
        self._carried_ids = self._scan_references(path)
        try:
            return super().parse(path)
        finally:
            self._carried_ids = None

    def _scan_references(self, path: Path | str) -> dict[int, str] | None:
        """`{row_number: reference}` if every row carries a distinct one, else
        `None`."""
        text, _encoding, _raw = read_text(path)
        body, comments = strip_comment_lines(text)
        rows = iter_csv_rows(body, start_line=comments + 1)

        header = next(rows, None)
        if header is None:
            return None
        columns = [normalise_header(cell) for cell in header.cells]
        wanted = normalise_header(self.REFERENCE_COLUMN)
        if wanted not in columns:
            return None
        index = columns.index(wanted)

        carried: dict[int, str] = {}
        seen: set[str] = set()
        for row in rows:
            if not row.cells or all(not cell.strip() for cell in row.cells):
                continue
            if len(row.cells) != len(columns):
                # Cut short or over-long: the shared loop quarantines it and it
                # never becomes a record, so it cannot carry an identity either.
                continue
            reference = row.cells[index].strip()
            if not reference or reference in seen:
                return None
            seen.add(reference)
            carried[row.row_number] = reference
        return carried or None

    def records_from_row(
        self, cells: dict[str, str], row: RawRow
    ) -> list[CanonicalRecord]:
        try:
            txn_date = parse_date_exact(
                self.required_text(cells, "Date"), DATE_FORMATS
            )
        except ValueError as error:
            raise RowError(
                QuarantineReason.BAD_DATE, f"column 'Date': {error}"
            ) from error

        # NOT stripped: doubled spaces inside an HDFC narration are data, and
        # the canonicaliser downstream is what normalises them. Stripping here
        # would silently change the input to a component that has its own tests
        # about exactly this.
        narration = cells.get("narration") or ""
        if not narration.strip():
            raise RowError(
                QuarantineReason.MISSING_VALUE,
                "column 'Narration' is required but empty",
            )

        withdrawal = self.required_paise(cells, "Withdrawal Amt.")
        deposit = self.required_paise(cells, "Deposit Amt.")
        balance = self.required_paise(cells, "Closing Balance")

        if withdrawal and deposit:
            raise RowError(
                QuarantineReason.AMBIGUOUS_DIRECTION,
                f"both 'Withdrawal Amt.' ({withdrawal} paise) and 'Deposit Amt.' "
                f"({deposit} paise) are non-zero; HDFC writes 0.00 in the unused "
                f"column, so this line's direction cannot be read",
            )
        if not withdrawal and not deposit:
            raise RowError(
                QuarantineReason.AMBIGUOUS_DIRECTION,
                "'Withdrawal Amt.' and 'Deposit Amt.' are both zero, so the line "
                "moves no money",
            )

        return [
            BankLine(
                # The reference this statement gave the line, when it gave
                # every line a distinct one; otherwise positional and
                # file-local. On a real HDFC export `Chq./Ref.No.` is blank or
                # `0000000000` on most lines and repeats on the rest, so the
                # positional form is what a real statement gets. Determinism
                # comes from position; identity across uploads comes from the
                # row fingerprint, which is the thing built for it.
                line_id=self._line_id(row),
                txn_date=txn_date,
                narration=narration,
                credit=deposit or None,
                debit=withdrawal or None,
                balance=balance,
                utr=extract_utr(narration),
            )
        ]

    def _line_id(self, row: RawRow) -> str:
        carried = (self._carried_ids or {}).get(row.row_number)
        return carried if carried else f"HDFC-{row.row_number:05d}"
