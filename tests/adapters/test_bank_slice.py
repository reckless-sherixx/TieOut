"""`slice-pdf-v1`: the two stages, tested separately and then together.

**Why almost everything here is a string.** The adapter splits into
`extract_pages` (a PDF's text layer) and `parse_text` (text -> records), and
every decision the format makes lives in the second. So the fixtures are `.txt`
files of extracted text and the bulk of this module never opens a PDF at all --
which is what keeps a PDF-*writing* dependency out of the project entirely.

The extract stage still has to be tested, and it is, against a PDF assembled
byte by byte in `_minimal_pdf` below. That is roughly forty lines of PDF syntax
in exchange for not adding a library whose only job would be to produce test
input, and it has the second virtue of being explicit: the file under test is
visible in this module rather than inside somebody's writer.

**No content from the genuine artefact appears here.** The layout was read off a
real Slice statement; every value in the fixtures and in this file is invented.
The one test that touches the real file is
`test_real_artifact_local_only.py`, it is skipped when the file is absent, and
it asserts aggregates only.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from core.adapters import registry
from core.adapters.bank_slice import (
    MAX_ROW_LINES,
    PAGE_SEPARATOR,
    SLICE_ANCHOR,
    SlicePDFStatementAdapter,
    extract_pages,
    extract_utr,
    parse_text,
    validate_balance_chain,
)
from core.adapters.base import QuarantineReason
from core.models import BankLine

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "real-formats"
CLEAN = FIXTURES / "slice-statement-clean.txt"
DIRTY = FIXTURES / "slice-statement-dirty.txt"


def _parse(path: Path):
    return validate_balance_chain(parse_text(path.read_text(encoding="utf-8")))


def _clean():
    return _parse(CLEAN)


# --- registration -----------------------------------------------------------


def test_the_adapter_is_registered():
    adapter = SlicePDFStatementAdapter()
    assert adapter.format_id == "slice-pdf-v1"
    assert adapter.format_id in {a.format_id for a in registry.adapters()}


def test_the_adapter_declares_that_it_reads_a_binary_container():
    """The flag the registry keys on. Without it a PDF never reaches `sniff`,
    because the head fails to decode and the file is refused as binary before
    any adapter is asked -- which is still the correct answer for every OTHER
    adapter here."""
    assert SlicePDFStatementAdapter().reads_binary is True
    others = [a for a in registry.adapters() if a.format_id != "slice-pdf-v1"]
    assert others, "the check would pass vacuously"
    for adapter in others:
        assert getattr(adapter, "reads_binary", False) is False, adapter.format_id


# --- the clean fixture ------------------------------------------------------


def test_the_clean_fixture_parses_with_zero_quarantine():
    parsed = _clean()
    assert parsed.quarantined == []
    assert len(parsed.records) == 15


def test_every_record_is_a_bank_line_with_exactly_one_direction():
    for record in _clean().records:
        assert isinstance(record, BankLine)
        assert (record.credit is None) != (record.debit is None), record.line_id


def test_the_balance_chain_closes_end_to_end():
    """The proof that the wrapped lines were put back together correctly.

    A mis-joined row takes its amount or its balance from the wrong place, and
    the chain is what notices. A clean fixture whose chain closes is a fixture
    whose reconstruction is right.
    """
    records = _clean().records
    previous = records[0].balance
    for record in records[1:]:
        moved = (record.credit or 0) - (record.debit or 0)
        assert previous + moved == record.balance, record.line_id
        previous = record.balance


def test_the_page_header_never_becomes_a_row():
    """The trap that is on every page of the real statement: the header's first
    line is `DD Mon 'YY - DD Mon 'YY`, which begins with the row-start
    pattern."""
    parsed = _clean()
    assert not any("- 30 Apr" in r.narration for r in parsed.records)
    # Two pages, each with a period line and a page-number line, plus the
    # running bank header on each and the footer: furniture, counted not dropped.
    assert parsed.skipped_rows == 7
    assert len(parsed.records) + len(parsed.quarantined) + parsed.skipped_rows == 22


def test_a_page_number_line_never_becomes_a_row():
    parsed = parse_text("1/2\n29/29\n")
    assert parsed.records == []
    assert parsed.quarantined == []
    assert parsed.skipped_rows == 2


# --- wrapped-line reconstruction, which is the hard part --------------------


def test_a_wrap_that_falls_mid_vpa_is_rejoined_without_a_space():
    """The narration wraps at a fixed character width, so it breaks mid-token.
    A space inserted at the wrap would corrupt every wrapped VPA in the file."""
    narrations = [r.narration for r in _clean().records]
    assert any("meeratextiles99@examplebank" in n for n in narrations)
    assert not any("meeratexti les99" in n for n in narrations)
    for narration in narrations:
        assert "@ " not in narration, narration


def test_a_wrap_that_falls_mid_word_inside_a_note_is_rejoined():
    narrations = [r.narration for r in _clean().records]
    assert any(n.endswith("Payment from slice 1234567890123460") for n in narrations)
    assert any("Payment from PhonePe" in n for n in narrations)


def test_the_line_carrying_the_money_is_joined_with_a_space_not_glued_on():
    """The one continuation that is a column boundary rather than a wrap. Glue
    it on and the narration's own trailing reference merges with the row's
    reference into one meaningless digit run."""
    narrations = [r.narration for r in _clean().records]
    assert any(n.endswith("-987654321012 1234567890123456") for n in narrations)


def test_a_single_line_row_is_a_row():
    record = next(r for r in _clean().records if r.txn_date.day == 5)
    assert record.narration.startswith("Interest Cr. for 04-Apr-2025")
    assert record.credit == 35


def test_rows_do_not_join_across_a_page_boundary():
    """A row is reconstructed from consecutive lines, and a page break ends
    that. An unterminated row at the foot of a page is quarantined there rather
    than reaching forward into the next page's header."""
    text = f"02 Apr '25 UPI-Debit-1-A-a@b{PAGE_SEPARATOR}1234567890123456 -₹85 ₹4,215.50\n"
    parsed = parse_text(text)
    assert parsed.records == []
    assert [q.reason for q in parsed.quarantined] == [
        QuarantineReason.TRUNCATED_ROW,
        QuarantineReason.TRUNCATED_ROW,
    ]


# --- the date-inside-a-narration trap ---------------------------------------


def test_a_narration_that_wraps_onto_a_line_beginning_with_a_date_stays_one_row():
    """The deliberate correct-rather-than-quarantine decision.

    A date at line start begins a new row only when no row is pending. A pending
    row is by construction incomplete -- rows terminate eagerly at their money
    pair -- so a date arriving mid-row is a wrap and is joined.

    The reverse rule is the one that hides: it would quarantine the real row and
    emit a fragment carrying the real row's amount and balance, so the balance
    chain would still close and nothing downstream would look wrong.
    """
    parsed = _clean()
    trapped = [r for r in parsed.records if "Ledger Services" in r.narration]
    assert len(trapped) == 1
    assert "Invoice dated 21 Apr '25 refers" in trapped[0].narration
    assert trapped[0].txn_date.day == 20
    assert trapped[0].debit == 30000
    assert not any(r.txn_date.day == 21 for r in parsed.records)


def test_a_row_that_never_reaches_a_money_pair_is_bounded_and_quarantined():
    """What stops the rule above from costing a page. A row with no money pair
    would otherwise swallow every line after it."""
    text = "02 Apr '25 nothing ends this row\n" + "still going\n" * 20
    parsed = parse_text(text)
    assert parsed.records == []
    assert [q.reason for q in parsed.quarantined] == [QuarantineReason.TRUNCATED_ROW]
    assert str(MAX_ROW_LINES) in parsed.quarantined[0].detail
    # The lines past the bound become furniture rather than a second bad row.
    assert parsed.skipped_rows == 21 - MAX_ROW_LINES


# --- money, exactly ---------------------------------------------------------


def test_amounts_and_balances_are_exact_paise():
    record = next(r for r in _clean().records if r.txn_date.day == 18)
    assert record.debit == 123456  # ₹1,234.56, thousands comma and all
    assert record.balance == 586129


def test_a_balance_with_a_trimmed_trailing_zero_is_read_at_face_value():
    """Slice does not pad. `₹6,000` and `₹5,699.5` are what the statement
    prints, and the balance chain on the genuine artefact is what proved these
    are real values rather than a truncated text layer -- it closed across 531
    transitions with them taken literally.

    This is the case a reader assuming two decimal places gets wrong by a factor
    of ten, silently, so it is pinned here.
    """
    records = {r.txn_date.day: r for r in _clean().records}
    assert records[19].balance == 600000
    assert records[23].balance == 569950


def test_no_amount_is_ever_routed_through_a_float():
    for record in _clean().records:
        for value in (record.credit, record.debit, record.balance):
            assert value is None or isinstance(value, int)
    # The exact-decimal path, asserted the way `test_contract.py` asserts it:
    # a value float cannot hold survives.
    parsed = parse_text("02 Apr '25 x 123456789012 ₹0.07 ₹8,362,057.23\n")
    assert parsed.records[0].credit == 7
    assert parsed.records[0].balance == int(Decimal("8362057.23") * 100)


def test_a_zero_amount_has_no_direction_and_is_quarantined():
    parsed = parse_text("02 Apr '25 x 123456789012 ₹0 ₹100\n")
    assert parsed.records == []
    assert parsed.quarantined[0].reason is QuarantineReason.AMBIGUOUS_DIRECTION


def test_a_minus_sign_is_a_debit_and_its_absence_is_a_credit():
    parsed = parse_text(
        "02 Apr '25 a 123456789012 -₹85 ₹100\n03 Apr '25 b 123456789013 ₹85 ₹185\n"
    )
    assert parsed.records[0].debit == 8500 and parsed.records[0].credit is None
    assert parsed.records[1].credit == 8500 and parsed.records[1].debit is None


# --- the reference, and the narrow UTR rule ---------------------------------


def test_a_twelve_digit_reference_is_a_utr():
    record = next(r for r in _clean().records if r.txn_date.day == 22)
    assert record.utr == "123456789012"
    assert record.utr not in record.narration


@pytest.mark.parametrize(
    "reference", ["1234567890123456", "12345678901", "1234567890123", "", None]
)
def test_anything_that_is_not_exactly_twelve_digits_is_not_a_utr(reference):
    """Deliberately narrow, on the HDFC precedent. The trailing reference runs
    12 to 17 digits on the genuine artefact and a UPI UTR is exactly 12, so a
    wider rule would put an internal transaction id in a field that means UTR --
    confident nonsense, which is worse for the matcher than a null."""
    assert extract_utr(reference) is None


def test_a_reference_that_is_not_a_utr_is_kept_in_the_narration():
    """Dropping a token the bank printed is not this layer's decision."""
    record = next(r for r in _clean().records if r.txn_date.day == 2)
    assert record.utr is None
    assert record.narration.endswith("1234567890123456")


# --- line identity ----------------------------------------------------------


def test_line_ids_are_positional_and_distinct():
    """A statement line has no natural key and the reference is not distinct on
    every row, so identity is the physical line the row starts on -- the HDFC
    precedent, and file-local by design."""
    ids = [r.line_id for r in _clean().records]
    assert len(set(ids)) == len(ids)
    assert all(i.startswith("SLICE-") for i in ids)
    assert ids == sorted(ids)


# --- the dirty fixture ------------------------------------------------------


def test_the_dirty_fixture_quarantines_one_of_each_defect():
    parsed = _parse(DIRTY)
    assert len(parsed.records) == 3
    assert sorted(q.reason.value for q in parsed.quarantined) == [
        "ARITHMETIC_MISMATCH",
        "BAD_DECIMAL",
        "TRUNCATED_ROW",
        "TRUNCATED_ROW",
    ]


def test_a_sub_paise_amount_is_quarantined_and_never_rounded():
    parsed = _parse(DIRTY)
    bad = next(q for q in parsed.quarantined if q.reason is QuarantineReason.BAD_DECIMAL)
    assert "12.345" in bad.detail
    assert not any(r.txn_date.day == 4 for r in parsed.records)


def test_a_broken_chain_names_both_figures_and_only_costs_its_own_row():
    """A break is a row-level fact, not a file-level one.

    The chain continues from the balance the FILE claimed rather than from the
    one it expected, so a single wrong row does not redden every row after it --
    which would turn one defect into a page of them and bury the line a human
    has to look at.
    """
    parsed = _parse(DIRTY)
    broken = next(
        q for q in parsed.quarantined if q.reason is QuarantineReason.ARITHMETIC_MISMATCH
    )
    assert "75000" in broken.detail and "70000" in broken.detail
    # The row after the break is kept, and it chains off the claimed balance.
    assert parsed.records[-1].balance == 100000


def test_an_orphan_money_line_is_quarantined_rather_than_skipped():
    """A line carrying a reference and a money pair with no date is
    unmistakably a transaction row that lost its beginning. Everything else
    without a date is document furniture."""
    parsed = parse_text("1234567890123475 -₹25 ₹975\n")
    assert [q.reason for q in parsed.quarantined] == [QuarantineReason.TRUNCATED_ROW]
    assert "no date" in parsed.quarantined[0].detail
    assert parsed.skipped_rows == 0


def test_furniture_is_counted_and_never_silently_dropped():
    """The cover block, the running bank header and the footer are the document,
    not damaged rows -- but they are still counted, so that
    `records + quarantined + skipped` accounts for every line seen."""
    parsed = parse_text("slice small finance bank\nNeed help? Contact support\n")
    assert parsed.records == [] and parsed.quarantined == []
    assert parsed.skipped_rows == 2


def test_a_page_header_repeated_mid_file_is_skipped_not_read_as_a_row():
    parsed = _parse(DIRTY)
    assert parsed.skipped_rows == 5
    assert not any("31 May" in r.narration for r in parsed.records)


# --- sniffing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "head", [b"", b"Date,Narration,Amount\n", b"{\"a\": 1}", b"PK\x03\x04", b"%PD"]
)
def test_sniff_is_zero_without_the_pdf_magic(head):
    assert SlicePDFStatementAdapter().sniff(head) == 0.0


def test_sniff_clears_the_threshold_on_the_magic_alone():
    """The honest limitation, pinned so it cannot be quietly widened.

    In a real Slice statement the text layer is inside a compressed content
    stream, so the anchor appears NOWHERE in the file's bytes -- not in the
    first 8192 and not anywhere else. The magic number is therefore all the
    evidence `sniff` can have, and it says "a PDF". `parse` is what says
    "a Slice PDF", on the extracted text, where the anchor is always reachable.
    """
    from core.adapters.base import DETECTION_THRESHOLD

    score = SlicePDFStatementAdapter().sniff(b"%PDF-1.7\n%\x80\x80\x80\x80\nnot text")
    assert DETECTION_THRESHOLD <= score < 1.0


def test_sniff_is_certain_when_the_anchor_is_actually_reachable():
    head = b"%PDF-1.4\n" + SLICE_ANCHOR.encode() + b"\n"
    assert SlicePDFStatementAdapter().sniff(head) == 1.0
    assert SlicePDFStatementAdapter().sniff(b"%PDF-1.4\nSLICE SMALL FINANCE BANK\n") == 1.0


def test_sniff_never_raises_on_arbitrary_bytes():
    for payload in (b"\x00" * 100, bytes(range(256)), b"%PDF-" + b"\xff" * 50):
        assert 0.0 <= SlicePDFStatementAdapter().sniff(payload) <= 1.0


# --- the extract stage, against a PDF built here ----------------------------


def _minimal_pdf(pages: list[list[str]]) -> bytes:
    """A PDF, assembled by hand, whose text layer is exactly `pages`.

    Uncompressed on purpose so the file is readable in a hex dump, and carrying
    a two-entry `/ToUnicode` CMap that maps `$` to U+20B9 -- which is how a
    rupee sign gets into a Helvetica-encoded content stream at all, and which
    exercises pypdf's real text-decoding path rather than a shortcut.
    """
    kids: list[str] = []
    body: list[tuple[int, bytes]] = []
    number = 3
    for lines in pages:
        content = "BT\n/F1 10 Tf\n1 0 0 1 40 750 Tm\n12 TL\n"
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")
            content += f"({escaped}) Tj T*\n"
        content += "ET\n"
        stream = content.encode("latin-1")
        page_number, content_number = number, number + 1
        kids.append(f"{page_number} 0 R")
        body.append(
            (
                page_number,
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_number} 0 R /Resources << /Font << /F1 90 0 R >> "
                f">> >>".encode(),
            )
        )
        body.append(
            (
                content_number,
                b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
            )
        )
        number += 2

    cmap = (
        b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        b"1 beginbfchar\n<24> <20B9>\nendbfchar\nendcmap\n"
        b"CMapName currentdict /CMap defineresource pop\nend\nend"
    )
    body += [
        (
            90,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding /ToUnicode 91 0 R >>",
        ),
        (91, b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap)),
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (
            2,
            b"<< /Type /Pages /Kids [%s] /Count %d >>"
            % (" ".join(kids).encode(), len(pages)),
        ),
    ]
    body.sort()

    out = b"%PDF-1.4\n"
    offsets: dict[int, int] = {}
    for num, payload in body:
        offsets[num] = len(out)
        out += b"%d 0 obj\n%s\nendobj\n" % (num, payload)
    xref_at = len(out)
    top = max(offsets) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % top
    for index in range(1, top):
        kind = b"n" if index in offsets else b"f"
        out += b"%010d 00000 %s \n" % (offsets.get(index, 0), kind)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        top,
        xref_at,
    )
    return out


SYNTHETIC_PAGES = [
    [
        SLICE_ANCHOR,
        "01 Apr '25 - 30 Apr '25",
        "1/2",
        "02 Apr '25 UPI-Debit-987654321013-A Trader-EXBK0001234-a@examp",
        "lebank",
        "1234567890123456 -$85 $4,215.50",
    ],
    [
        SLICE_ANCHOR,
        "01 Apr '25 - 30 Apr '25",
        "2/2",
        "03 Apr '25 UPI-Credit-987654321014-B Trader-EXBK0005678-b@examplebank",
        "123456789012 $1,500 $5,715.50",
    ],
]


def test_extract_pages_returns_one_string_per_page_in_order(tmp_path: Path):
    path = tmp_path / "statement.pdf"
    path.write_bytes(_minimal_pdf(SYNTHETIC_PAGES))
    pages = extract_pages(path)
    assert len(pages) == 2
    assert "1/2" in pages[0] and "2/2" in pages[1]
    assert SLICE_ANCHOR in pages[0].lower()


def test_the_two_stages_compose_into_the_same_answer(tmp_path: Path):
    """The join between the stages, which is the only thing the `.txt` fixtures
    cannot cover: that `parse` feeds `parse_text` exactly what
    `extract_pages` produced, separated by the page separator."""
    path = tmp_path / "statement.pdf"
    path.write_bytes(_minimal_pdf(SYNTHETIC_PAGES))

    through_the_adapter = SlicePDFStatementAdapter().parse(path)
    through_the_pure_stage = validate_balance_chain(
        parse_text(PAGE_SEPARATOR.join(extract_pages(path)))
    )

    assert [r.model_dump() for r in through_the_adapter.records] == [
        r.model_dump() for r in through_the_pure_stage.records
    ]
    assert through_the_adapter.record_count == 2
    assert through_the_adapter.quarantine_count == 0
    assert through_the_adapter.records[0].debit == 8500
    assert through_the_adapter.records[1].credit == 150000
    assert through_the_adapter.records[1].utr == "123456789012"
    # The mid-VPA wrap survived a round trip through a real PDF text layer.
    assert through_the_adapter.records[0].narration.count("a@examplebank") == 1


def test_a_real_pdf_is_detected_as_this_format(tmp_path: Path):
    path = tmp_path / "anything.pdf"
    path.write_bytes(_minimal_pdf(SYNTHETIC_PAGES))
    assert registry.detect(path).format_id == "slice-pdf-v1"
    assert registry.ingest(path).format_id == "slice-pdf-v1"


def test_the_result_carries_the_files_own_bytes_hash(tmp_path: Path):
    import hashlib

    path = tmp_path / "statement.pdf"
    path.write_bytes(_minimal_pdf(SYNTHETIC_PAGES))
    result = SlicePDFStatementAdapter().parse(path)
    assert result.file_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(result.row_hashes) == result.record_count
    assert len(set(result.row_hashes)) == result.record_count
    assert result.encoding == "pdf-text"


# --- a PDF that is not a Slice statement ------------------------------------


def test_a_pdf_without_the_anchor_is_refused_rather_than_parsed(tmp_path: Path):
    """Detection can only say "a PDF"; this is where "a SLICE PDF" is decided.

    Somebody else's bank statement must not be read through this adapter's
    grammar on the strength of a magic number.
    """
    path = tmp_path / "someone-elses.pdf"
    path.write_bytes(
        _minimal_pdf([["Global Trust Bank Ltd", "02 Apr '25 x 123456789012 -$85 $100"]])
    )
    result = SlicePDFStatementAdapter().parse(path)
    assert result.records == []
    assert [q.reason for q in result.quarantined] == [
        QuarantineReason.UNRECOGNISED_FORMAT
    ]
    assert SLICE_ANCHOR in result.quarantined[0].detail


# --- nothing is ever a traceback --------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [b"", b"%PDF-1.4\n", b"%PDF-1.4\ngarbage" * 30, b"\x00" * 200, bytes(range(256))],
)
def test_a_file_that_is_not_a_readable_pdf_is_a_record_not_a_traceback(
    payload, tmp_path: Path
):
    path = tmp_path / "broken.pdf"
    path.write_bytes(payload)
    result = SlicePDFStatementAdapter().parse(path)
    assert result.records == []
    assert result.quarantine_count == 1
    assert result.quarantined[0].reason in {
        QuarantineReason.UNDECODABLE_FILE,
        QuarantineReason.UNRECOGNISED_FORMAT,
    }
    assert len(result.row_hashes) == result.record_count


def test_a_missing_file_is_a_record_too(tmp_path: Path):
    result = SlicePDFStatementAdapter().parse(tmp_path / "never-written.pdf")
    assert result.quarantined[0].reason is QuarantineReason.UNDECODABLE_FILE


def test_a_truncated_pdf_does_not_raise(tmp_path: Path):
    whole = _minimal_pdf(SYNTHETIC_PAGES)
    for cut in (10, len(whole) // 3, len(whole) // 2, len(whole) - 20):
        path = tmp_path / f"cut-{cut}.pdf"
        path.write_bytes(whole[:cut])
        result = SlicePDFStatementAdapter().parse(path)
        assert len(result.row_hashes) == result.record_count


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n\n\n",
        PAGE_SEPARATOR * 5,
        "₹",
        "₹₹ ₹₹",
        "02 Apr '25",
        "02 Apr '25 ₹ ₹",
        "99 Zzz '99 x 123456789012 ₹1 ₹1",
        "1234567890123456 -₹85 ₹4,215.50",
    ],
)
def test_parse_text_never_raises_on_degenerate_input(text):
    parsed = validate_balance_chain(parse_text(text))
    assert isinstance(parsed.records, list)
    assert isinstance(parsed.quarantined, list)


@pytest.mark.parametrize("line", ["99 Zzz '99", "32 Apr '25", "00 Feb '25"])
def test_a_date_shaped_but_impossible_date_is_quarantined_not_crashed(line):
    """The row-start pattern is a SHAPE. A line matching it reaches
    `parse_date_exact`, which accepts one layout and refuses everything else --
    so an impossible date is one quarantined row, never a guess and never a
    traceback."""
    parsed = parse_text(f"{line} x 123456789012 ₹1 ₹1\n")
    assert parsed.records == []
    assert [q.reason for q in parsed.quarantined] == [QuarantineReason.BAD_DATE]
    assert line in parsed.quarantined[0].detail


# --- the silent zero, on the shape real data had ----------------------------
#
# 2026-09-02: the IMAP connector pulled nine attachments from a real bank. Two
# were one-page PDFs of roughly 1,200 characters carrying the bank's running
# header and no transaction rows. They sniffed at 0.70 -- ABOVE
# `DETECTION_THRESHOLD`, because the magic number says "a PDF" -- so this
# adapter accepted them, and each returned zero records and zero quarantine
# rows. Nothing anywhere told the merchant the file had contributed nothing.
#
# `fixtures/real-formats/slice-statement-empty.txt` is that shape with every
# value invented. The first two tests pin the defect at the parser -- which is
# CORRECT behaviour there, because none of those lines is a damaged row -- and
# the third shows the ingest boundary turning it into one visible failure.

EMPTY = FIXTURES / "slice-statement-empty.txt"


def _empty_statement_lines() -> list[str]:
    """The fixture's document lines, ready for `_minimal_pdf`.

    Provenance comments are dropped because a PDF has no comment syntax to put
    them in, and the rupee sign becomes `$` for the reason `SYNTHETIC_PAGES`
    already does it: a Helvetica content stream is latin-1, and U+20B9 reaches
    the extracted text through the `/ToUnicode` map instead.
    """
    return [
        line.replace("₹", "$")
        for line in EMPTY.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]


def test_a_statement_with_no_rows_parses_to_nothing_at_all():
    """The defect, reproduced. Every line is furniture, so the parser has
    nothing to report: no records AND no quarantine. Held here so that if a
    later change makes the parser itself speak up, this test is what says the
    boundary check has become redundant rather than the two drifting apart."""
    parsed = validate_balance_chain(parse_text(EMPTY.read_text(encoding="utf-8")))
    assert parsed.records == []
    assert parsed.quarantined == []
    assert parsed.skipped_rows > 0


def test_the_same_document_as_a_pdf_is_accepted_by_detection_and_still_says_nothing(
    tmp_path: Path,
):
    """End to end through the real adapter, from a PDF, exactly as the real
    files arrived: detection clears the threshold and the result is silent.

    The genuine files scored 0.70 because their text layer sits in a compressed
    content stream, so the anchor was unreachable at sniff time. This one is
    uncompressed and therefore scores higher -- the score is not what makes it a
    silent zero, CLEARING THE THRESHOLD is, and that is what is asserted.
    """
    from core.adapters.base import DETECTION_THRESHOLD

    path = tmp_path / "0333xxxxxxx1300.pdf"
    path.write_bytes(_minimal_pdf([_empty_statement_lines()]))

    assert SlicePDFStatementAdapter().sniff(path.read_bytes()[:8192]) >= (
        DETECTION_THRESHOLD
    )
    assert registry.detect(path).format_id == "slice-pdf-v1"

    result = SlicePDFStatementAdapter().parse(path)
    assert result.records == []
    assert result.quarantined == []


def test_the_ingest_boundary_turns_that_silence_into_one_named_failure(
    tmp_path: Path,
):
    """What the merchant now sees instead of nothing: one file-level
    `EMPTY_DOCUMENT` row naming the format that read the file and found no
    transactions in it."""
    from api.ingest import enforce_visible_outcome

    path = tmp_path / "0333xxxxxxx5081.pdf"
    path.write_bytes(_minimal_pdf([_empty_statement_lines()]))

    rows = enforce_visible_outcome(SlicePDFStatementAdapter().parse(path))
    assert len(rows) == 1
    assert rows[0].reason is QuarantineReason.EMPTY_DOCUMENT
    assert "slice-pdf-v1" in rows[0].detail
    # No byte of the document reaches the reason. Same rule as `UploadRefused`.
    assert rows[0].raw == ""
