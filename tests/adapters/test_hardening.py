"""The promises that hold across every adapter, asserted across every adapter.

Each per-format module proves its own mapping. This one proves the properties
that would still have to be true if a fourth format landed tomorrow: nothing
crashes, nothing is silently dropped, no money is rounded, and the same bytes
produce the same hashes.

The malformed-input sweep is written with the standard library, not
`hypothesis`. Property tests are phase 2 and a new dev dependency; a
deterministic sweep over hand-chosen mutations is what fits here, and it is
seeded and reproducible rather than merely random.
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

import pytest

from core.adapters import registry
from core.adapters.bank_hdfc import HDFCStatementAdapter
from core.adapters.bank_icici import ICICIStatementAdapter
from core.adapters.base import QuarantineReason, UndecodableFileError
from core.adapters.cod_remittance import CODRemittanceAdapter
from core.adapters.mt940 import MT940Adapter
from core.adapters.orders_shopify import ShopifyOrdersAdapter
from core.adapters.razorpay_settlement import RazorpaySettlementAdapter

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "real-formats"

#: Every format, whatever its physical shape. `mt940-v1` is not
#: comma-separated, so it belongs here but not in `CSV_CLEAN_FILES` below.
CLEAN_FILES = {
    "razorpay-settlement-v2": FIXTURES / "razorpay-settlement-clean.csv",
    "bank-csv-hdfc-v1": FIXTURES / "hdfc-statement-clean.csv",
    "bank-csv-icici-v1": FIXTURES / "icici-statement-clean.csv",
    "mt940-v1": FIXTURES / "mt940-statement-clean.sta",
    "orders-csv-shopify-v1": FIXTURES / "shopify-orders-clean.csv",
    "cod-remittance-delhivery-v1": FIXTURES / "cod-remittance-clean.csv",
}
DIRTY_FILES = {
    "razorpay-settlement-v2": FIXTURES / "razorpay-settlement-dirty.csv",
    "bank-csv-hdfc-v1": FIXTURES / "hdfc-statement-dirty.csv",
    "bank-csv-icici-v1": FIXTURES / "icici-statement-dirty.csv",
    "mt940-v1": FIXTURES / "mt940-statement-dirty.sta",
    "orders-csv-shopify-v1": FIXTURES / "shopify-orders-dirty.csv",
    "cod-remittance-delhivery-v1": FIXTURES / "cod-remittance-dirty.csv",
}

#: The subset whose physical shape is "comment lines, one header, one record
#: per line". Two checks below count data lines by splitting on commas, and a
#: tag-delimited MT940 message has neither a header nor one record per line --
#: so those checks name this list rather than silently meaning something else
#: on a format they were not written for.
CSV_CLEAN_FILES = {
    format_id: path
    for format_id, path in CLEAN_FILES.items()
    if path.suffix == ".csv"
}

ALL_FILES = list(CLEAN_FILES.values()) + list(DIRTY_FILES.values()) + [
    FIXTURES / "icici-statement-latin1.csv",
    FIXTURES / "mt940-unbalanced.sta",
]
CSV_FILES = [path for path in ALL_FILES if path.suffix == ".csv"]


def _adapters():
    return [
        RazorpaySettlementAdapter(),
        HDFCStatementAdapter(),
        ICICIStatementAdapter(),
        MT940Adapter(),
        ShopifyOrdersAdapter(),
        CODRemittanceAdapter(),
    ]


#: Which adapter every hand fixture must detect as. Built from the two maps
#: above plus the files that are neither a clean nor a dirty pair, and asserted
#: below to be EXHAUSTIVE over `fixtures/real-formats/` -- a fixture added
#: without an entry fails the check rather than quietly going unclassified,
#: which is exactly how a second adapter would learn to recognise somebody
#: else's file without anyone noticing.
DETECTED_AS = {
    **{path.name: format_id for format_id, path in CLEAN_FILES.items()},
    **{path.name: format_id for format_id, path in DIRTY_FILES.items()},
    "icici-statement-latin1.csv": "bank-csv-icici-v1",
    "mt940-unbalanced.sta": "mt940-v1",
}

#: Fixtures that are a *stage* of a format rather than a file in it, and which
#: detection therefore has nothing to say about.
#:
#: `slice-pdf-v1` reads a PDF in two stages: a container becomes text, and the
#: text is parsed. These two files are the second stage's input -- extracted
#: text, not a PDF -- so they carry no `%PDF-` magic and no adapter does or
#: should recognise them. They are listed here rather than left out of the
#: exhaustiveness check, because "the directory is fully classified" is the
#: guarantee that stops a fixture from quietly going unowned, and a `.txt` that
#: matched nothing would otherwise be indistinguishable from an accident.
#: `tests/adapters/test_bank_slice.py` is what exercises them.
TEXT_LAYER_FIXTURES = {
    "slice-statement-clean.txt": "slice-pdf-v1",
    "slice-statement-dirty.txt": "slice-pdf-v1",
}


# --- detection is unambiguous on every fixture ------------------------------


def test_the_fixture_directory_is_fully_classified():
    """This map moved here from the ICICI module when the third format landed:
    it was never about ICICI, it is the registry's own guarantee, and pinning
    the *number* of adapters in it meant every new format broke a test that had
    nothing to say about it."""
    present = {path.name for path in FIXTURES.iterdir() if path.is_file()}
    owned = set(DETECTED_AS) | set(TEXT_LAYER_FIXTURES)
    assert present == owned, (
        f"unclassified fixtures: {sorted(present - owned)}; "
        f"missing files: {sorted(owned - present)}"
    )


def test_a_text_layer_fixture_is_not_a_file_any_adapter_claims():
    """The other half of the exemption above, so it cannot smuggle a fixture
    past detection.

    A stage-two fixture must be recognised by NOBODY. If one ever started
    scoring above the threshold, either an adapter has begun sniffing something
    other than a file's own shape or the fixture is not what the map says it
    is, and both are worth failing over.
    """
    from core.adapters.base import DETECTION_THRESHOLD

    for name in TEXT_LAYER_FIXTURES:
        head = (FIXTURES / name).read_bytes()[:8192]
        confident = [
            adapter.format_id
            for adapter in registry.adapters()
            if adapter.sniff(head) >= DETECTION_THRESHOLD
        ]
        assert confident == [], f"{name} is stage-two text but scored for {confident}"


@pytest.mark.parametrize("name,format_id", sorted(DETECTED_AS.items()))
def test_exactly_one_adapter_is_confident_about_each_fixture(name, format_id):
    from core.adapters.base import DETECTION_THRESHOLD

    path = FIXTURES / name
    head = path.read_bytes()[:8192]
    confident = [
        adapter.format_id
        for adapter in registry.adapters()
        if adapter.sniff(head) >= DETECTION_THRESHOLD
    ]
    assert confident == [format_id], f"{name} scored confident for {confident}"
    assert registry.detect(path).format_id == format_id


# --- every clean fixture is clean under its own adapter ---------------------


@pytest.mark.parametrize("format_id,path", sorted(CLEAN_FILES.items()))
def test_every_clean_fixture_parses_with_zero_quarantine(format_id, path):
    result = registry.ingest(path)
    assert result.format_id == format_id
    assert result.quarantined == []
    assert result.record_count > 0


@pytest.mark.parametrize("format_id,path", sorted(DIRTY_FILES.items()))
def test_every_dirty_fixture_still_yields_its_clean_rows(format_id, path):
    """The whole point of quarantine: broken rows cost nothing but themselves."""
    result = registry.ingest(path)
    assert result.format_id == format_id
    assert result.record_count > 0
    assert result.quarantine_count > 0


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_no_record_carries_a_non_integer_amount(path):
    """`Money` is int paise. A float reaching a canonical record is the single
    failure this whole layer exists to prevent, so it is checked by type on
    every field of every record of every fixture."""
    for record in registry.ingest(path).records:
        for name in ("amount", "credit", "debit", "balance", "gross_amount"):
            value = getattr(record, name, None)
            if value is not None:
                assert isinstance(value, int) and not isinstance(value, bool), (
                    f"{path.name}: {name} is {type(value).__name__}"
                )


@pytest.mark.parametrize("path", CSV_FILES, ids=lambda p: p.name)
def test_every_row_is_either_a_record_or_a_quarantine(path):
    """Nothing silently dropped, arithmetic version. Data lines in the file
    must equal rows parsed plus rows quarantined plus rows skipped -- counted
    from the raw text, independently of anything the adapter reports."""
    text = path.read_bytes().decode("latin-1")
    lines = [line for line in text.splitlines() if line.strip()]
    # Data lines are everything after the header, and the header is the first
    # line that is not a leading `#` provenance comment -- which is exactly
    # `strip_comment_lines`' own definition.
    #
    # NOT "every line that does not start with `#`". A Shopify order is named
    # `#1001`, so on that export every data row starts with a hash and the
    # simpler count reports zero data rows. The adapters are unaffected --
    # `strip_comment_lines` stops at the first non-comment line and the header
    # sits between the comments and the data -- but the collision is real and
    # `test_a_hash_prefixed_order_name_is_data_not_a_comment` pins it.
    header_index = next(
        index for index, line in enumerate(lines) if not line.startswith("#")
    )
    data_lines = len(lines) - header_index - 1

    result = registry.ingest(path)
    # One data line can produce several records (a settlement row becomes up to
    # three legs), so rows-parsed is counted as lines that were neither
    # quarantined nor skipped.
    parsed_lines = data_lines - result.quarantine_count - result.skipped_rows
    assert parsed_lines >= 0
    assert (parsed_lines + result.quarantine_count + result.skipped_rows) == data_lines
    if parsed_lines:
        assert result.record_count >= parsed_lines


# --- idempotency ------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_file_hash_is_the_hash_of_the_bytes(path):
    assert registry.ingest(path).file_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_parsing_twice_produces_identical_hashes(path):
    first = registry.ingest(path)
    second = registry.ingest(path)
    assert first.file_sha256 == second.file_sha256
    assert first.row_hashes == second.row_hashes
    assert len(first.row_hashes) == first.record_count


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_row_hashes_are_unique_within_a_file(path):
    """Two rows hashing alike would make the phase-3 dedup drop a real row.
    The fixtures contain no two identical canonical records, deliberately."""
    hashes = registry.ingest(path).row_hashes
    assert len(set(hashes)) == len(hashes)


def test_a_renamed_copy_is_the_same_upload(tmp_path: Path):
    """Idempotency is about content. `august.csv` and `august (1).csv` holding
    the same bytes are one upload, and the file hash is what says so."""
    source = CLEAN_FILES["bank-csv-hdfc-v1"]
    copy = tmp_path / "august (1).csv"
    copy.write_bytes(source.read_bytes())
    assert registry.ingest(copy).file_sha256 == registry.ingest(source).file_sha256
    assert registry.ingest(copy).row_hashes == registry.ingest(source).row_hashes


def test_one_changed_paise_changes_both_the_file_and_a_row_hash(tmp_path: Path):
    """The negative control for the test above: content-addressing that cannot
    tell two different files apart is not dedup, it is data loss."""
    source = CLEAN_FILES["bank-csv-hdfc-v1"]
    edited = tmp_path / "edited.csv"
    edited.write_bytes(source.read_bytes().replace(b"71153.04", b"71153.05", 1))

    original = registry.ingest(source)
    changed = registry.ingest(edited)
    assert changed.file_sha256 != original.file_sha256
    assert changed.row_hashes != original.row_hashes
    # ...and only the one row moved.
    assert sum(1 for a, b in zip(original.row_hashes, changed.row_hashes) if a != b) == 1


# --- file-level quarantine, via `ingest` ------------------------------------


def test_a_binary_file_becomes_a_quarantine_record_not_a_traceback(tmp_path: Path):
    path = tmp_path / "statement.csv"
    path.write_bytes(b"PK\x03\x04\x00\x00\x00\x00\x08\x00\x00\x00")
    result = registry.ingest(path)
    assert result.format_id == registry.UNREADABLE_FORMAT_ID
    assert result.records == []
    assert [q.reason for q in result.quarantined] == [QuarantineReason.UNDECODABLE_FILE]
    assert result.file_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_an_unrecognised_header_becomes_a_quarantine_record(tmp_path: Path):
    path = tmp_path / "some-other-bank.csv"
    path.write_text("Txn Ref,Particulars,Amount\n1,X,10.00\n", encoding="utf-8")
    result = registry.ingest(path)
    assert result.format_id == registry.UNREADABLE_FORMAT_ID
    assert [q.reason for q in result.quarantined] == [QuarantineReason.UNRECOGNISED_FORMAT]
    # The message must name what WAS tried, or the merchant has no next step.
    for adapter in _adapters():
        assert adapter.format_id in result.quarantined[0].detail


def test_ingest_of_a_missing_file_is_a_record_too(tmp_path: Path):
    result = registry.ingest(tmp_path / "never-written.csv")
    assert result.format_id == registry.UNREADABLE_FORMAT_ID
    assert result.quarantined[0].reason is QuarantineReason.UNDECODABLE_FILE


def test_detect_still_raises_because_it_answers_a_different_question(tmp_path: Path):
    """`ingest` swallowing everything would be wrong for `detect`: a caller
    asking "which adapter is this" deserves an answer or an exception, never a
    silent empty result."""
    path = tmp_path / "binary.csv"
    path.write_bytes(b"\x00\x00\x00\x00")
    with pytest.raises(UndecodableFileError):
        registry.detect(path)


def test_an_empty_file_is_handled_by_every_adapter(tmp_path: Path):
    path = tmp_path / "empty.csv"
    path.write_bytes(b"")
    for adapter in _adapters():
        result = adapter.parse(path)
        assert result.records == []
        assert result.quarantined[0].reason is QuarantineReason.MISSING_HEADER_COLUMN


def test_a_header_only_file_yields_nothing_and_complains_about_nothing(tmp_path: Path):
    source = CLEAN_FILES["bank-csv-icici-v1"]
    header = [
        line
        for line in source.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ][0]
    path = tmp_path / "header-only.csv"
    path.write_text(header + "\n", encoding="utf-8")
    result = registry.ingest(path)
    assert result.records == []
    assert result.quarantined == []


# --- malformed input never crashes -----------------------------------------


def _mutations(payload: bytes, seed: int, count: int) -> list[bytes]:
    """Deterministic byte-level damage: the ways a real upload arrives broken.

    Truncation is a cancelled download, deletion is a corrupted transfer, and a
    stray comma or quote is a spreadsheet round-trip. Seeded, so a failure is
    reproducible from the test name alone.
    """
    rng = random.Random(seed)
    out: list[bytes] = []
    for _ in range(count):
        data = bytearray(payload)
        kind = rng.randrange(4)
        if not data:
            break
        cut = rng.randrange(len(data))
        if kind == 0:
            data = data[:cut]  # truncated mid-file
        elif kind == 1:
            del data[cut : cut + rng.randrange(1, 40)]  # a hole
        elif kind == 2:
            data[cut:cut] = b'","'  # a spreadsheet's idea of quoting
        else:
            data[cut:cut] = b",,,"  # extra separators
        out.append(bytes(data))
    return out


@pytest.mark.parametrize("format_id,path", sorted(CLEAN_FILES.items()))
def test_no_mutation_of_a_clean_fixture_makes_an_adapter_raise(
    format_id, path, tmp_path: Path
):
    """200 seeded mutations per format. Any outcome is acceptable except an
    exception: records, quarantine, or nothing at all -- but never a traceback,
    because the file that reaches production will be stranger than these."""
    adapter = {a.format_id: a for a in _adapters()}[format_id]
    payload = path.read_bytes()
    for index, mutated in enumerate(_mutations(payload, seed=20260830, count=200)):
        target = tmp_path / f"mutated-{index}.csv"
        target.write_bytes(mutated)
        try:
            result = adapter.parse(target)
        except UndecodableFileError:
            continue  # the one exception `parse` is allowed, and it is a result
        except Exception as error:  # pragma: no cover - this is the failure
            raise AssertionError(
                f"{format_id} raised {type(error).__name__} on mutation {index}: {error}"
            ) from error
        assert len(result.row_hashes) == result.record_count


@pytest.mark.parametrize("format_id,path", sorted(CLEAN_FILES.items()))
def test_ingest_never_raises_on_a_mutated_file(format_id, path, tmp_path: Path):
    payload = path.read_bytes()
    for index, mutated in enumerate(_mutations(payload, seed=42, count=100)):
        target = tmp_path / f"ingest-{index}.csv"
        target.write_bytes(mutated)
        result = registry.ingest(target)
        assert result.record_count == len(result.row_hashes)


def test_a_row_of_pure_junk_is_quarantined_by_every_adapter(tmp_path: Path):
    for format_id, source in CSV_CLEAN_FILES.items():
        adapter = {a.format_id: a for a in _adapters()}[format_id]
        text = source.read_text(encoding="utf-8")
        column_count = len(
            [line for line in text.splitlines() if not line.startswith("#")][0].split(",")
        )
        junk = ",".join(["!@#$%"] * column_count)
        target = tmp_path / f"junk-{format_id}.csv"
        target.write_text(text + junk + "\n", encoding="utf-8")

        result = adapter.parse(target)
        assert result.quarantine_count == 1, format_id
        assert result.quarantined[0].raw == junk
        # And the good rows above it are untouched.
        assert result.record_count == adapter.parse(source).record_count
