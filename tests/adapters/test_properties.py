"""Property-based tests over every adapter, with `hypothesis`.

These do not replace the seeded-mutation sweep in `test_hardening.py`; they sit
next to it. The sweep is 200 hand-chosen byte-level mutations per format,
deterministic and reproducible from the test name alone -- which is what you
want when a failure has to be understood a month later. What it cannot do is
surprise anybody: it damages files the way I imagined files get damaged.
`hypothesis` shrinks a real counter-example out of a space nobody imagined, so
the two are complementary and both are kept.

Four properties, each asserted for all six file-shaped formats -- plus a fifth
group, (e), for the one format that cannot answer (b). `slice-pdf-v1` reads a
paginated PDF whose rows are reconstructed from consecutive lines, so "the same
file with its rows shuffled" does not exist for it; what it answers instead is
idempotency, the money round trip in this bank's two extra renderings, and a
never-raises property aimed at the line-joining state machine. The registry is
partitioned between the two groups and the partition is asserted, so a format
still cannot escape this module by being unusual.

**(a) Arbitrary bytes never raise.** `parse` may raise `UndecodableFileError`
and nothing else; `ingest` may raise nothing at all. Whatever a merchant
uploads has to be recorded, and a traceback is not a record.

**(b) Row-permutation invariance.** The order of the rows in a file is not
information. Reordering must produce the same records and the same quarantine
reasons.

Two formats can go further than that and two cannot, and the difference is
worth stating rather than smoothing over. `razorpay-settlement-v2` and
`orders-csv-shopify-v1` derive record identity from the file's own content --
an `entity_id`, an order `Name` -- so a permutation leaves every record and
every row hash byte-identical, and the test asserts sorted hashes match. The
two bank CSVs synthesise `line_id` from the physical line number, and
`mt940-v1` additionally derives a running `balance` from position, and
`cod-remittance-delhivery-v1` names a remittance-level deduction leg after the
line it sat on. For those four, position IS part of the record by design (see
each adapter's docstring for why), so the invariant is asserted over the
projection that excludes exactly the positional part -- and nothing broader is
claimed.

**(c) Money round-trips exactly.** An arbitrary paise integer, rendered the way
a real file renders it -- plain, Indian-grouped, symbol-prefixed, and for MT940
with a decimal comma -- parses back to the same integer. No float, ever.

**(d) Idempotency.** Parsing the same bytes twice gives identical records and
identical hashes. That is the primitive the phase-3 re-upload path rests on.
Its neighbour is the same claim across *different* bytes carrying the same
content: Windows line endings and Excel's UTF-8 BOM must change the file hash
and change nothing else. That is the shape a real upload arrives in far more
often than the LF, no-BOM shape the fixtures are written in.

**What these found in the phase-1 adapters: nothing.** Recorded because a
property suite that finds nothing is only worth having if it is on the record
that it looked. It looked at arbitrary bytes, at this-format headers over
arbitrary cells, at every permutation of every clean and dirty fixture, at
paise values across thirteen orders of magnitude in four renderings, and at
CRLF and BOM variants of every fixture.

Example counts are capped so the whole suite stays inside its time budget, and
`deadline=None` is set wherever a parse touches the filesystem -- a first
example that happens to run during a GC pause is not a failure.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from core.adapters import registry
from core.adapters.bank_hdfc import HDFCStatementAdapter
from core.adapters.bank_icici import ICICIStatementAdapter
from core.adapters.base import (
    UndecodableFileError,
    iter_csv_rows,
    parse_paise,
    strip_comment_lines,
)
from core.adapters import bank_slice
from core.adapters.cod_remittance import CODRemittanceAdapter
from core.adapters.mt940 import MT940Adapter, parse_mt940_amount
from core.adapters.orders_shopify import ShopifyOrdersAdapter
from core.adapters.razorpay_settlement import RazorpaySettlementAdapter

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "real-formats"

#: A scratch directory made once at import. Deliberately NOT pytest's
#: `tmp_path`: a function-scoped fixture is created once and then shared by
#: every example of a `@given` test, which hypothesis flags as a health check
#: because it silently makes examples depend on each other.
SCRATCH = Path(tempfile.mkdtemp(prefix="adapter-properties-"))

#: Shared budget. Small on purpose -- every one of these runs against all six
#: formats, and the suite has a wall-clock ceiling that matters more than a few
#: extra examples. The whole module is about 30 seconds of a ~2:20 suite at
#: these numbers; raise them locally when hunting something, not in the commit.
FILE_EXAMPLES = 15
PERMUTATION_EXAMPLES = 10
MONEY_EXAMPLES = 120


def _write(name: str, payload: bytes) -> Path:
    path = SCRATCH / name
    path.write_bytes(payload)
    return path


# --- what varies between the formats ---------------------------------------


def _permute_csv(text: str, order: list[int]) -> str:
    """Reorder a CSV's data rows, leaving comments and header where they are.

    Rows are split on physical lines. Every fixture involved here is free of
    embedded newlines inside quoted fields, so this is exact for them; a
    file where it was not would simply produce a different valid file, which
    is still a fair input to the property.
    """
    body, comment_count = strip_comment_lines(text)
    lines = text.splitlines()
    comments = lines[:comment_count]
    header, *rows = body.splitlines()
    reordered = [rows[index] for index in order]
    return "\n".join([*comments, header, *reordered]) + "\n"


def _mt940_units(text: str) -> tuple[list[str], list[list[str]], list[str]]:
    """Split an MT940 message into `(prologue, movement blocks, epilogue)`.

    A movement is a `:61:` and everything up to the next tag that is not part
    of it -- its `:86:` narration and any continuation lines. Those are the
    units a permutation may reorder; the balances and the header may not move,
    because moving `:62F:` above the lines would not be a permuted statement,
    it would be a different file.
    """
    body, comment_count = strip_comment_lines(text)
    lines = text.splitlines()
    prologue = lines[:comment_count]
    blocks: list[list[str]] = []
    epilogue: list[str] = []
    current: list[str] | None = None
    for line in body.splitlines():
        if line.startswith(":61:"):
            current = [line]
            blocks.append(current)
            continue
        if line.startswith(":62") or line.startswith(":64") or line.startswith(":65"):
            current = None
            epilogue.append(line)
            continue
        if current is not None and not line.startswith(":20:"):
            current.append(line)
            continue
        current = None
        if blocks:
            epilogue.append(line)
        else:
            prologue.append(line)
    return prologue, blocks, epilogue


def _permute_mt940(text: str, order: list[int]) -> str:
    prologue, blocks, epilogue = _mt940_units(text)
    reordered = [line for index in order for line in blocks[index]]
    return "\n".join([*prologue, *reordered, *epilogue]) + "\n"


def _row_count_csv(text: str) -> int:
    body, _ = strip_comment_lines(text)
    return len(body.splitlines()) - 1


def _row_count_mt940(text: str) -> int:
    return len(_mt940_units(text)[1])


@dataclass(frozen=True)
class Format:
    """One adapter, its clean fixture, and how this module may shuffle it."""

    format_id: str
    adapter: object
    clean: Path
    permute: Callable[[str, list[int]], str]
    row_count: Callable[[str], int]
    #: Fields that are positional by design and therefore not invariant under a
    #: permutation. Empty means the whole record is invariant, and the row
    #: hashes are asserted invariant too.
    positional_fields: tuple[str, ...]

    @property
    def order_independent_hashes(self) -> bool:
        return not self.positional_fields


FORMATS = (
    Format(
        "razorpay-settlement-v2",
        RazorpaySettlementAdapter(),
        FIXTURES / "razorpay-settlement-clean.csv",
        _permute_csv,
        _row_count_csv,
        # `txn_id` is the report's own `entity_id`. Nothing is positional.
        positional_fields=(),
    ),
    Format(
        "bank-csv-hdfc-v1",
        HDFCStatementAdapter(),
        FIXTURES / "hdfc-statement-clean.csv",
        _permute_csv,
        _row_count_csv,
        # `line_id` is `HDFC-<physical line>`: the statement carries no stable
        # per-line id, so determinism comes from position.
        positional_fields=("line_id",),
    ),
    Format(
        "bank-csv-icici-v1",
        ICICIStatementAdapter(),
        FIXTURES / "icici-statement-clean.csv",
        _permute_csv,
        _row_count_csv,
        positional_fields=("line_id",),
    ),
    Format(
        "mt940-v1",
        MT940Adapter(),
        FIXTURES / "mt940-statement-clean.sta",
        _permute_mt940,
        _row_count_mt940,
        # `balance` too: MT940 prints only an opening and a closing figure, so
        # the per-line balance is the running chain and moving a line moves it.
        positional_fields=("line_id", "balance"),
    ),
    Format(
        "orders-csv-shopify-v1",
        ShopifyOrdersAdapter(),
        FIXTURES / "shopify-orders-clean.csv",
        _permute_csv,
        _row_count_csv,
        # `order_id` is the export's own `Name`, and a line-item continuation
        # is recognised by shape rather than by adjacency -- which is exactly
        # what makes this format survive a permutation intact.
        positional_fields=(),
    ),
    Format(
        "cod-remittance-delhivery-v1",
        CODRemittanceAdapter(),
        FIXTURES / "cod-remittance-clean.csv",
        _permute_csv,
        _row_count_csv,
        # A remittance-level deduction row carries no waybill, so its legs are
        # named after the line it sat on. Every other leg is named after a
        # waybill and is invariant.
        positional_fields=("txn_id",),
    ),
)

FORMAT_IDS = [fmt.format_id for fmt in FORMATS]

#: The CSV formats, which are the ones whose DIRTY fixture can be permuted
#: line-for-line. MT940's movement blocks belong to a statement and its dirty
#: fixture holds two, so shuffling them across the file would not be a permuted
#: message -- it would be a different one, and a test that failed on it would
#: be reporting the shuffler's bug as the adapter's.
CSV_FORMATS = [fmt for fmt in FORMATS if fmt.format_id != "mt940-v1"]
CSV_FORMAT_IDS = [fmt.format_id for fmt in CSV_FORMATS]

DIRTY_FIXTURES = {
    "razorpay-settlement-v2": "razorpay-settlement-dirty.csv",
    "bank-csv-hdfc-v1": "hdfc-statement-dirty.csv",
    "bank-csv-icici-v1": "icici-statement-dirty.csv",
    "orders-csv-shopify-v1": "shopify-orders-dirty.csv",
    "cod-remittance-delhivery-v1": "cod-remittance-dirty.csv",
}




def _project(record, positional_fields: tuple[str, ...]) -> tuple:
    dumped = record.model_dump()
    for field in positional_fields:
        dumped.pop(field, None)
    return tuple(sorted((key, str(value)) for key, value in dumped.items()))


#: Formats whose rows are NOT permutable, and which are covered by their own
#: properties further down rather than by the shuffling ones above.
#:
#: `slice-pdf-v1` is the only member and the reason is inherent to the format
#: rather than a convenience. A Slice statement is paginated and a transaction
#: is reconstructed from consecutive lines: page order is meaningful, line order
#: within a row is the row, and "the same file with its rows shuffled" is not a
#: thing that can be written down. Shuffling it would not test an invariant, it
#: would test the shuffler. What replaces the permutation properties is
#: idempotency -- parsing the same text twice must give the same answer -- plus
#: the money round trip and the never-raises property, which are the parts that
#: do transfer.
PAGE_ORDERED_FORMAT_IDS = ["slice-pdf-v1"]


def test_every_registered_adapter_is_covered_by_these_properties():
    """A format that quietly escaped this module would be the one that broke.

    The check is against the registry rather than against a list, so adding an
    adapter without adding it here fails right away. The registry is partitioned
    rather than compared to one list: a paginated format cannot answer the
    permutation properties, and pretending otherwise would mean either a
    meaningless test or a silent exemption.
    """
    assert {adapter.format_id for adapter in registry.adapters()} == (
        set(FORMAT_IDS) | set(PAGE_ORDERED_FORMAT_IDS)
    )
    assert not set(FORMAT_IDS) & set(PAGE_ORDERED_FORMAT_IDS), (
        "a format may not be in both groups; it would be permuted and exempted "
        "from permutation at the same time"
    )


# --- (a) arbitrary bytes never raise ---------------------------------------


@pytest.mark.parametrize("fmt", FORMATS, ids=FORMAT_IDS)
@settings(max_examples=FILE_EXAMPLES, deadline=None)
@given(payload=st.binary(min_size=0, max_size=1500))
def test_arbitrary_bytes_never_raise_past_the_adapter(fmt: Format, payload: bytes):
    path = _write(f"bytes-{fmt.format_id}.bin", payload)
    try:
        result = fmt.adapter.parse(path)
    except UndecodableFileError:
        return  # the one exception `parse` is allowed, and it is a result
    assert len(result.row_hashes) == result.record_count


@settings(max_examples=FILE_EXAMPLES * 2, deadline=None)
@given(payload=st.binary(min_size=0, max_size=1500))
def test_ingest_never_raises_at_all(payload: bytes):
    path = _write("bytes-ingest.bin", payload)
    result = registry.ingest(path)
    assert len(result.row_hashes) == result.record_count
    assert result.records or result.quarantined or result.skipped_rows >= 0


#: Cell contents a spreadsheet, a locale or a careless export actually produces.
CELLS = st.one_of(
    st.text(
        alphabet=st.characters(
            min_codepoint=32, max_codepoint=0x2FFF, blacklist_categories=("Cs",)
        ),
        max_size=24,
    ),
    st.sampled_from(
        [
            "",
            "0.00",
            "-1",
            "NULL",
            "None",
            "nan",
            "1e5",
            "1,23,456.78",
            "₹250.00",
            "0.005",
            "2026-13-45",
            "  ",
            '"',
            "#",
        ]
    ),
)


@pytest.mark.parametrize("fmt", CSV_FORMATS, ids=CSV_FORMAT_IDS)
@settings(max_examples=FILE_EXAMPLES, deadline=None)
@given(rows=st.lists(st.lists(CELLS, min_size=1, max_size=25), max_size=8))
def test_a_real_header_with_arbitrary_cells_never_raises(fmt: Format, rows):
    """Nastier than random bytes, because it gets PAST detection.

    Random bytes mostly fail to decode or fail to look like the format, so they
    exercise the outer guards. A file with this adapter's own header and
    garbage underneath reaches the mapping, which is where the interesting
    failures live.

    CSV formats only, and by parametrisation rather than by a skip inside the
    body: MT940 has no header row at all, so there is no such file to build,
    and a skipped test reads like a gap where this is a category. The
    equivalent for MT940 is the arbitrary-tag-sequence test below.
    """
    text = fmt.clean.read_text(encoding="utf-8")
    body, _ = strip_comment_lines(text)
    header = body.splitlines()[0]
    lines = [header] + [",".join(cell.replace("\n", " ") for cell in row) for row in rows]
    path = _write(f"cells-{fmt.format_id}.csv", ("\n".join(lines) + "\n").encode("utf-8"))

    result = fmt.adapter.parse(path)
    assert len(result.row_hashes) == result.record_count

    # Expected row count comes from the CSV reader, NOT from `len(rows)`. A
    # generated cell holding a bare `"` opens a quoted field and swallows the
    # following line, so two generated lines are legitimately one CSV record --
    # that is the csv module doing its job, not the adapter losing a row. What
    # is being asserted is the mapping's arithmetic: every record the file
    # actually contains comes back as a record, a quarantine or a skip.
    csv_rows = max(len(list(iter_csv_rows(path.read_text(encoding="utf-8")))) - 1, 0)
    assert (
        result.record_count + result.quarantine_count + result.skipped_rows == csv_rows
        or result.record_count > csv_rows  # one row may become several legs
    )
    assert result.quarantine_count + result.skipped_rows <= csv_rows


@settings(max_examples=FILE_EXAMPLES, deadline=None)
@given(
    lines=st.lists(
        st.tuples(
            st.sampled_from([":20:", ":25:", ":28C:", ":60F:", ":61:", ":62F:", ":86:", ""]),
            st.text(
                alphabet=st.characters(min_codepoint=32, max_codepoint=127), max_size=40
            ),
        ),
        max_size=14,
    )
)
def test_arbitrary_mt940_tag_sequences_never_raise(lines):
    """MT940's equivalent of the header test: real tags, arbitrary content, in
    an arbitrary order -- including messages with no `:20:`, two `:62F:` and a
    `:61:` after the closing balance."""
    adapter = MT940Adapter()
    text = "\n".join(f"{tag}{content}" for tag, content in lines) + "\n"
    path = _write("tags.sta", text.encode("utf-8"))
    result = adapter.parse(path)
    assert len(result.row_hashes) == result.record_count


# --- (b) row-permutation invariance ----------------------------------------


@pytest.mark.parametrize("fmt", FORMATS, ids=FORMAT_IDS)
@settings(
    max_examples=PERMUTATION_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.data_too_large],
)
@given(data=st.data())
def test_row_order_is_not_information(fmt: Format, data):
    text = fmt.clean.read_text(encoding="utf-8")
    count = fmt.row_count(text)
    order = data.draw(st.permutations(range(count)))

    original = fmt.adapter.parse(_write(f"perm-base-{fmt.format_id}", text.encode("utf-8")))
    shuffled = fmt.adapter.parse(
        _write(
            f"perm-shuffled-{fmt.format_id}",
            fmt.permute(text, list(order)).encode("utf-8"),
        )
    )

    assert shuffled.record_count == original.record_count
    assert shuffled.skipped_rows == original.skipped_rows
    assert sorted(q.reason.value for q in shuffled.quarantined) == sorted(
        q.reason.value for q in original.quarantined
    )
    assert sorted(_project(r, fmt.positional_fields) for r in shuffled.records) == sorted(
        _project(r, fmt.positional_fields) for r in original.records
    )
    if fmt.order_independent_hashes:
        assert sorted(shuffled.row_hashes) == sorted(original.row_hashes)
    # Hashes are always unique within a file, whatever order it arrived in.
    assert len(set(shuffled.row_hashes)) == len(shuffled.row_hashes)


@pytest.mark.parametrize("fmt", FORMATS, ids=FORMAT_IDS)
@settings(max_examples=PERMUTATION_EXAMPLES, deadline=None)
@given(data=st.data())
def test_the_money_a_file_carries_does_not_depend_on_row_order(fmt: Format, data):
    """The blunt version of the invariant, and the one a finance controller
    would ask for: shuffling the file must not change what it is worth."""
    text = fmt.clean.read_text(encoding="utf-8")
    order = data.draw(st.permutations(range(fmt.row_count(text))))

    def total(result) -> int:
        return sum(
            (getattr(record, "amount", None) or 0)
            + (getattr(record, "credit", None) or 0)
            - (getattr(record, "debit", None) or 0)
            + (getattr(record, "gross_amount", None) or 0)
            for record in result.records
        )

    original = fmt.adapter.parse(_write(f"money-base-{fmt.format_id}", text.encode("utf-8")))
    shuffled = fmt.adapter.parse(
        _write(
            f"money-shuffled-{fmt.format_id}",
            fmt.permute(text, list(order)).encode("utf-8"),
        )
    )
    assert total(shuffled) == total(original)


@pytest.mark.parametrize("fmt", CSV_FORMATS, ids=CSV_FORMAT_IDS)
@settings(max_examples=PERMUTATION_EXAMPLES, deadline=None)
@given(data=st.data())
def test_row_order_is_not_information_on_a_broken_file_either(fmt: Format, data):
    """The harder half of the invariant.

    On a clean file a permutation only has to leave good rows good. On a broken
    one it has to leave the QUARANTINE stable too -- and that is where an
    order-dependent decision would show, because duplicate detection is the one
    rule in this layer that looks at rows other than the one in hand. Which of
    a duplicate pair gets reported moves with the shuffle; how many, and under
    what reason, must not.
    """
    path = FIXTURES / DIRTY_FIXTURES[fmt.format_id]
    text = path.read_text(encoding="utf-8")
    order = data.draw(st.permutations(range(fmt.row_count(text))))

    original = fmt.adapter.parse(
        _write(f"dirty-base-{fmt.format_id}", text.encode("utf-8"))
    )
    shuffled = fmt.adapter.parse(
        _write(
            f"dirty-shuffled-{fmt.format_id}",
            fmt.permute(text, list(order)).encode("utf-8"),
        )
    )

    assert sorted(q.reason.value for q in shuffled.quarantined) == sorted(
        q.reason.value for q in original.quarantined
    )
    assert shuffled.record_count == original.record_count
    assert shuffled.skipped_rows == original.skipped_rows
    assert sorted(_project(r, fmt.positional_fields) for r in shuffled.records) == sorted(
        _project(r, fmt.positional_fields) for r in original.records
    )


# --- (c) money round-trips exactly -----------------------------------------

#: Up to a thousand crore in paise, positive and negative. Comfortably beyond
#: any real settlement and comfortably inside the exact range of every integer
#: type involved.
PAISE = st.integers(min_value=-10**13, max_value=10**13)


def _render_rupees(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    rupees, remainder = divmod(abs(paise), 100)
    return f"{sign}{rupees}.{remainder:02d}"


def _group_indian(digits: str) -> str:
    """`1234567` -> `12,34,567`. Last three, then pairs -- the lakh convention
    a real Indian export writes and a three-digit grouper gets wrong."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join([*parts, tail])


@settings(max_examples=MONEY_EXAMPLES, deadline=None)
@given(paise=PAISE)
def test_a_paise_integer_rendered_as_rupees_parses_back_exactly(paise: int):
    assert parse_paise(_render_rupees(paise)) == paise


@settings(max_examples=MONEY_EXAMPLES, deadline=None)
@given(paise=PAISE)
def test_indian_digit_grouping_round_trips(paise: int):
    sign = "-" if paise < 0 else ""
    rupees, remainder = divmod(abs(paise), 100)
    grouped = f"{sign}{_group_indian(str(rupees))}.{remainder:02d}"
    assert parse_paise(grouped) == paise


@settings(max_examples=MONEY_EXAMPLES, deadline=None)
@given(paise=st.integers(min_value=0, max_value=10**13))
def test_a_rupee_symbol_prefix_round_trips(paise: int):
    assert parse_paise(f"₹{_render_rupees(paise)}") == paise
    assert parse_paise(f"INR {_render_rupees(paise)}") == paise


@settings(max_examples=MONEY_EXAMPLES, deadline=None)
@given(paise=st.integers(min_value=0, max_value=10**13))
def test_an_mt940_decimal_comma_amount_round_trips(paise: int):
    """The same property under the other convention. MT940 writes `342614,53`
    where a CSV writes `342614.53`, and a parser that confused them would be
    off by four orders of magnitude with no row looking wrong."""
    rupees, remainder = divmod(paise, 100)
    assert parse_mt940_amount(f"{rupees},{remainder:02d}") == paise


@settings(max_examples=MONEY_EXAMPLES, deadline=None)
@given(paise=PAISE)
def test_no_rendering_of_an_exact_paise_value_is_ever_refused(paise: int):
    """The contrapositive of the sub-paise rule, which is the half that could
    quietly break: refusing 46556.545 is right, and refusing 46556.54 would be
    a bug that quarantines a merchant's whole export."""
    for rendering in (
        _render_rupees(paise),
        f" {_render_rupees(paise)} ",
    ):
        assert parse_paise(rendering) == paise


@settings(max_examples=MONEY_EXAMPLES, deadline=None)
@given(
    paise=st.integers(min_value=0, max_value=10**11),
    third_digit=st.integers(min_value=1, max_value=9),
)
def test_a_sub_paise_value_is_always_refused_and_never_rounded(
    paise: int, third_digit: int
):
    rupees, remainder = divmod(paise, 100)
    with pytest.raises(ValueError):
        parse_paise(f"{rupees}.{remainder:02d}{third_digit}")


@settings(max_examples=FILE_EXAMPLES, deadline=None)
@given(
    collections=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=10**9),  # COD collected
            st.integers(min_value=0, max_value=10**7),  # freight
            st.integers(min_value=0, max_value=10**7),  # COD handling
            st.integers(min_value=0, max_value=10**7),  # RTO
            st.integers(min_value=0, max_value=10**7),  # GST
        ),
        min_size=1,
        max_size=12,
    )
)
def test_a_generated_remittance_nets_to_exactly_the_sum_of_its_paise(collections):
    """Money round-trip end to end, not just through `parse_paise`.

    Arbitrary paise integers are rendered as rupee decimals into a real
    remittance file, parsed by the real adapter, and the legs must sum to the
    integer arithmetic done here in Python. This is the property that would
    catch a float anywhere along the path -- rendering, parsing, or the
    signing of a deduction leg -- because a float would be exact for small
    numbers and wrong for large ones, which is precisely what a wide integer
    range explores.
    """
    adapter = CODRemittanceAdapter()
    header = (
        "Remittance Ref,UTR,Remittance Date,Waybill,Order ID,Row Type,"
        "Delivery Date,COD Amount,Freight Charge,COD Handling Fee,RTO Charge,GST"
    )
    lines = [header]
    expected = 0
    for index, (cod, freight, fee, rto, gst) in enumerate(collections):
        if not any((cod, freight, fee, rto, gst)):
            # A row that moves no money is quarantined by design, so it would
            # contribute nothing and make the expected total wrong to assert.
            cod = 1
        expected += cod - freight - fee - rto - gst
        lines.append(
            f"DLV/REM/PROP01,UTRPROP0001,14-08-2026,WB{index:06d},ORD-{index:06d},"
            f"COD,12-08-2026,{_render_rupees(cod)},{_render_rupees(freight)},"
            f"{_render_rupees(fee)},{_render_rupees(rto)},{_render_rupees(gst)}"
        )
    payload = "\n".join([*lines, ""]).encode("utf-8")
    path = _write("generated-remittance.csv", payload)

    result = adapter.parse(path)
    assert result.quarantined == [], [q.detail for q in result.quarantined]
    assert sum(record.amount for record in result.records) == expected
    assert isinstance(sum(record.amount for record in result.records), int)


# --- (d) idempotency --------------------------------------------------------


@pytest.mark.parametrize("fmt", FORMATS, ids=FORMAT_IDS)
@settings(max_examples=FILE_EXAMPLES, deadline=None)
@given(payload=st.binary(min_size=0, max_size=1500))
def test_parsing_arbitrary_bytes_twice_gives_the_same_answer(fmt: Format, payload: bytes):
    """Idempotency on the inputs that are hardest to be idempotent on. A parse
    that took a different path the second time -- through a cache, a mutated
    default argument, an iterator consumed once -- would show up here long
    before it showed up on a clean file."""
    path = _write(f"idem-{fmt.format_id}.bin", payload)
    try:
        first = fmt.adapter.parse(path)
        second = fmt.adapter.parse(path)
    except UndecodableFileError:
        with pytest.raises(UndecodableFileError):
            fmt.adapter.parse(path)
        return
    assert first.file_sha256 == second.file_sha256
    assert first.row_hashes == second.row_hashes
    assert [record.model_dump() for record in first.records] == [
        record.model_dump() for record in second.records
    ]
    assert [(q.row_number, q.reason, q.detail) for q in first.quarantined] == [
        (q.row_number, q.reason, q.detail) for q in second.quarantined
    ]


@pytest.mark.parametrize("fmt", FORMATS, ids=FORMAT_IDS)
@settings(max_examples=FILE_EXAMPLES, deadline=None)
@given(name=st.text(alphabet="abcdefgh -()", min_size=1, max_size=12))
def test_the_same_bytes_under_any_name_are_the_same_upload(fmt: Format, name: str):
    """Identity is content. `august.csv` and `august (1).csv` holding the same
    bytes are one upload, and detection may not consult the name to decide."""
    payload = fmt.clean.read_bytes()
    original = fmt.adapter.parse(_write(f"named-base-{fmt.format_id}", payload))
    renamed = fmt.adapter.parse(_write(f"{name}-{fmt.format_id}", payload))
    assert renamed.file_sha256 == original.file_sha256
    assert renamed.row_hashes == original.row_hashes
    assert renamed.format_id == original.format_id


# --- (d, continued) the same content in different bytes --------------------


@pytest.mark.parametrize("fmt", FORMATS, ids=FORMAT_IDS)
@settings(max_examples=PERMUTATION_EXAMPLES, deadline=None)
@given(data=st.data())
def test_line_endings_and_a_byte_order_mark_are_not_data(fmt: Format, data):
    """Windows line endings and Excel's BOM must not change a single record.

    This is the shape a real upload arrives in far more often than the LF,
    no-BOM shape the fixtures are written in: a merchant opens the export in
    Excel, saves it, and gets CRLF and a UTF-8 BOM for free. An adapter that
    let either reach a narration or an amount would be wrong on almost every
    real file while passing every fixture test in this suite.

    The file hash MUST differ -- the bytes did change, and content-addressing
    that could not tell them apart would be broken in the other direction.
    """
    import codecs

    text = fmt.clean.read_text(encoding="utf-8")
    order = data.draw(st.permutations(range(fmt.row_count(text))))
    shuffled = fmt.permute(text, list(order))

    base = fmt.adapter.parse(
        _write(f"eol-base-{fmt.format_id}", shuffled.encode("utf-8"))
    )
    crlf = shuffled.replace("\n", "\r\n")
    variants = {
        "crlf": crlf.encode("utf-8"),
        "bom": codecs.BOM_UTF8 + shuffled.encode("utf-8"),
        "bom-crlf": codecs.BOM_UTF8 + crlf.encode("utf-8"),
    }
    for name, payload in variants.items():
        other = fmt.adapter.parse(_write(f"eol-{name}-{fmt.format_id}", payload))
        assert [r.model_dump() for r in other.records] == [
            r.model_dump() for r in base.records
        ], f"{fmt.format_id}: {name} changed the records"
        assert other.row_hashes == base.row_hashes, f"{fmt.format_id}: {name}"
        # Row NUMBERS too, not just reasons. A Windows line ending counted as
        # two lines would still report the right defects, at the wrong lines,
        # on every file a merchant ever exports through Excel.
        assert [(q.row_number, q.reason, q.raw) for q in other.quarantined] == [
            (q.row_number, q.reason, q.raw) for q in base.quarantined
        ], f"{fmt.format_id}: {name}"
        assert other.file_sha256 != base.file_sha256


# --- (e) the page-ordered format, whose properties are a different set ------
#
# `slice-pdf-v1` cannot answer (b): see `PAGE_ORDERED_FORMAT_IDS` above. These
# are what it answers instead, and they are asserted against the pure stage --
# `parse_text` -- because that is where every decision this format makes lives.
# Reaching it directly also means no PDF has to be written to test it, which is
# the whole point of the two-stage split.


SLICE_CLEAN = FIXTURES / "slice-statement-clean.txt"


@settings(max_examples=FILE_EXAMPLES, deadline=None)
@given(payload=st.text(max_size=1200))
def test_arbitrary_text_never_raises_past_the_slice_parser(payload: str):
    """The (a) property in the shape this format's pure stage takes it.

    Arbitrary bytes are covered for the adapter as a whole by
    `test_ingest_never_raises_at_all`; this is the sharper version, because
    `parse_text` is where the line-joining state machine lives and a state
    machine is the thing that runs off the end of its own input.
    """
    parsed = bank_slice.parse_text(payload)
    assert bank_slice.validate_balance_chain(parsed) is not None
    assert parsed.skipped_rows >= 0


@settings(max_examples=FILE_EXAMPLES, deadline=None)
@given(
    payload=st.lists(
        st.sampled_from(
            [
                "02 Apr '25 UPI-Debit-987654321013-A Trader-EXBK0001234-a@examplebank",
                "1234567890123456 -₹85 ₹4,215.50",
                "123456789012 ₹1,500 ₹5,715.50",
                "01 Apr '25 - 30 Apr '25",
                "1/2",
                "amplebank-987654321012",
                "slice small finance bank",
                "",
                "\f",
            ]
        ),
        max_size=25,
    )
)
def test_arbitrary_arrangements_of_real_lines_never_raise(payload: list[str]):
    """The nastier half: not random text, but the format's OWN lines in orders
    the format never produces -- a tail with no date, a date with no tail, a
    page break mid-row, a header where a continuation belongs. This is where a
    two-sided delimiter rule goes wrong if it is going to."""
    parsed = bank_slice.validate_balance_chain(
        bank_slice.parse_text("\n".join(payload))
    )
    assert len(parsed.records) >= 0
    for record in parsed.records:
        assert (record.credit is None) != (record.debit is None)


@settings(max_examples=PERMUTATION_EXAMPLES, deadline=None)
@given(data=st.data())
def test_parsing_slice_text_twice_gives_the_same_answer(data):
    """The (d) property. Idempotency is what replaces permutation invariance
    here: the order of a paginated statement is fixed, so the claim that can
    still be made -- and the one the re-upload path actually rests on -- is that
    parsing it again changes nothing."""
    suffix = data.draw(st.sampled_from(["", "\n", "\r\n", "\n\n"]))
    text = SLICE_CLEAN.read_text(encoding="utf-8") + suffix

    first = bank_slice.validate_balance_chain(bank_slice.parse_text(text))
    second = bank_slice.validate_balance_chain(bank_slice.parse_text(text))

    assert [r.model_dump() for r in first.records] == [
        r.model_dump() for r in second.records
    ]
    assert [(q.row_number, q.reason, q.raw) for q in first.quarantined] == [
        (q.row_number, q.reason, q.raw) for q in second.quarantined
    ]
    assert first.skipped_rows == second.skipped_rows


@settings(max_examples=MONEY_EXAMPLES, deadline=None)
@given(paise=PAISE)
def test_a_paise_integer_rendered_the_way_slice_renders_it_round_trips(paise: int):
    """The (c) property, extended to the two renderings this bank uses and no
    other adapter's fixture contains: a whole rupee amount printed with NO
    decimal part at all, and a balance with its trailing zero trimmed.

    Both were discovered on the genuine artefact, and the second one is the
    reason this test exists rather than being folded into the shared money
    properties: `1234.50` printed as `1234.5` is a paise value that a reader
    expecting two places would get wrong by a factor of ten, silently.
    """
    amount = abs(paise)
    rupees, remainder = divmod(amount, 100)
    plain = f"{rupees}" if remainder == 0 else f"{rupees}.{remainder:02d}"
    assert parse_paise(plain) == amount

    if remainder % 10 == 0 and remainder != 0:
        trimmed = f"{rupees}.{remainder // 10}"
        assert parse_paise(trimmed) == amount


def test_the_money_a_slice_statement_carries_survives_the_parse():
    """The blunt version, and the one the balance chain makes checkable.

    Every other format's money property is asserted under a shuffle. This one
    cannot be, so it is asserted against the statement's own redundancy instead:
    the net of every credit and debit must equal the distance the running
    balance travelled. That is a stronger statement than a sum, because it ties
    the amounts to a column the adapter parsed separately.
    """
    parsed = bank_slice.validate_balance_chain(
        bank_slice.parse_text(SLICE_CLEAN.read_text(encoding="utf-8"))
    )
    assert parsed.quarantined == []
    assert len(parsed.records) >= 2

    moved = sum((r.credit or 0) - (r.debit or 0) for r in parsed.records[1:])
    travelled = parsed.records[-1].balance - parsed.records[0].balance
    assert moved == travelled
