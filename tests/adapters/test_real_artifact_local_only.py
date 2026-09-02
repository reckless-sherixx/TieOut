"""The one test that reads a genuine bank artefact. Local only, aggregates only.

**What this file is for.** `VALIDATION.md` classifies every claim in this
repository by the evidence behind it, and until `slice-pdf-v1` landed the
strongest class available was "a hand fixture written from a published schema".
This test is the only thing in the suite that can produce the class above it:
it parses a real 29-page Slice statement that a real person downloaded from
their own account, and it records what came out.

**The privacy contract, which has no exceptions.**

1. The artefact lives at `incoming/slice-statement.pdf`. `incoming/` is in
   `.gitignore`. The file is never committed and never leaves the machine it
   was downloaded onto.
2. This test asserts **aggregates only** -- row counts, page counts, the
   chain-closure verdict, quarantine counts by reason. It never asserts, prints
   or compares a narration, a counterparty, a VPA, a reference, an amount or a
   balance, and it must never be edited to.
3. It **skips** when the file is absent, which is what it does on every machine
   but one and in CI always. A skip here is the normal outcome, not a gap.
4. Nothing it learns reaches a committed fixture. The committed fixtures are
   hand-written synthetic text with invented values; see
   `fixtures/real-formats/slice-statement-clean.txt`.

**Why aggregates are worth anything.** The balance chain is the statement's own
redundancy: every row's balance must equal the previous one plus a credit or
minus a debit. A wrapped-line reconstruction that mis-joins a row takes an
amount or a balance from the wrong place and the chain stops closing. So
"532 rows, 0 quarantined, chain closes across all 531 transitions" is a strong
claim about the parser that can be made without disclosing a single row -- and
it is the claim `VALIDATION.md` section 4 records.

The figures asserted below are LOWER BOUNDS and invariants rather than the
exact numbers observed, so that the test does not have to be edited when the
account holder downloads a longer statement next month. The exact figures
observed on the run of record are in `VALIDATION.md` and
`SLICE-ADAPTER-REPORT.md`.
"""

from __future__ import annotations

import collections
from pathlib import Path

import pytest

from core.adapters import registry
from core.adapters.bank_slice import SlicePDFStatementAdapter, extract_pages

#: Not a fixture and never will be. `conftest.py` does not know about it, no
#: other test may read it, and the directory it sits in is ignored by git.
ARTEFACT = (
    Path(__file__).resolve().parent.parent.parent / "incoming" / "slice-statement.pdf"
)

pytestmark = pytest.mark.skipif(
    not ARTEFACT.is_file(),
    reason=(
        "the genuine Slice statement is not present. It is a personal bank "
        "statement, deliberately never committed, so this is the expected "
        "outcome everywhere except the one machine that holds it."
    ),
)


@pytest.fixture(scope="module")
def parsed():
    return SlicePDFStatementAdapter().parse(ARTEFACT)


def test_the_artefact_is_a_multi_page_pdf_with_an_extractable_text_layer():
    pages = extract_pages(ARTEFACT)
    assert len(pages) >= 20
    assert all(page.strip() for page in pages), "a page with no text layer"


def test_detection_picks_this_adapter_off_the_bytes_alone():
    assert registry.detect(ARTEFACT).format_id == "slice-pdf-v1"


def test_every_page_carries_the_sniff_anchor():
    """The anchor is the bank's own running header -- boilerplate slice prints,
    not anything belonging to the account holder. That it is on every page and
    not only page 1 is what makes it a layout fact rather than a coincidence of
    the cover."""
    from core.adapters.bank_slice import SLICE_ANCHOR

    pages = extract_pages(ARTEFACT)
    assert all(SLICE_ANCHOR in page.lower() for page in pages)


def test_the_whole_statement_parses(parsed):
    assert parsed.format_id == "slice-pdf-v1"
    assert parsed.record_count >= 500
    assert len(parsed.row_hashes) == parsed.record_count
    assert len(set(parsed.row_hashes)) == parsed.record_count


def test_nothing_in_the_genuine_statement_is_quarantined(parsed):
    """The headline. Zero is the assertion because zero is what happened, and a
    tolerance here would let a regression in the line-joining rules hide behind
    "a few bad rows are normal" -- which for this file they are not."""
    by_reason = collections.Counter(q.reason.value for q in parsed.quarantined)
    assert parsed.quarantine_count == 0, f"quarantine by reason: {dict(by_reason)}"


def test_the_balance_chain_closes_across_the_entire_statement(parsed):
    """The proof that the wrapped lines were reconstructed correctly.

    Five hundred-odd rows, each spread over one to four lines of extracted text,
    rejoined by shape alone -- and every one of them lands where the previous
    row's balance says it must. A single mis-join would show here.
    """
    records = parsed.records
    assert len(records) >= 2
    breaks = 0
    previous = records[0].balance
    for record in records[1:]:
        if previous + (record.credit or 0) - (record.debit or 0) != record.balance:
            breaks += 1
        previous = record.balance
    assert breaks == 0, f"{breaks} of {len(records) - 1} transitions did not close"


def test_every_row_has_exactly_one_direction(parsed):
    for record in parsed.records:
        assert (record.credit is None) != (record.debit is None)


def test_every_row_is_inside_the_statement_period(parsed):
    """A row whose date came from somewhere other than its own line -- a page
    header, say -- would almost certainly land outside the range the rest of the
    file occupies. Asserted as a spread rather than as dates, which are content.
    """
    dates = [record.txn_date for record in parsed.records]
    assert (max(dates) - min(dates)).days <= 400


def test_furniture_is_accounted_for_and_not_mistaken_for_rows(parsed):
    """The cover block, the two-line header on every page and the footer are
    skipped, counted, and greater in number than the pages -- which is the
    cheapest way to say "the per-page headers were all recognised"."""
    assert parsed.skipped_rows >= 2 * len(extract_pages(ARTEFACT))


def test_the_aggregates_are_reported_for_the_record(parsed, capsys):
    """Prints the numbers that go into `VALIDATION.md`, and NOTHING else.

    Every value below is a count, a percentage or a type name. No narration, no
    counterparty, no VPA, no reference, no amount and no balance is printed
    here, and none may be added.
    """
    records = parsed.records
    closed = sum(
        1
        for previous, record in zip(records, records[1:])
        if previous.balance + (record.credit or 0) - (record.debit or 0)
        == record.balance
    )
    transitions = max(len(records) - 1, 1)
    lines = [
        "",
        "--- slice-pdf-v1 :: genuine artefact aggregates (no row content) ---",
        f"pages                 : {len(extract_pages(ARTEFACT))}",
        f"rows parsed           : {parsed.record_count}",
        f"quarantined           : {parsed.quarantine_count} "
        f"{dict(collections.Counter(q.reason.value for q in parsed.quarantined))}",
        f"skipped (furniture)   : {parsed.skipped_rows}",
        f"chain closure         : {closed}/{transitions} "
        f"({100.0 * closed / transitions:.2f}%)",
        f"credits / debits      : {sum(1 for r in records if r.credit)} / "
        f"{sum(1 for r in records if r.debit)}",
        f"rows with a UTR       : {sum(1 for r in records if r.utr)}",
        f"distinct row hashes   : {len(set(parsed.row_hashes))}",
        "--------------------------------------------------------------------",
    ]
    with capsys.disabled():
        print("\n".join(lines))
    assert closed == transitions
