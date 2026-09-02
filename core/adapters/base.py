"""The source-adapter contract: what every real-file reader must provide.

`core/ingest/reader.py` is the *strict* reader. It reads this project's own
canonical CSVs, where money is already integer paise and a malformed row is a
programming error worth crashing on. Adapters sit in FRONT of that layer. They
read files a merchant actually has -- a Razorpay settlement export, an HDFC
statement, an ICICI statement -- and emit the same canonical records
(`Order`, `PSPTransaction`, `BankLine`) the engine already understands. The
engine and the canonical schema do not change.

Four rules give this module its shape, and each one exists because a real file
broke something:

1. **Quarantine, never crash, never silently drop.** A row this layer cannot
   turn into a canonical record becomes a `QuarantinedRow` carrying the raw
   line, a machine-readable reason and the physical line number; parsing
   continues with the next row. An ingest that dies on row 40,312 of a bank
   export is not a product, and one that skips row 40,312 without saying so is
   worse -- the merchant's books are then quietly short by one line.

2. **Exact decimal money.** Real files carry rupee-decimal strings
   ("46556.54"), not paise integers. `parse_paise` converts them through
   `decimal.Decimal` and **never** `float`. A value that does not land exactly
   on a paise boundary is quarantined, not rounded: 46556.545 rupees has no
   integer-paise answer, so this layer does not invent one.

3. **Sniffing is by header shape, never by filename.** `sniff` is handed bytes
   and nothing else -- there is no parameter through which a filename could
   reach it. A file called `icici-report.csv` that contains an HDFC header is
   an HDFC file.

4. **Content hashing at file and row level.** `AdapterResult.file_sha256` and
   `row_hashes` are the primitives a later upload path uses to make re-upload
   idempotent. This phase builds them; phase 3 wires them.

Encodings get their own paragraph because real bank exports are filthy. UTF-8
is tried first, then UTF-8-with-BOM is recognised and its BOM stripped, then
latin-1. A file that survives none of that is a *file-level* quarantine
(`UndecodableFileError`), which the caller records the same way it records a
row-level one -- not a traceback.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from core.models import BankLine, Order, PSPTransaction

#: How many bytes of a file `detect` hands to each `sniff`. A header row plus a
#: couple of data rows is plenty to recognise a layout, and reading a fixed
#: prefix means detection costs the same on a 12-row fixture and a 400MB export.
SNIFF_BYTES = 8192

#: Below this, `detect` refuses rather than guesses. A confidence in
#: (0.0, 0.6) means "some columns looked familiar", which is exactly the
#: situation where guessing produces a plausible-looking wrong parse.
DETECTION_THRESHOLD = 0.60


class QuarantineReason(str, Enum):
    """Why a row (or a file) could not become a canonical record.

    These strings are stable and machine-readable: the quarantine review screen
    groups by them, so renaming one is a breaking change for that screen.
    """

    #: An amount column held something that is not an exact rupee decimal, or
    #: is a rupee decimal with sub-paise precision. Both are refusals, not
    #: rounding opportunities.
    BAD_DECIMAL = "BAD_DECIMAL"
    #: A date or timestamp column did not match any format this layout uses.
    BAD_DATE = "BAD_DATE"
    #: A required cell was present-but-empty.
    MISSING_VALUE = "MISSING_VALUE"
    #: The row had fewer fields than the header -- a line cut short.
    TRUNCATED_ROW = "TRUNCATED_ROW"
    #: The row had more fields than the header -- usually an unquoted comma
    #: inside a narration.
    EXTRA_FIELDS = "EXTRA_FIELDS"
    #: Byte-identical to a row already seen in this same file.
    DUPLICATE_ROW = "DUPLICATE_ROW"
    #: A categorical cell held a value this layout does not define.
    UNKNOWN_VALUE = "UNKNOWN_VALUE"
    #: The row is well-formed but describes something the canonical schema does
    #: not carry (a Route transfer, say). Visible, never dropped.
    UNSUPPORTED_ROW_TYPE = "UNSUPPORTED_ROW_TYPE"
    #: Credit and debit both populated, or neither -- direction is undecidable.
    AMBIGUOUS_DIRECTION = "AMBIGUOUS_DIRECTION"
    #: The row's own columns disagree: a settlement line whose gross minus fee
    #: minus tax is not the credit it claims. Quarantined whole, because a
    #: half-trusted settlement row silently changes what a batch nets to.
    ARITHMETIC_MISMATCH = "ARITHMETIC_MISMATCH"
    #: The row produced values the canonical model itself rejected.
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    #: File-level: no supported encoding decoded the bytes.
    UNDECODABLE_FILE = "UNDECODABLE_FILE"
    #: File-level: the header did not carry a column the layout requires.
    MISSING_HEADER_COLUMN = "MISSING_HEADER_COLUMN"
    #: File-level: the file decoded fine and no adapter recognised its header,
    #: or two recognised it equally well. Distinct from the one above because
    #: the fixes differ -- "your export is missing a column" against "we do not
    #: read this format yet".
    UNRECOGNISED_FORMAT = "UNRECOGNISED_FORMAT"


@dataclass(frozen=True)
class QuarantinedRow:
    """One row that did not become a record, and everything needed to fix it.

    `raw` is the row exactly as it appeared, so a human can look at the line
    and see the problem. `row_number` counts physical lines from 1 with the
    header included -- the number an editor shows, which is the entire point of
    recording it.
    """

    row_number: int
    raw: str
    reason: QuarantineReason
    detail: str


CanonicalRecord = Order | PSPTransaction | BankLine


@dataclass(frozen=True)
class AdapterResult:
    """Everything one parse produced: records, quarantine, and the hashes.

    `row_hashes` is positionally parallel to `records` -- `row_hashes[i]` is the
    fingerprint of `records[i]`. That parity is checked in `__post_init__`
    rather than documented and hoped for, because a dedup path that trusts a
    misaligned pair of lists deduplicates the wrong row.
    """

    format_id: str
    format_version: str
    records: list[CanonicalRecord]
    quarantined: list[QuarantinedRow]
    #: SHA-256 of the file's raw bytes, before any decoding. The same file
    #: uploaded twice hashes the same regardless of how it decoded.
    file_sha256: str
    #: SHA-256 per emitted record, in record order. See `row_fingerprint`.
    row_hashes: list[str]
    #: Which codec actually decoded the file. Reported, not guessed at again:
    #: "this ICICI export was latin-1" is a fact worth showing a user.
    encoding: str
    #: Rows the layout deliberately ignores (a trailing "Statement Summary"
    #: block, say) -- counted so the arithmetic `data rows == records +
    #: quarantined + skipped` always closes.
    skipped_rows: int = field(default=0)

    def __post_init__(self) -> None:
        if len(self.row_hashes) != len(self.records):
            raise ValueError(
                "row_hashes must be positionally parallel to records: "
                f"{len(self.row_hashes)} hashes for {len(self.records)} records"
            )

    @property
    def record_count(self) -> int:
        return len(self.records)

    @property
    def quarantine_count(self) -> int:
        return len(self.quarantined)


class AdapterError(Exception):
    """Base for the two failures that are about the file, not a row."""


class UndecodableFileError(AdapterError):
    """No supported encoding decoded the bytes: a file-level quarantine.

    Raised rather than returned because there is no row to attach it to and no
    partial result to return. The caller turns it into a file-level quarantine
    record; what it must not do is let it surface as a traceback.
    """


class FormatDetectionError(AdapterError):
    """`detect` would have had to guess, so it refused.

    Two causes, both named in the message along with every candidate and its
    confidence: nothing cleared `DETECTION_THRESHOLD`, or the top two tied.
    """


@runtime_checkable
class SourceAdapter(Protocol):
    """One real file format, in front of the canonical schema."""

    #: Stable identifier, layout included -- "razorpay-settlement-v2",
    #: "bank-csv-hdfc-v1". A different layout is a different id, never a flag
    #: on an existing adapter.
    format_id: str
    #: The revision of that layout this adapter implements.
    format_version: str

    def sniff(self, head: bytes) -> float:
        """Confidence in 0.0..1.0 that `head` is this format. Bytes only."""

    def parse(self, path: Path) -> AdapterResult:
        """Parse the whole file. Raises only `UndecodableFileError`."""


# --- exact decimal money ----------------------------------------------------

#: A rupee amount as a bank or PSP writes one: optional sign, optional currency
#: symbol, digits with optional grouping commas (Indian 1,23,456.78 grouping
#: included, which is why the grouping is not validated positionally), and at
#: most two decimal places. Exponent notation, bare dots and "nan" all fail
#: here rather than reaching `Decimal`, which would happily accept the last two.
_RUPEE_RE = re.compile(r"^[+-]?\d[\d,]*(?:\.\d{1,2})?$")

_STRIPPED_PREFIXES = ("₹", "Rs.", "Rs", "INR")


def parse_paise(raw: str) -> int:
    """Exact rupee-decimal string -> integer paise. Never `float`.

    Accepts what real files carry: "46556.54", "1,23,456.78", "0.00", "-12.34",
    "₹250.00", and integers with no decimal point at all.

    Refuses -- with `ValueError` naming the offending text -- anything that does
    not land exactly on a paise boundary. "46556.545" is the case that matters:
    it is 4655654.5 paise, there is no integer answer, and inventing one loses
    half a paise on every row of a 40,000-row export. Quarantine is the correct
    outcome; rounding is not.
    """
    text = (raw or "").strip()
    for prefix in _STRIPPED_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    if not text:
        raise ValueError("empty amount: an amount column must carry a number")
    if not _RUPEE_RE.match(text):
        raise ValueError(
            f"{raw!r} is not an exact rupee amount: expected an optional sign, "
            f"digits (grouping commas allowed) and at most two decimal places"
        )
    try:
        rupees = Decimal(text.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - regex already excludes
        raise ValueError(f"{raw!r} is not a decimal number") from exc
    paise = rupees * 100
    if paise != paise.to_integral_value():
        raise ValueError(
            f"{raw!r} is {paise} paise, which is not a whole number of paise; "
            f"sub-paise values are quarantined, never rounded"
        )
    return int(paise)


def parse_optional_paise(raw: str | None) -> int | None:
    """`parse_paise`, except that an absent cell is `None` rather than an error.

    "Absent" is the empty string or whitespace only -- never the literal text
    "None", "NULL", "nan" or "-", each of which is a value someone's export
    tooling invented and each of which this function refuses so it lands in
    quarantine where a human can see it.
    """
    if raw is None or not raw.strip():
        return None
    return parse_paise(raw)


# --- encodings --------------------------------------------------------------

#: Bytes that no rupee statement contains and that latin-1 would silently turn
#: into text. NUL is the giveaway for a spreadsheet, zip or UTF-16 file renamed
#: to .csv; without this guard latin-1 "succeeds" on every binary file, since
#: latin-1 maps all 256 byte values.
_BINARY_MARKERS = (b"\x00",)


def decode_bytes(payload: bytes) -> tuple[str, str]:
    """Decode a statement file, returning `(text, encoding_name)`.

    Order is UTF-8-with-BOM, UTF-8, then latin-1. The BOM check comes first
    because `utf-8` decodes a BOM'd file successfully but leaves U+FEFF glued
    to the first header name, which then fails to match any expected column and
    produces a mystifying "missing header column" error on a perfectly good
    file.

    latin-1 is a genuine fallback, not a rubber stamp: it cannot fail on any
    byte string, so a binary guard runs first. Otherwise every .xlsx renamed to
    .csv would "decode" into mojibake and be reported as a header problem
    instead of as the wrong file.
    """
    if any(marker in payload for marker in _BINARY_MARKERS):
        raise UndecodableFileError(
            "file contains NUL bytes, so it is not a text export -- it is most "
            "likely a spreadsheet or archive renamed to .csv"
        )
    if payload.startswith(b"\xef\xbb\xbf"):
        try:
            return payload.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    try:
        return payload.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return payload.decode("latin-1"), "latin-1"
    except UnicodeDecodeError as exc:  # pragma: no cover - latin-1 cannot fail
        raise UndecodableFileError(
            "no supported encoding (utf-8, utf-8-sig, latin-1) decoded this file"
        ) from exc


def read_text(path: Path | str) -> tuple[str, str, bytes]:
    """Read a file as `(text, encoding_name, raw_bytes)`.

    The raw bytes come back too because `file_sha256` hashes them *before*
    decoding: the identity of an upload is its bytes, not its interpretation.
    """
    raw = Path(path).read_bytes()
    text, encoding = decode_bytes(raw)
    return text, encoding, raw


# --- hashing ----------------------------------------------------------------


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def row_fingerprint(format_id: str, record: BaseModel) -> str:
    """Stable content hash of one canonical record.

    Scoped by `format_id` so that two adapters emitting a coincidentally
    identical record do not collide in a dedup table -- provenance is part of
    the identity of an ingested row.

    Stability rests on `model_dump_json()` serialising pydantic fields in
    declaration order, which makes the hash a function of the record's values
    and the frozen field order in `core/models.py`. A field added to a frozen
    model would change every fingerprint, which is correct: it would be a
    different record shape.
    """
    payload = f"{format_id}\n{record.model_dump_json()}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# --- header shape helpers (shared by every CSV adapter's `sniff`) -----------

#: A hand-written fixture needs to say where its schema came from, and CSV has
#: no comment syntax. Leading lines beginning with `#` are treated as provenance
#: and skipped before the header is read -- by `sniff` and `parse` alike, so a
#: commented fixture and an uncommented real export detect identically.
COMMENT_PREFIX = "#"


def strip_comment_lines(text: str) -> tuple[str, int]:
    """Drop leading `#` provenance lines; return `(body, lines_dropped)`."""
    lines = text.splitlines(keepends=True)
    dropped = 0
    for line in lines:
        if line.startswith(COMMENT_PREFIX):
            dropped += 1
            continue
        break
    return "".join(lines[dropped:]), dropped


#: Whitespace sitting next to punctuation, which real exports place
#: inconsistently: ICICI ships `Deposit Amount (INR )` in some statements and
#: `Deposit Amount (INR)` in others, and they are the same column.
_PAD_AROUND_PUNCTUATION = re.compile(r"\s*([^\w\s])\s*")


def normalise_header(name: str) -> str:
    """Fold a header cell to a comparison key.

    Case is noise. So is whitespace: runs collapse to one space, and any space
    touching punctuation disappears entirely, so `Deposit Amount (INR )` and
    `Deposit Amount (INR)` fold together. Punctuation itself is KEPT, because
    `Chq./Ref.No.` versus `Chq/Ref No` is a real difference between two bank
    layouts and folding it away would let two adapters tie on one file.

    Expected column names are run through this same function rather than being
    written pre-folded, so an adapter declares its columns the way its bank
    prints them.
    """
    folded = " ".join((name or "").strip().lower().split())
    return _PAD_AROUND_PUNCTUATION.sub(r"\1", folded)


@dataclass(frozen=True)
class RawRow:
    """One physical CSV record, before any interpretation.

    `raw` is the exact source text (newline stripped) so a quarantine record can
    show the operator the line they have to fix, and `row_number` is the
    physical line that text starts on, counting the header as line 1 after any
    provenance comments. Those two together are what makes a quarantine
    actionable rather than merely honest.
    """

    row_number: int
    raw: str
    cells: list[str]


class _LineCapture:
    """Feeds `csv.reader` while remembering the source lines of each record.

    `csv.reader` consumes an iterator of lines and gives back parsed fields; it
    never hands the original text back. A quarantine record needs that text, and
    a field containing an embedded newline means one record is not one line --
    so the lines are captured as the reader pulls them rather than being
    re-derived by splitting, which would misattribute every row after the first
    multi-line narration.
    """

    def __init__(self, lines: list[str]) -> None:
        self._iterator = iter(lines)
        self.pending: list[str] = []
        self.consumed = 0

    def __iter__(self) -> _LineCapture:
        return self

    def __next__(self) -> str:
        line = next(self._iterator)
        self.pending.append(line)
        self.consumed += 1
        return line

    def take(self) -> str:
        text = "".join(self.pending).rstrip("\r\n")
        self.pending.clear()
        return text


def iter_csv_rows(text: str, *, start_line: int = 1) -> Iterator[RawRow]:
    """Yield every CSV record of `text` with its raw source and line number.

    Blank lines are yielded as rows with no cells; the caller decides whether a
    blank line is padding to skip or a defect to quarantine, because the two
    layouts disagree about that.

    `start_line` is the physical line number of the first line of `text`, which
    is one past however many provenance comment lines were stripped.
    """
    lines = text.splitlines(keepends=True)
    capture = _LineCapture(lines)
    reader = csv.reader(capture)
    row_start = start_line
    for cells in reader:
        raw = capture.take()
        yield RawRow(row_number=row_start, raw=raw, cells=list(cells))
        row_start = start_line + capture.consumed


# --- dates ------------------------------------------------------------------


def parse_date_exact(raw: str, formats: tuple[str, ...]) -> date:
    """Parse a date against an explicit whitelist of layouts, or raise.

    No "try everything and see what sticks". HDFC writes `01/08/26` and ICICI
    writes `01-08-2026`; a permissive parser handed `01/08/26` cannot tell
    1 August from 8 January, and guessing wrong shifts a whole statement by
    months without any row looking wrong. Each adapter declares the formats its
    bank actually uses and refuses everything else.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty date")
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{raw!r} does not match any of the expected date formats {list(formats)}")


def parse_datetime_exact(raw: str, formats: tuple[str, ...]) -> datetime:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"{raw!r} does not match any of the expected timestamp formats {list(formats)}"
    )


def header_cells(head: bytes) -> list[str]:
    """Normalised header names from a sniff prefix, or `[]` if undecodable.

    `sniff` must never raise: a file that is not this format is a zero, not an
    exception, and a truncated multi-byte character at the 8192-byte boundary is
    not a reason to fail detection for every adapter.
    """
    try:
        text, _ = decode_bytes(head)
    except UndecodableFileError:
        return []
    body, _ = strip_comment_lines(text)
    first = body.splitlines()[0] if body.splitlines() else ""
    return [normalise_header(cell) for cell in first.split(",")]


def header_confidence(head: bytes, required: tuple[str, ...], distinctive: tuple[str, ...]) -> float:
    """Confidence that `head`'s header is this layout.

    Zero unless every `required` column is present -- a layout missing a column
    it cannot work without is not that layout, however familiar the rest looks.
    Above that, the score is how many `distinctive` columns appear, scaled into
    0.7..1.0. `distinctive` columns are the ones that separate this layout from
    its neighbours (HDFC's `Narration` versus ICICI's `Transaction Remarks`),
    which is what keeps two bank adapters from tying on the same file.
    """
    cells = set(header_cells(head))
    wanted = [normalise_header(column) for column in required]
    if not cells or not all(column in cells for column in wanted):
        return 0.0
    if not distinctive:
        return 0.7
    hits = sum(1 for column in distinctive if normalise_header(column) in cells)
    return 0.7 + 0.3 * (hits / len(distinctive))
