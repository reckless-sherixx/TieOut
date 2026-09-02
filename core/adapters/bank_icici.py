"""ICICI Bank account-statement export -> canonical `BankLine`.

**Schema provenance.** Header `S No.,Value Date,Transaction Date,Cheque
Number,Transaction Remarks,Withdrawal Amount (INR ),Deposit Amount (INR
),Balance (INR )`, dates as `dd-mm-yyyy`, and balances suffixed `Cr` or `Dr`.
This layout is from knowledge and is the LEAST verified of the three formats in
this phase: ICICI publishes no export schema, retail and corporate net-banking
produce different files, and search returned only converter vendors describing
normalised output rather than ICICI's own headers. It is marked UNVERIFIED in
ADAPTERS-REPORT.md and is the first thing to check against a real export.

**What actually differs from HDFC, and why it needed its own adapter.**

* The narration column is `Transaction Remarks`, not `Narration`.
* Dates are `01-08-2026`, not `01/08/26`. Both banks put the day first, so a
  permissive parser would appear to work on both and be wrong the moment
  either changes; each adapter therefore accepts exactly one layout.
* There are two date columns and they are not interchangeable. `Value Date` is
  when the money became available; `Transaction Date` is when it moved.
  `BankLine.txn_date` is the second one, because that is what the narration and
  the settlement it corresponds to are stamped with.
* **The balance carries its own direction.** ICICI writes `1245678.90 Cr` and,
  when overdrawn, `4500.00 Dr` -- a debit balance is a negative balance. This
  is the DR/CR convention HDFC does not have (HDFC signs the balance directly),
  and it is exactly the kind of thing that a shared adapter with a flag would
  get wrong once and then get wrong quietly: a `Dr` suffix silently dropped
  turns an overdraft into a credit of the same size.

The amount columns themselves are still withdrawal/deposit pairs, so the
suffix is read only on the balance -- and a suffix appearing on an amount
column is `UNKNOWN_VALUE`, not something to be helpfully tolerated.

**`S No.` is not the line id.** It restarts at 1 in every export and is absent
in some, so `line_id` is synthesised from position exactly as it is for HDFC,
and cross-upload identity comes from the row fingerprint.
"""

from __future__ import annotations

import re

from core.adapters.base import (
    CanonicalRecord,
    QuarantineReason,
    RawRow,
    parse_date_exact,
    parse_paise,
)
from core.adapters.csv_source import CsvSourceAdapter, RowError
from core.models import BankLine

#: ICICI writes a four-digit year with hyphens: `01-08-2026`.
DATE_FORMATS = ("%d-%m-%Y",)

#: `1245678.90 Cr`, `4500.00 Dr`, or a bare number. The suffix is optional
#: because not every ICICI export writes it, and a bare balance is read as a
#: credit balance -- which is what an account normally has.
_BALANCE_RE = re.compile(r"^(?P<amount>.+?)\s*(?P<direction>CR|DR)?$", re.IGNORECASE)

#: An explicit UTR/RRN marker in an ICICI remark. ICICI's remarks are more
#: structured than HDFC's -- `NEFT-N260801123456789-RAZORPAY SOFTWARE` puts the
#: reference second, not last -- so the extraction rule genuinely differs and is
#: not a shared helper pretending to be one.
_ICICI_REFERENCE_RE = re.compile(
    r"\b(?:NEFT|RTGS|IMPS|UPI|MMT)[-/]([A-Za-z0-9]{10,22})\b", re.IGNORECASE
)
_UTR_MARKER_RE = re.compile(r"\bUTR[:\s-]*([A-Za-z0-9]{10,22})\b", re.IGNORECASE)


def extract_utr(remarks: str) -> str | None:
    """Lift a reference out of an ICICI remark, or return `None`.

    Same discipline as the HDFC extractor and deliberately not the same rule:
    ICICI puts the reference in the second hyphen-delimited field, HDFC in the
    last. Sharing one regex between them would mean one that is loose enough
    for both, which is one that is wrong on each.
    """
    marked = _UTR_MARKER_RE.search(remarks or "")
    if marked:
        return marked.group(1)
    match = _ICICI_REFERENCE_RE.search(remarks or "")
    if match and any(character.isdigit() for character in match.group(1)):
        return match.group(1)
    return None


class ICICIStatementAdapter(CsvSourceAdapter):
    format_id = "bank-csv-icici-v1"
    format_version = "1.0"

    REQUIRED_COLUMNS = (
        "Transaction Date",
        "Transaction Remarks",
        "Withdrawal Amount (INR)",
        "Deposit Amount (INR)",
        "Balance (INR)",
    )
    #: ICICI's alone: HDFC has no `S No.` and calls the other two `Value Dt`
    #: and `Chq./Ref.No.`. These are what break a tie between the two bank
    #: adapters on a file that is genuinely one of them.
    DISTINCTIVE_COLUMNS = ("S No.", "Value Date", "Cheque Number")

    def _balance_paise(self, cells: dict[str, str]) -> int:
        raw = (cells.get("balance(inr)") or "").strip()
        if not raw:
            raise RowError(
                QuarantineReason.MISSING_VALUE,
                "column 'Balance (INR)' is required but empty",
            )
        match = _BALANCE_RE.match(raw)
        if match is None:  # pragma: no cover - the pattern matches any string
            raise RowError(
                QuarantineReason.BAD_DECIMAL,
                f"column 'Balance (INR)' value {raw!r} is unreadable",
            )
        try:
            magnitude = parse_paise(match.group("amount"))
        except ValueError as error:
            raise RowError(
                QuarantineReason.BAD_DECIMAL, f"column 'Balance (INR)': {error}"
            ) from error
        direction = (match.group("direction") or "CR").upper()
        # A Dr balance is an overdrawn account. Dropping the suffix would turn
        # it into a credit of the same size -- the single most expensive thing
        # this adapter could get wrong.
        return -magnitude if direction == "DR" else magnitude

    def _amount_paise(self, cells: dict[str, str], column: str) -> int:
        raw = (cells.get(self._key(column)) or "").strip()
        if raw and raw.upper().endswith(("CR", "DR")):
            raise RowError(
                QuarantineReason.UNKNOWN_VALUE,
                f"column {column!r} value {raw!r} carries a Cr/Dr suffix; in this "
                f"layout only 'Balance (INR)' does, and direction on an amount "
                f"column means the file is not the layout it appears to be",
            )
        return self.required_paise(cells, column)

    @staticmethod
    def _key(column: str) -> str:
        from core.adapters.base import normalise_header

        return normalise_header(column)

    def records_from_row(
        self, cells: dict[str, str], row: RawRow
    ) -> list[CanonicalRecord]:
        try:
            txn_date = parse_date_exact(
                self.required_text(cells, "Transaction Date"), DATE_FORMATS
            )
        except ValueError as error:
            raise RowError(
                QuarantineReason.BAD_DATE, f"column 'Transaction Date': {error}"
            ) from error

        remarks = cells.get("transaction remarks") or ""
        if not remarks.strip():
            raise RowError(
                QuarantineReason.MISSING_VALUE,
                "column 'Transaction Remarks' is required but empty",
            )

        withdrawal = self._amount_paise(cells, "Withdrawal Amount (INR)")
        deposit = self._amount_paise(cells, "Deposit Amount (INR)")
        balance = self._balance_paise(cells)

        if withdrawal and deposit:
            raise RowError(
                QuarantineReason.AMBIGUOUS_DIRECTION,
                f"both 'Withdrawal Amount (INR)' ({withdrawal} paise) and "
                f"'Deposit Amount (INR)' ({deposit} paise) are non-zero; this "
                f"line's direction cannot be read",
            )
        if not withdrawal and not deposit:
            raise RowError(
                QuarantineReason.AMBIGUOUS_DIRECTION,
                "'Withdrawal Amount (INR)' and 'Deposit Amount (INR)' are both "
                "zero, so the line moves no money",
            )

        return [
            BankLine(
                line_id=f"ICICI-{row.row_number:05d}",
                txn_date=txn_date,
                narration=remarks,
                credit=deposit or None,
                debit=withdrawal or None,
                balance=balance,
                utr=extract_utr(remarks),
            )
        ]
