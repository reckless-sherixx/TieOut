"""Slice (small finance bank) PDF account statement -> canonical `BankLine`.

**Schema provenance, and why this file is different from every other adapter
in this directory.** Every other layout here was written from a published
schema, from corroborated descriptions, or from knowledge. This one was read
off a *genuine statement PDF a real person downloaded from their own account*
-- 29 pages of it. `VALIDATION.md` §4 carries the only
VERIFIED-AGAINST-GENUINE-ARTEFACT row in the repository because of this file.

The artefact itself is not here and never will be. It is a personal bank
statement: it lives in `incoming/`, which `.gitignore` covers, and nothing
derived from its content -- no counterparty, no VPA, no amount, no reference --
appears in this module, in a fixture, in a test or in a report. What was taken
from it is *structure*: the shapes below, and the aggregate counts recorded in
`VALIDATION.md`. The committed fixtures are hand-written synthetic text in the
same layout with invented names, VPAs and amounts.

---

**Two stages, and the split is load-bearing.** `extract_pages` turns a PDF into
one string per page and is the only part that knows pypdf exists.
`parse_text` is a pure function from text to records and knows nothing about
PDFs at all. Everything worth testing is in the second stage, so the committed
fixtures are `.txt` files of *extracted text* -- no PDF-writing dependency is
needed anywhere in the suite, and the parser is exercised without a PDF.

**The layout.** A transaction occupies one visual row of five columns -- date,
narration, reference, amount, balance -- and the text layer emits it as one to
four lines:

    DD Mon 'YY <narration, first slice>
    <narration, continued>
    <reference> <signed amount> <balance>

A short row collapses onto a single line. The narration wraps at a fixed
character width, which means **it wraps mid-token, including mid-VPA and
mid-word**. That is why a continuation line is joined with NO separator: the
tail of a VPA and the head of the next line are two halves of one string, and
a space between them would corrupt every wrapped narration in the file. The
line carrying the reference and the money is a different thing -- it is the
row's remaining *columns*, not a wrap -- so it is recognised by shape and
joined with a space. `_is_tail_line` is that distinction, and it is the single
most consequential line in this module.

**Row delimitation is a two-sided rule.** A row STARTS at a date at line start
and ENDS when the accumulated text ends in the money pair. Both halves are
needed, and the reason is a trap the real file contains on every one of its 29
pages: the page header's first line is `DD Mon 'YY - DD Mon 'YY`, the statement
period, which starts with the row-start pattern and is not a row. It is matched
and skipped before the state machine sees it.

**A date inside a narration is corrected, not quarantined.** A narration can
wrap such that a continuation line begins with something that reads as a date.
The rule that handles it: a date at line start begins a new row **only when no
row is pending**. Because a row is terminated eagerly by its money pair, a
pending row is by construction an incomplete one, so the date is a wrap and is
joined as a continuation. This is a deliberate correct-rather-than-quarantine
choice, and the reverse rule would be worse in a way that hides: it would
quarantine the real row AND emit a fragment carrying the real row's amount and
balance, so the balance chain would still close and nothing would look wrong.

`MAX_ROW_LINES` bounds what that rule can cost. A row whose money pair is
missing entirely would otherwise swallow every line after it; instead it is
quarantined after six lines and the next date starts a fresh row. Real rows run
to four.

**The balance chain is the proof the reconstruction is right.** Every row's
balance must equal the previous row's balance plus a credit or minus a debit.
A mis-joined row almost always breaks it, so a file that closes end to end is a
file whose wrapped lines were put back together correctly. A break is a
row-level `ARITHMETIC_MISMATCH` naming both figures; it is never a reason to
drop a row or to stop.

**Trailing zeros are trimmed by this bank, and the chain is what proved it.**
Balances arrive as `1,234.5` and `1,234` as well as `1,234.56`. That looked
like a truncated text layer until the chain closed exactly -- across 531
transitions -- with those values taken at face value. They are real; slice
simply does not pad. `parse_paise` already accepts them.

**UTR extraction is deliberately narrow, on the HDFC precedent.** The trailing
reference is 12 to 17 digits depending on which of the two narration
generations a row belongs to, and a UPI UTR is exactly 12. So a reference is
lifted into `BankLine.utr` only when it is exactly 12 digits, and otherwise it
stays in the narration where the downstream canonicaliser can mine it. On the
genuine artefact that populates `utr` on very few rows, and that is the correct
outcome: a wider rule would hand the matcher a 17-digit internal transaction id
in a field that means "UTR", which is worse than a null.

**Two narration generations, one adapter.** The statement changes format
part-way through its own date range: an older `UPI Debit-<party>-<vpa>-<ref>`
and a newer `UPI-Debit-<ref>-<party>-<ifsc>-<vpa>`. They are not two layouts,
because nothing in this adapter's mapping depends on the difference -- the
narration is carried verbatim either way and the columns are identical. The
type vocabulary is discovered, not enumerated, for the same reason.

**Everything that is not a transaction row is furniture, and it is counted.**
The cover block on page 1, the two-line header on every page and the footer on
the last are `skipped_rows`, not quarantine: they are the document, not damaged
rows. The one exception is a line that carries a money pair but has no date --
that is unmistakably a transaction row that lost its date, and it is
quarantined.

**Line identity is positional**, per the HDFC precedent: a statement line has
no natural key, and the reference is not distinct on every row. `SLICE-<line>`
where the line is the physical line the row starts on, counting the whole
document with page separators included.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from core.adapters.base import (
    AdapterResult,
    QuarantinedRow,
    QuarantineReason,
    parse_paise,
    parse_date_exact,
    row_fingerprint,
    sha256_bytes,
    strip_comment_lines,
)
from core.models import BankLine

#: What `extract_pages` puts between two pages, and what `parse_text` splits on.
#: A form feed is what a page break has meant in a text stream since line
#: printers, and it cannot occur inside a statement's own text.
PAGE_SEPARATOR = "\f"

#: The bank's own running header, present on every page of the genuine
#: artefact. This is boilerplate the bank prints, not anything belonging to the
#: account holder, which is why it can be named here. It is matched
#: case-insensitively because it is set in lower case in the artefact and a
#: template change to title case would not make the file a different format.
SLICE_ANCHOR = "slice small finance bank"

#: `01 Apr '25`. Slice writes an abbreviated month and a two-digit year with a
#: leading apostrophe. Only this layout is accepted, for the reason
#: `parse_date_exact` exists: a permissive parser cannot be corrected later.
DATE_FORMATS = ("%d %b '%y",)

#: A date at the start of a line: what begins a row, when no row is pending.
_DATE_START_RE = re.compile(r"^(\d{1,2} [A-Z][a-z]{2} '\d{2})\b")

#: The page header's first line -- the statement period. It starts with the
#: row-start pattern and is NOT a row. Matched first, on every page.
_PERIOD_RE = re.compile(r"^\d{1,2} [A-Z][a-z]{2} '\d{2}\s*-\s*\d{1,2} [A-Z][a-z]{2} '\d{2}\s*$")

#: The page header's second line: `3/29`.
_PAGE_NUMBER_RE = re.compile(r"^\d{1,3}\s*/\s*\d{1,3}\s*$")

#: The money pair that ends a row, with the reference that precedes it when the
#: row has one. The amounts are captured as non-space runs rather than as
#: validated numbers on purpose: termination is a question about SHAPE, and a
#: row whose amount is malformed must still terminate so it can be quarantined
#: as one bad row. A stricter pattern here would make a bad decimal swallow
#: every line after it.
_TAIL_RE = re.compile(
    r"(?:(?<=\s)|^)(?:(\d{6,})\s+)?(-?)₹(\S+)\s+₹(\S+)\s*$"
)

#: A line that is nothing but a row's remaining columns. Joined with a SPACE;
#: every other continuation is a mid-token wrap and is joined with nothing.
_TAIL_LINE_RE = re.compile(r"^\s*(?:\d{6,}\s+)?-?₹\S+\s+₹\S+\s*$")

#: A UPI UTR is exactly twelve digits. Anything else in the reference column is
#: an internal transaction id and stays in the narration. See the docstring.
_UTR_RE = re.compile(r"^\d{12}$")

#: How many lines one row may occupy before it is declared unterminated. Real
#: rows run to four; six leaves headroom without letting a row with no money
#: pair consume the rest of the page.
MAX_ROW_LINES = 6


@dataclass(frozen=True)
class TextParse:
    """What `parse_text` produced: the pure stage's whole result.

    Separate from `AdapterResult` because it carries no file-level facts -- no
    bytes, no hash, no encoding. Those belong to the stage that opened a file,
    and keeping them out of here is what lets every parser test be a string in
    and records out.
    """

    records: list[BankLine]
    quarantined: list[QuarantinedRow]
    #: Page headers, the cover block and the footer: document furniture the
    #: layout deliberately ignores, counted so that
    #: `lines seen == records + quarantined + skipped` closes.
    skipped_rows: int


def extract_pages(path: Path | str) -> list[str]:
    """A PDF's text layer, one string per page, in page order.

    The only function in this module that knows pypdf exists, and it imports it
    lazily: `core/` is imported by the CLI, the API and every test, and a
    top-level PDF dependency would be paid for by all of them to serve one
    adapter.

    Raises whatever pypdf raises. `SlicePDFStatementAdapter.parse` is where that
    becomes a file-level quarantine rather than a traceback, because that is the
    layer that owes the caller a result.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return [page.extract_text() or "" for page in reader.pages]


def _is_tail_line(line: str) -> bool:
    return bool(_TAIL_LINE_RE.match(line))


def extract_utr(reference: str | None) -> str | None:
    """A reference is a UTR only when it is exactly twelve digits. Never guess."""
    if reference and _UTR_RE.match(reference):
        return reference
    return None


def parse_text(text: str) -> TextParse:
    """Extracted statement text -> `BankLine`s, quarantine and a skip count.

    Pure: same string in, same records out, no filesystem and no clock. Pages
    are separated by `PAGE_SEPARATOR` and are processed in order; a row never
    spans a page boundary, so a row still pending when a page ends is
    unterminated and is quarantined there.

    Leading `#` provenance comments are stripped exactly as they are for the CSV
    layouts, so a hand-written fixture and a real extraction parse identically.
    """
    body, dropped = strip_comment_lines(text)

    records: list[BankLine] = []
    quarantined: list[QuarantinedRow] = []
    skipped = 0

    line_number = dropped
    for page in body.split(PAGE_SEPARATOR):
        pending: _Pending | None = None
        for raw in page.split("\n"):
            line_number += 1
            line = raw.rstrip("\r").rstrip()
            if not line.strip():
                continue

            if _PERIOD_RE.match(line) or _PAGE_NUMBER_RE.match(line):
                # The page header. The period line starts with a date and is the
                # trap this check exists for.
                skipped += 1
                continue

            if pending is None:
                if _DATE_START_RE.match(line):
                    pending = _Pending(line_number, [line])
                elif _is_tail_line(line):
                    # Unmistakably a transaction row that lost its date: money
                    # with nothing to attach it to. The one orphan worth
                    # quarantining.
                    quarantined.append(
                        QuarantinedRow(
                            row_number=line_number,
                            raw=line,
                            reason=QuarantineReason.TRUNCATED_ROW,
                            detail=(
                                "a line carrying a reference and a money pair with no "
                                "date to start its row: a transaction line whose "
                                "beginning is missing"
                            ),
                        )
                    )
                    continue
                else:
                    # Cover block, footer, marketing line: the document, not a
                    # damaged row. Counted, never dropped silently.
                    skipped += 1
                    continue
            else:
                pending.lines.append(line)

            joined = pending.joined()
            match = _TAIL_RE.search(joined)
            if match is not None:
                outcome = _row(pending, joined, match)
                if isinstance(outcome, BankLine):
                    records.append(outcome)
                else:
                    quarantined.append(outcome)
                pending = None
            elif len(pending.lines) >= MAX_ROW_LINES:
                quarantined.append(_unterminated(pending))
                pending = None

        if pending is not None:
            quarantined.append(_unterminated(pending))

    return TextParse(records=records, quarantined=quarantined, skipped_rows=skipped)


@dataclass
class _Pending:
    """The lines of one row so far, and where it started."""

    line_number: int
    lines: list[str]

    def joined(self) -> str:
        """The row as one string, with the wrap rule applied.

        A continuation is glued on with nothing because the narration wraps
        mid-token; a line that is only the row's remaining columns gets a space,
        because it is a column boundary and not a wrap.
        """
        text = self.lines[0]
        for line in self.lines[1:]:
            text = f"{text} {line}" if _is_tail_line(line) else text + line
        return text

    @property
    def raw(self) -> str:
        return "\n".join(self.lines)


def _unterminated(pending: _Pending) -> QuarantinedRow:
    return QuarantinedRow(
        row_number=pending.line_number,
        raw=pending.raw,
        reason=QuarantineReason.TRUNCATED_ROW,
        detail=(
            f"a row starting with a date but never reaching an amount and a "
            f"balance within {MAX_ROW_LINES} lines or before the page ended"
        ),
    )


def _row(pending: _Pending, joined: str, match: re.Match[str]) -> BankLine | QuarantinedRow:
    """One reconstructed row -> a `BankLine`, or the quarantine that explains it."""
    reference, sign, amount_text, balance_text = match.groups()

    def bad(reason: QuarantineReason, detail: str) -> QuarantinedRow:
        return QuarantinedRow(
            row_number=pending.line_number,
            raw=pending.raw,
            reason=reason,
            detail=detail,
        )

    date_match = _DATE_START_RE.match(joined)
    if date_match is None:  # pragma: no cover - a row only starts on a date
        return bad(QuarantineReason.BAD_DATE, "the row does not begin with a date")
    try:
        txn_date = parse_date_exact(date_match.group(1), DATE_FORMATS)
    except ValueError as error:
        return bad(QuarantineReason.BAD_DATE, str(error))

    try:
        amount = parse_paise(amount_text)
    except ValueError as error:
        return bad(QuarantineReason.BAD_DECIMAL, f"amount: {error}")
    try:
        balance = parse_paise(balance_text)
    except ValueError as error:
        return bad(QuarantineReason.BAD_DECIMAL, f"balance: {error}")

    if amount == 0:
        return bad(
            QuarantineReason.AMBIGUOUS_DIRECTION,
            "the row's amount is zero, so the line moves no money and has no "
            "direction to read",
        )

    # Everything between the date and the money pair is the narration, carried
    # verbatim and unwrapped. The reference stays in it unless it was lifted
    # into `utr`, because dropping a token a bank printed is not this layer's
    # decision to make.
    narration = joined[date_match.end() : match.start()].strip()
    utr = extract_utr(reference)
    if reference and utr is None and reference not in narration:
        narration = f"{narration} {reference}".strip()

    if not narration:
        return bad(
            QuarantineReason.MISSING_VALUE,
            "the row carries a date and an amount but no narration at all",
        )

    return BankLine(
        line_id=f"SLICE-{pending.line_number:05d}",
        txn_date=txn_date,
        narration=narration,
        credit=None if sign == "-" else amount,
        debit=amount if sign == "-" else None,
        balance=balance,
        utr=utr,
    )


def validate_balance_chain(parse: TextParse) -> TextParse:
    """Re-check every row against its predecessor's balance, in place of trust.

    A statement's running balance is the one piece of redundancy it carries, and
    it is what proves the wrapped-line reconstruction was right: a mis-joined
    row takes an amount or a balance from the wrong place and the chain stops
    closing. So a break is not a warning, it is a row-level
    `ARITHMETIC_MISMATCH` naming both figures, and the row does not become a
    record.

    The first row is unconstrained: it has no predecessor in the statement, and
    the opening balance lives in the cover block this layout treats as
    furniture. Every transition after it is checked.

    The chain continues from the balance the *file* claimed, not from the one
    the chain expected. A single wrong row otherwise breaks every row after it,
    turning one defect into a page of them and burying the line a human has to
    look at.
    """
    kept: list[BankLine] = []
    broken: list[QuarantinedRow] = []
    previous: int | None = None
    for record in parse.records:
        moved = (record.credit or 0) - (record.debit or 0)
        if previous is not None and previous + moved != record.balance:
            broken.append(
                QuarantinedRow(
                    row_number=int(record.line_id.rsplit("-", 1)[1]),
                    raw=record.narration,
                    reason=QuarantineReason.ARITHMETIC_MISMATCH,
                    detail=(
                        f"balance chain broken: previous balance {previous} paise "
                        f"{'+' if moved >= 0 else '-'} {abs(moved)} paise is "
                        f"{previous + moved} paise, but the row claims "
                        f"{record.balance} paise"
                    ),
                )
            )
        else:
            kept.append(record)
        previous = record.balance
    if not broken:
        return parse
    return TextParse(
        records=kept,
        quarantined=[*parse.quarantined, *broken],
        skipped_rows=parse.skipped_rows,
    )


class SlicePDFStatementAdapter:
    """One `BankLine` per transaction row of a Slice statement PDF."""

    format_id = "slice-pdf-v1"
    format_version = "1.0"

    #: This adapter reads a binary container, so the registry must be willing to
    #: show it bytes that are not text. Every other adapter here reads a text
    #: export and must NOT be shown them -- see `registry.sniff_scores`.
    reads_binary = True

    def sniff(self, head: bytes) -> float:
        """Confidence from the magic bytes, and from the anchor when it is
        reachable.

        **The honest limitation, stated here because it decides what `parse`
        has to do.** `sniff` is handed the first `SNIFF_BYTES` of the file and
        nothing else. In a real Slice statement the text layer is inside a
        compressed content stream, so `SLICE_ANCHOR` does not appear in those
        bytes -- or anywhere in the raw file. The magic number is therefore the
        only evidence available at sniff time, and it says "a PDF", not "a
        Slice PDF".

        So the anchor is checked here for the case where it IS reachable (a PDF
        with uncompressed streams), and `parse` checks it again on the extracted
        page-1 text, where it is always reachable. A PDF that is not a Slice
        statement gets a file-level `UNRECOGNISED_FORMAT` quarantine naming the
        anchor it lacked -- refused, never parsed on a guess.
        """
        if not head.startswith(b"%PDF-"):
            return 0.0
        if SLICE_ANCHOR.encode("utf-8") in head.lower():
            return 1.0
        return 0.70

    def parse(self, path: Path | str) -> AdapterResult:
        """Extract, identify, parse, then check the chain. Never raises."""
        path = Path(path)
        try:
            raw = path.read_bytes()
        except OSError as error:
            return self._file_level(
                "", QuarantineReason.UNDECODABLE_FILE, f"the file could not be read: {error}"
            )
        digest = sha256_bytes(raw)

        try:
            pages = extract_pages(path)
        except Exception as error:  # noqa: BLE001 - a result is owed, not a traceback
            return self._file_level(
                digest,
                QuarantineReason.UNDECODABLE_FILE,
                f"this file did not open as a PDF: {type(error).__name__}: {error}",
            )

        first = pages[0].lower() if pages else ""
        if SLICE_ANCHOR not in first:
            return self._file_level(
                digest,
                QuarantineReason.UNRECOGNISED_FORMAT,
                f"this is a PDF, but page 1 does not carry {SLICE_ANCHOR!r}, so it "
                f"is not a Slice statement and will not be parsed as one",
            )

        parsed = validate_balance_chain(parse_text(PAGE_SEPARATOR.join(pages)))
        return AdapterResult(
            format_id=self.format_id,
            format_version=self.format_version,
            records=list(parsed.records),
            quarantined=list(parsed.quarantined),
            file_sha256=digest,
            row_hashes=[row_fingerprint(self.format_id, r) for r in parsed.records],
            # Not a codec. A PDF's text is decoded by pypdf out of a content
            # stream, so there is no "utf-8 or latin-1" answer to report and
            # inventing one would be a lie in a field other adapters fill with a
            # fact.
            encoding="pdf-text",
            skipped_rows=parsed.skipped_rows,
        )

    def _file_level(
        self, digest: str, reason: QuarantineReason, detail: str
    ) -> AdapterResult:
        return AdapterResult(
            format_id=self.format_id,
            format_version=self.format_version,
            records=[],
            quarantined=[QuarantinedRow(row_number=1, raw="", reason=reason, detail=detail)],
            file_sha256=digest,
            row_hashes=[],
            encoding="pdf-text",
            skipped_rows=0,
        )
