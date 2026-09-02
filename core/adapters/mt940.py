"""SWIFT MT940 customer statement -> canonical `BankLine`. Stdlib only.

**Schema provenance: VERIFIED-BY-STANDARD.** This is the first format in this
package whose schema is a genuinely published standard rather than an export
layout reconstructed from knowledge. MT940 is the SWIFT FIN *Customer
Statement Message*, and its tags mean what the SWIFT user handbook says they
mean, in every bank in every country:

===========  ==============================================================
`:20:`       Transaction Reference Number -- the sender's reference for this
             statement message. 16x.
`:25:`       Account Identification -- the account the statement is for. 35x.
`:28C:`      Statement Number / Sequence Number, `5n[/5n]`. `:28:` is the
             older spelling of the same field and is accepted.
`:60F:`      Opening Balance, *first* -- the booked balance this statement
             starts from. `:60M:` is the intermediate form, used when one
             statement is split across several messages; both are read here
             as "the balance this page opens on".
`:61:`       Statement Line -- one booked entry. Subfields, in order: value
             date `6!n`, optional entry date `4!n`, debit/credit mark `2a`,
             optional funds code `1!a`, amount `15d`, transaction type
             identification code `1!a3!c`, customer reference `16x`,
             optionally `//` and the bank's own reference `16x`, and
             optionally supplementary details on a following line, `34x`.
`:62F:`      Closing Balance (booked), `:62M:` its intermediate form.
`:64:`       Closing *available* balance -- read past, deliberately. It is not
             a booked balance and it does not take part in the chain.
`:86:`       Information to Account Owner -- the narration, up to 6 lines of
             65 characters. Real files wrap, so continuation lines are part of
             the field and are joined here.
===========  ==============================================================

Two details of the standard are traps worth naming rather than discovering:

* **The debit/credit mark is up to two characters wide and the funds code is
  optional**, so `CR71153,04` is *not* "CR" meaning credit -- it is mark `C`
  followed by funds code `R` (the third letter of `INR`) followed by the
  amount. A parser that reads the mark as a fixed two characters gets
  `D450,00` wrong, and one that reads it as a fixed one character glues the
  funds code onto the amount.
* **`RC` and `RD` are reversals and invert the sign.** `RC` reverses a credit,
  so it takes money *out*; `RD` reverses a debit, so it puts money *in*. A
  reversal read as a plain credit doubles the error it was posted to correct.

**Amounts use a decimal comma, never a decimal point, and carry no grouping
separators.** `342614,53` is three hundred and forty-two thousand rupees. That
is why this module does not hand raw text to `parse_paise` -- `parse_paise`
reads `,` as Indian digit grouping, exactly as an HDFC CSV means it, and would
turn `342614,53` into 34261453 rupees. The comma is converted here, under a
regex that admits at most one of them, and sub-paise precision is refused the
same way it is everywhere else in this package.

**The blast radius of a bad line is the STATEMENT, not the line.** This is the
one place where this adapter deliberately departs from "a broken row costs
nothing but itself", and the format forces it. MT940 does not print a running
balance on each line; it prints an opening balance, the movements, and a
closing balance. The per-line `BankLine.balance` this adapter emits is
therefore *derived* -- opening plus everything before it. So one unreadable
`:61:` makes every balance after it a guess, and a chain that does not close
means an unknown number of lines are missing. Either way the honest output is:
the defective lines quarantined individually with their own reasons, plus one
statement-level quarantine naming the consequence, and **no `BankLine` at all
from that statement**. Lines that did parse in a failed statement are counted
in `AdapterResult.skipped_rows`, so the arithmetic still closes and nothing is
silently dropped.

Statements are scoped independently, which is why a file of several statements
loses only the ones that are broken. A file holding one statement is the common
case, and there a failed chain is a file-level quarantine -- which is what it
should be.

**UTR extraction is as narrow as HDFC's.** `BankLine.utr` is nullable and the
canonicaliser downstream does the real narration mining. Here a UTR is the bank
reference after `//` in `:61:`, or a value behind an explicit `UTR` marker in
the `:86:` narration, and nothing else. The customer reference is deliberately
NOT used: it is the *sender's* reference, which is frequently `NONREF` and is
never the bank's own settlement identifier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError

from core.adapters.base import (
    AdapterResult,
    CanonicalRecord,
    QuarantinedRow,
    QuarantineReason,
    UndecodableFileError,
    decode_bytes,
    parse_paise,
    read_text,
    row_fingerprint,
    sha256_bytes,
    strip_comment_lines,
)
from core.models import BankLine

#: A tag line: `:` tag `:` content. The tag is two digits with an optional
#: single-letter option (`60F`, `28C`), which is the whole of the MT940 tag
#: grammar this adapter needs to recognise.
TAG_RE = re.compile(r"^:(?P<tag>\d{2}[A-Z]?):(?P<content>.*)$")

#: Field 61, subfield by subfield. See the module docstring for why the mark
#: and the funds code cannot be read as fixed widths.
STATEMENT_LINE_RE = re.compile(
    r"^(?P<value_date>\d{6})"
    r"(?P<entry_date>\d{4})?"
    r"(?P<mark>RC|RD|C|D)"
    r"(?P<funds_code>[A-Za-z])?"
    r"(?P<amount>\d[\d,]*)"
    r"(?P<txn_type>[A-Z][A-Za-z0-9]{3})"
    r"(?P<reference>.*)$"
)

#: Fields 60a/62a: mark, YYMMDD, ISO currency, amount.
BALANCE_RE = re.compile(
    r"^(?P<mark>[CD])(?P<date>\d{6})(?P<currency>[A-Za-z]{3})(?P<amount>\d[\d,]*)$"
)

#: An MT940 amount: digits, at most one decimal comma, at most two decimals.
#: A decimal POINT is refused outright rather than tolerated -- a file writing
#: `342614.53` is not MT940, and guessing which convention it meant is exactly
#: the class of guess this package does not make.
AMOUNT_RE = re.compile(r"^(?P<whole>\d{1,15})(?:,(?P<fraction>\d*))?$")

#: `C` credits, `D` debits, and the two reversal marks that invert them.
CREDIT_MARKS = frozenset({"C", "RD"})
DEBIT_MARKS = frozenset({"D", "RC"})

OPENING_TAGS = ("60F", "60M")
CLOSING_TAGS = ("62F", "62M")

#: Below this length a `:61:` is not malformed, it is cut short -- an
#: interrupted transfer rather than a file this adapter cannot read. The two
#: get different reasons because they have different fixes.
MIN_STATEMENT_LINE_LENGTH = 12

_REFERENCE_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9]{6,22}$")
_UTR_MARKER_RE = re.compile(r"\bUTR[:\s-]*([A-Za-z0-9]{6,22})\b", re.IGNORECASE)


class Mt940LineError(Exception):
    """One `:61:` cannot become a `BankLine`, and here is exactly why."""

    def __init__(self, reason: QuarantineReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def parse_mt940_amount(raw: str) -> int:
    """MT940 decimal-comma amount -> integer paise. Never `float`.

    `342614,53` -> 34261453. `1000,` -> 100000. `1234,567` raises, because
    sub-paise has no integer answer and this package does not invent one.
    """
    text = (raw or "").strip()
    matched = AMOUNT_RE.match(text)
    if not matched:
        raise ValueError(
            f"{raw!r} is not an MT940 amount: expected digits with at most one "
            f"decimal comma (MT940 writes `342614,53`, never `342614.53` and "
            f"never grouping separators)"
        )
    fraction = matched.group("fraction") or ""
    if len(fraction) > 2:
        raise ValueError(
            f"{raw!r} carries {len(fraction)} decimal places, which is sub-paise; "
            f"sub-paise values are quarantined, never rounded"
        )
    whole = matched.group("whole")
    return parse_paise(f"{whole}.{fraction.ljust(2, '0')}")


def extract_utr(bank_reference: str | None, narration: str) -> str | None:
    """The bank's own reference, or an explicitly marked UTR. Never a guess."""
    if bank_reference and _REFERENCE_RE.match(bank_reference):
        return bank_reference
    marked = _UTR_MARKER_RE.search(narration or "")
    return marked.group(1) if marked else None


@dataclass
class TagBlock:
    """One MT940 field: its tag, its content with continuations joined, and the
    physical line it starts on."""

    tag: str
    content: str
    raw: str
    row_number: int


@dataclass
class Statement:
    """One `:20:`-delimited statement, as read off the file before any mapping."""

    reference: str | None = None
    opening: TagBlock | None = None
    closing: TagBlock | None = None
    #: `(statement line, narration block or None)` in file order.
    lines: list[tuple[TagBlock, TagBlock | None]] = field(default_factory=list)
    #: The line `:20:` was on, for the statement-level quarantine record.
    row_number: int = 1
    raw: str = ""


def _blocks(body: str, start_line: int) -> list[TagBlock]:
    """Split the message into tag blocks, joining continuation lines.

    A line that does not start with a tag belongs to the field above it: that
    is how `:86:` carries six lines of narration and how `:61:` carries its
    supplementary details. Joining with a single space rather than
    concatenating is deliberate -- MT940 lines are lines, not a wrapped
    paragraph, and gluing them would run the last word of one into the first
    word of the next.
    """
    blocks: list[TagBlock] = []
    for offset, line in enumerate(body.splitlines()):
        row_number = start_line + offset
        matched = TAG_RE.match(line)
        if matched:
            blocks.append(
                TagBlock(
                    tag=matched.group("tag"),
                    content=matched.group("content").strip(),
                    raw=line,
                    row_number=row_number,
                )
            )
            continue
        if not line.strip():
            continue
        if blocks:
            blocks[-1].content = f"{blocks[-1].content} {line.strip()}".strip()
            blocks[-1].raw = f"{blocks[-1].raw}\n{line}"
    return blocks


def _statements(blocks: list[TagBlock]) -> list[Statement]:
    statements: list[Statement] = []
    current: Statement | None = None
    for block in blocks:
        if block.tag == "20":
            current = Statement(
                reference=block.content, row_number=block.row_number, raw=block.raw
            )
            statements.append(current)
            continue
        if current is None:
            # Content before any `:20:`. A statement with no reference is still
            # a statement -- refusing to read it would drop real lines because
            # a header was missing.
            current = Statement(row_number=block.row_number, raw=block.raw)
            statements.append(current)
        if block.tag in OPENING_TAGS:
            current.opening = block
        elif block.tag in CLOSING_TAGS:
            current.closing = block
        elif block.tag == "61":
            current.lines.append((block, None))
        elif block.tag == "86" and current.lines and current.lines[-1][1] is None:
            statement_line, _ = current.lines[-1]
            current.lines[-1] = (statement_line, block)
    return statements


@dataclass(frozen=True)
class _Movement:
    """One parsed `:61:`, before the running balance is known."""

    row_number: int
    raw: str
    value_date: date
    narration: str
    signed_paise: int
    utr: str | None


class MT940Adapter:
    """The SWIFT statement format: one adapter, for the standard itself.

    There is one adapter here rather than one per bank because -- unlike the
    CSV exports of phase 1 -- MT940 is a published standard, and every bank's
    file carries the same tags in the same order with the same meanings. What
    varies between banks is what they *write into* `:86:`, which is narration,
    which is the canonicaliser's job downstream and not a layout difference.
    """

    format_id = "mt940-v1"
    format_version = "1.0"

    def sniff(self, head: bytes) -> float:
        """Tag shape, never a filename or an extension.

        `:20:` and an opening balance are what make a file an MT940 statement;
        the rest raise confidence. A CSV scores zero here for the same reason
        this scores zero on a CSV -- neither carries the other's shape.
        """
        try:
            text, _ = decode_bytes(head)
        except UndecodableFileError:
            return 0.0
        body, _ = strip_comment_lines(text)
        tags = {
            matched.group("tag")
            for matched in (TAG_RE.match(line) for line in body.splitlines())
            if matched
        }
        if "20" not in tags or not any(tag in tags for tag in OPENING_TAGS):
            return 0.0
        distinctive = (
            "25" in tags,
            "28C" in tags or "28" in tags,
            "61" in tags,
            any(tag in tags for tag in CLOSING_TAGS),
        )
        return 0.7 + 0.3 * (sum(distinctive) / len(distinctive))

    def parse(self, path: Path | str) -> AdapterResult:
        text, encoding, raw_bytes = read_text(path)
        body, comment_lines = strip_comment_lines(text)

        records: list[CanonicalRecord] = []
        quarantined: list[QuarantinedRow] = []
        row_hashes: list[str] = []
        skipped = 0

        blocks = _blocks(body, start_line=comment_lines + 1)
        statements = _statements(blocks)
        if not statements:
            quarantined.append(
                QuarantinedRow(
                    row_number=comment_lines + 1,
                    raw="",
                    reason=QuarantineReason.MISSING_HEADER_COLUMN,
                    detail=(
                        "the file carries no MT940 tag at all; an MT940 statement "
                        "opens with :20: and a :60F: opening balance"
                    ),
                )
            )
        for statement in statements:
            produced, statement_quarantine, statement_skipped = self._statement(statement)
            quarantined.extend(statement_quarantine)
            skipped += statement_skipped
            for record in produced:
                records.append(record)
                row_hashes.append(row_fingerprint(self.format_id, record))

        return AdapterResult(
            format_id=self.format_id,
            format_version=self.format_version,
            records=records,
            quarantined=quarantined,
            file_sha256=sha256_bytes(raw_bytes),
            row_hashes=row_hashes,
            encoding=encoding,
            skipped_rows=skipped,
        )

    # --- one statement ------------------------------------------------------

    def _statement(
        self, statement: Statement
    ) -> tuple[list[CanonicalRecord], list[QuarantinedRow], int]:
        quarantined: list[QuarantinedRow] = []

        if statement.opening is None or statement.closing is None:
            missing = [
                name
                for name, block in (
                    (":60F:", statement.opening),
                    (":62F:", statement.closing),
                )
                if block is None
            ]
            quarantined.append(
                QuarantinedRow(
                    row_number=statement.row_number,
                    raw=statement.raw,
                    reason=QuarantineReason.MISSING_VALUE,
                    detail=(
                        f"statement {statement.reference!r} has no {missing} balance "
                        f"field, so its per-line balances cannot be derived and its "
                        f"chain cannot be checked"
                    ),
                )
            )
            return [], quarantined, len(statement.lines)

        try:
            opening = self._balance(statement.opening)
            closing = self._balance(statement.closing)
        except Mt940LineError as error:
            quarantined.append(
                QuarantinedRow(
                    row_number=statement.opening.row_number,
                    raw=statement.opening.raw,
                    reason=error.reason,
                    detail=error.detail,
                )
            )
            return [], quarantined, len(statement.lines)

        movements: list[_Movement] = []
        seen: dict[str, int] = {}
        failed = 0
        for line_block, narration_block in statement.lines:
            key = (
                line_block.raw
                if narration_block is None
                else f"{line_block.raw}\n{narration_block.raw}"
            )
            if key in seen:
                failed += 1
                quarantined.append(
                    QuarantinedRow(
                        row_number=line_block.row_number,
                        raw=key,
                        reason=QuarantineReason.DUPLICATE_ROW,
                        detail=(
                            "identical to the statement line already read at line "
                            f"{seen[key]}"
                        ),
                    )
                )
                continue
            seen[key] = line_block.row_number
            try:
                movements.append(self._movement(line_block, narration_block))
            except Mt940LineError as error:
                failed += 1
                quarantined.append(
                    QuarantinedRow(
                        row_number=line_block.row_number,
                        raw=key,
                        reason=error.reason,
                        detail=error.detail,
                    )
                )

        if failed:
            quarantined.append(
                QuarantinedRow(
                    row_number=statement.row_number,
                    raw=statement.raw,
                    reason=QuarantineReason.ARITHMETIC_MISMATCH,
                    detail=(
                        f"statement {statement.reference!r}: {failed} of "
                        f"{len(statement.lines)} statement line(s) could not be read, "
                        f"so the :60F: -> :62F: balance chain cannot be verified and "
                        f"every derived per-line balance after the first bad line "
                        f"would be a guess; the whole statement is quarantined and "
                        f"its {len(movements)} readable line(s) are not emitted"
                    ),
                )
            )
            return [], quarantined, len(movements)

        computed = opening + sum(movement.signed_paise for movement in movements)
        if computed != closing:
            quarantined.append(
                QuarantinedRow(
                    row_number=statement.closing.row_number,
                    raw=statement.closing.raw,
                    reason=QuarantineReason.ARITHMETIC_MISMATCH,
                    detail=(
                        f"statement {statement.reference!r} does not close: opening "
                        f"balance {opening} paise plus {len(movements)} movement(s) "
                        f"totalling {computed - opening} paise gives {computed} paise, "
                        f"but the file declares a closing balance of {closing} paise "
                        f"-- a difference of {closing - computed} paise. An MT940 "
                        f"statement whose chain does not close is missing lines, so "
                        f"none of its lines are emitted"
                    ),
                )
            )
            return [], quarantined, len(movements)

        records: list[CanonicalRecord] = []
        running = opening
        for movement in movements:
            running += movement.signed_paise
            try:
                records.append(
                    BankLine(
                        line_id=f"MT940-{movement.row_number:05d}",
                        txn_date=movement.value_date,
                        narration=movement.narration,
                        credit=movement.signed_paise if movement.signed_paise > 0 else None,
                        debit=-movement.signed_paise if movement.signed_paise < 0 else None,
                        balance=running,
                        utr=movement.utr,
                    )
                )
            except ValidationError as error:  # pragma: no cover - defence in depth
                quarantined.append(
                    QuarantinedRow(
                        row_number=movement.row_number,
                        raw=movement.raw,
                        reason=QuarantineReason.SCHEMA_VIOLATION,
                        detail=(
                            "canonical model rejected the line: "
                            f"{error.error_count()} error(s)"
                        ),
                    )
                )
        return records, quarantined, 0

    def _balance(self, block: TagBlock) -> int:
        """`:60F:`/`:62F:` -> signed paise. A `D` mark is an overdrawn account."""
        matched = BALANCE_RE.match(block.content)
        if not matched:
            raise Mt940LineError(
                QuarantineReason.UNKNOWN_VALUE,
                f":{block.tag}: content {block.content!r} is not a balance field; "
                f"expected a C/D mark, YYMMDD, an ISO currency and an amount",
            )
        currency = matched.group("currency").upper()
        if currency != "INR":
            raise Mt940LineError(
                QuarantineReason.UNKNOWN_VALUE,
                f":{block.tag}: currency {currency!r}: the canonical schema is INR-only",
            )
        try:
            datetime.strptime(matched.group("date"), "%y%m%d")
        except ValueError as error:
            raise Mt940LineError(
                QuarantineReason.BAD_DATE,
                f":{block.tag}: date {matched.group('date')!r} is not a YYMMDD date",
            ) from error
        try:
            amount = parse_mt940_amount(matched.group("amount"))
        except ValueError as error:
            raise Mt940LineError(
                QuarantineReason.BAD_DECIMAL, f":{block.tag}: {error}"
            ) from error
        return amount if matched.group("mark") == "C" else -amount

    def _movement(self, line: TagBlock, narration_block: TagBlock | None) -> _Movement:
        content = line.content
        if len(content) < MIN_STATEMENT_LINE_LENGTH:
            raise Mt940LineError(
                QuarantineReason.TRUNCATED_ROW,
                f":61: content {content!r} is {len(content)} character(s); a "
                f"statement line cannot be shorter than "
                f"{MIN_STATEMENT_LINE_LENGTH} and this one is cut short",
            )
        matched = STATEMENT_LINE_RE.match(content)
        if not matched:
            raise Mt940LineError(
                QuarantineReason.UNKNOWN_VALUE,
                f":61: content {content!r} does not match the field 61 grammar "
                f"(value date, optional entry date, C/D/RC/RD mark, optional funds "
                f"code, amount, 4-character transaction type, reference)",
            )

        try:
            value_date = datetime.strptime(matched.group("value_date"), "%y%m%d").date()
        except ValueError as error:
            raise Mt940LineError(
                QuarantineReason.BAD_DATE,
                f":61: value date {matched.group('value_date')!r} is not a YYMMDD date",
            ) from error

        try:
            amount = parse_mt940_amount(matched.group("amount"))
        except ValueError as error:
            raise Mt940LineError(QuarantineReason.BAD_DECIMAL, f":61: {error}") from error

        if amount == 0:
            raise Mt940LineError(
                QuarantineReason.AMBIGUOUS_DIRECTION,
                ":61: amount is zero, so the line moves no money",
            )

        mark = matched.group("mark")
        signed = amount if mark in CREDIT_MARKS else -amount

        reference = matched.group("reference")
        customer_reference, _, bank_reference = reference.partition("//")

        narration = (narration_block.content if narration_block else "").strip()
        if not narration:
            # A `:61:` with no `:86:` sometimes carries supplementary details on
            # its own continuation line; `_blocks` has already folded those into
            # `content`, so what is left to describe the line is its reference.
            narration = customer_reference.strip()
        if not narration:
            raise Mt940LineError(
                QuarantineReason.MISSING_VALUE,
                ":61: has neither a following :86: narration nor a customer "
                "reference, so the line has no description at all",
            )

        return _Movement(
            row_number=line.row_number,
            raw=line.raw,
            value_date=value_date,
            narration=narration,
            signed_paise=signed,
            utr=extract_utr(bank_reference.strip() or None, narration),
        )
