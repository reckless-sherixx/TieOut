"""The round-trip proof: the adapters in the loop, with ground truth still in it.

This is the only measured accuracy number this project has over real file
formats, and the argument it rests on is one sentence: **generate a labelled
dataset, write it in the real formats, read it back through the adapter
registry, and the metrics must be identical to the generator-native run's**.
Ground truth never leaves the loop; the adapters enter it.

Identical means identical. Not "within tolerance", not "the headline rate
matches" -- `Metrics.model_dump_json()` byte for byte, tier walk included, on
both scale points. There is no honest reason for a rate to move: the two runs
read the same settlements out of two encodings of one dataset, so a difference
is a defect in an adapter or in the exporter and the assertion exists to find
it, never to be relaxed around it.

Four things are asserted beyond the metrics, because each is a way the
comparison could be true for the wrong reason:

* **detection is unaided.** `registry.ingest` is handed a path and nothing
  else; `sniff` sees bytes and has no parameter a filename could reach. A test
  that told the adapter which format to expect would prove nothing about the
  upload path.
* **zero quarantine.** A clean export that quietly lost rows could still score
  identically if the rows it lost were ones no metric counts.
* **record-level equality**, not only metric equality. Every canonical record
  read back is compared field by field against the record the generator wrote,
  with exactly one documented exception -- `BankLine.utr`, which the HDFC layout
  has no column left to carry. Metrics can agree while records differ; records
  agreeing is the stronger claim and is the one made here.
* **the trap and the defects survive.** The obfuscated-reference narrations,
  the byte-identical trap narrations and the doubled spaces are the dataset's
  difficulty. An export that tidied them would produce a higher match rate on
  an easier problem, and the metrics would then differ -- which is exactly why
  the equality above is worth asserting.

**Why this module is not under `tests/adapters/`.**
`tests/adapters/test_adapter_boundaries.py` asserts that no adapter test
imports `core.generator` or reads a generated dataset, and that boundary is
what makes the hand-written fixtures evidence rather than a conversation
between the generator and itself. This test necessarily does both. Exempting it
there would have punched a hole in the check for every future adapter test; a
separate package keeps the boundary absolute and puts the one test that crosses
it somewhere a reader can see that it does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.adapters import registry
from core.generator.emit import emit_dataset
from core.generator.export import (
    HDFC_FILE,
    RAZORPAY_FILE,
    SHOPIFY_FILE,
    export_dataset,
)
from core.generator.pipeline import build_dataset
from core.ingest import reader
from core.matcher.engine import run_match
from core.models import BankLine, Order, PSPTransaction
from scorer.score import score

#: The two scale points the brief names. 500 is the demo dataset; 50 is the
#: smallest run that still carries every defect class, so a defect that only
#: survives the export at scale would show up here.
SCALES = (500, 50)

#: The one canonical field the HDFC layout cannot carry. See
#: `core/generator/export.py`: an HDFC statement has a single reference column,
#: `Chq./Ref.No.`, ground truth is keyed on `line_id` so the id must have it,
#: and the narration is written verbatim so a UTR cannot be appended there
#: either -- doing so would give the ambiguity trap's two lines distinguishable
#: narrations and destroy the trap.
LOST_BANK_FIELDS = frozenset({"utr"})

#: format_id each exported file must be detected as, by header shape alone.
EXPECTED_FORMATS = {
    RAZORPAY_FILE: "razorpay-settlement-v2",
    HDFC_FILE: "bank-csv-hdfc-v1",
    SHOPIFY_FILE: "orders-csv-shopify-v1",
}


def _dataset(tmp_path: Path, count: int) -> Path:
    """Generate, emit and export one dataset into its own directory."""
    out = tmp_path / f"seed42-{count}"
    batches, injections = build_dataset(seed=42, count=count)
    emit_dataset(batches, injections, out_dir=out, seed=42)
    export_dataset(batches, out)
    return out


def _native(out: Path):
    """The generator-native run: the canonical CSVs, through the strict reader."""
    return run_match(
        reader.read_orders(out / "orders.csv"),
        reader.read_psp(out / "psp.csv"),
        reader.read_bank(out / "bank.csv"),
    )


def _ingested(out: Path) -> dict[str, object]:
    """Every exported file through `registry.ingest`, with nothing else told."""
    return {name: registry.ingest(out / name) for name in EXPECTED_FORMATS}


def _partition(results) -> tuple[list[Order], list[PSPTransaction], list[BankLine]]:
    orders: list[Order] = []
    psp: list[PSPTransaction] = []
    bank: list[BankLine] = []
    for result in results.values():
        for record in result.records:
            if isinstance(record, Order):
                orders.append(record)
            elif isinstance(record, PSPTransaction):
                psp.append(record)
            else:
                bank.append(record)
    return orders, psp, bank


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict[int, dict]:
    """Both scale points, generated once: the suite has a time budget."""
    root = tmp_path_factory.mktemp("round-trip")
    built = {}
    for count in SCALES:
        out = _dataset(root, count)
        results = _ingested(out)
        orders, psp, bank = _partition(results)
        built[count] = {
            "out": out,
            "results": results,
            "native": score(_native(out), out / "truth.json"),
            "round_trip": score(run_match(orders, psp, bank), out / "truth.json"),
            "records": (orders, psp, bank),
        }
    return built


# --- the claim --------------------------------------------------------------


@pytest.mark.parametrize("count", SCALES)
def test_the_round_trip_reproduces_every_metric_byte_for_byte(runs, count):
    native = runs[count]["native"]
    round_trip = runs[count]["round_trip"]

    assert round_trip.model_dump_json() == native.model_dump_json()


@pytest.mark.parametrize("count", SCALES)
def test_the_tier_walk_is_identical_too(runs, count):
    """Spelled out separately from the JSON comparison above, because the tier
    breakdown is the part a reader wants to see named: an export that made the
    problem easier would move matches from T3 up to T0 while every rate stayed
    put."""
    assert runs[count]["round_trip"].tier_counts == runs[count]["native"].tier_counts
    assert sum(runs[count]["round_trip"].tier_counts.values()) > 0


@pytest.mark.parametrize("count", SCALES)
def test_nothing_is_quarantined_on_a_clean_export(runs, count):
    for name, result in runs[count]["results"].items():
        assert result.quarantine_count == 0, (name, result.quarantined[:3])
        assert result.skipped_rows == 0, name


@pytest.mark.parametrize("count", SCALES)
def test_the_registry_picks_each_adapter_from_the_bytes_alone(runs, count):
    for name, expected in EXPECTED_FORMATS.items():
        assert runs[count]["results"][name].format_id == expected


def test_detection_never_sees_the_filename(runs, tmp_path):
    """Renaming an export must not change which adapter reads it. `sniff` takes
    bytes and has no parameter through which a name could arrive; this asserts
    the property rather than the signature."""
    out = runs[500]["out"]
    disguised = tmp_path / "hdfc_aug.csv"
    disguised.write_bytes((out / RAZORPAY_FILE).read_bytes())

    assert registry.detect(disguised).format_id == "razorpay-settlement-v2"


# --- the stronger claim: the records themselves -----------------------------


@pytest.mark.parametrize("count", SCALES)
def test_every_order_survives_the_round_trip_field_for_field(runs, count):
    out = runs[count]["out"]
    written = {order.order_id: order for order in reader.read_orders(out / "orders.csv")}
    read_back = {order.order_id: order for order in runs[count]["records"][0]}

    assert set(read_back) == set(written)
    for order_id, order in read_back.items():
        assert order.model_dump() == written[order_id].model_dump(), order_id


@pytest.mark.parametrize("count", SCALES)
def test_every_psp_leg_survives_the_round_trip_field_for_field(runs, count):
    out = runs[count]["out"]
    written = {txn.txn_id: txn for txn in reader.read_psp(out / "psp.csv")}
    read_back = {txn.txn_id: txn for txn in runs[count]["records"][1]}

    assert set(read_back) == set(written)
    for txn_id, txn in read_back.items():
        assert txn.model_dump() == written[txn_id].model_dump(), txn_id


@pytest.mark.parametrize("count", SCALES)
def test_every_bank_line_survives_except_the_one_field_hdfc_cannot_carry(runs, count):
    out = runs[count]["out"]
    written = {line.line_id: line for line in reader.read_bank(out / "bank.csv")}
    read_back = {line.line_id: line for line in runs[count]["records"][2]}

    assert set(read_back) == set(written)
    for line_id, line in read_back.items():
        mine = line.model_dump()
        theirs = written[line_id].model_dump()
        for field in LOST_BANK_FIELDS:
            mine.pop(field)
            theirs.pop(field)
        assert mine == theirs, line_id


def test_the_lost_field_is_lost_loudly_and_costs_no_metric(runs):
    """`utr` reads back as `None` on every line, and the metric equality above
    is what proves that costs nothing. Asserted here so the loss is a recorded
    fact rather than something a future reader has to notice."""
    lines = runs[500]["records"][2]
    written = reader.read_bank(runs[500]["out"] / "bank.csv")

    assert all(line.utr is None for line in lines)
    # ... and the generator did write UTRs, so this is a real loss and not a
    # dataset that happened to have none.
    assert any(line.utr for line in written)


# --- the defects have to survive, or the comparison is between two problems --


@pytest.mark.parametrize("count", SCALES)
def test_every_narration_survives_byte_for_byte(runs, count):
    """Doubled spaces, obfuscated settlement references and the trap's
    byte-identical narration. The export writes the narration column verbatim
    and this is the assertion that keeps it that way."""
    out = runs[count]["out"]
    written = {line.line_id: line.narration for line in reader.read_bank(out / "bank.csv")}
    for line in runs[count]["records"][2]:
        assert line.narration == written[line.line_id], line.line_id


def test_the_ambiguity_trap_is_still_ambiguous_after_the_export(runs):
    """The trap's two lines must remain indistinguishable: same narration, same
    date, same credit, no UTR. If the export gave either line anything the other
    lacks, `trap_capture_rate` would stop measuring anything."""
    out = runs[500]["out"]
    truth = json.loads((out / "truth.json").read_text(encoding="utf-8"))
    # One entry per trap INSTANCE. A run at this scale carries several, and
    # comparing across two different pairs would compare two lines that were
    # never meant to be alike.
    pairs = [
        defect["affected_ids"]
        for defect in truth["injected_defects"]
        if defect["defect_type"] == "ambiguous_unresolvable"
    ]
    assert pairs

    lines = {line.line_id: line for line in runs[500]["records"][2]}
    for pair in pairs:
        first, second = (lines[line_id] for line_id in pair)
        assert (second.narration, second.txn_date, second.credit) == (
            first.narration,
            first.txn_date,
            first.credit,
        )
        assert second.utr is first.utr is None


def test_the_obfuscated_reference_narrations_reach_the_engine_unparsed(runs):
    """The defect written for the analyst layer. It only exists as a defect if
    the deterministic canonicaliser still cannot read the reference out of the
    exported file -- an export that normalised the narration would delete the
    defect and raise the match rate."""
    from core.canonicalize.narration import canonicalize

    out = runs[500]["out"]
    truth = json.loads((out / "truth.json").read_text(encoding="utf-8"))
    obfuscated = {
        line_id
        for defect in truth["injected_defects"]
        if defect["defect_type"] == "obfuscated_settlement_ref"
        for line_id in defect["affected_ids"]
        if line_id.startswith("BL-")
    }
    assert obfuscated

    lines = {line.line_id: line for line in runs[500]["records"][2]}
    for line_id in obfuscated:
        assert canonicalize(lines[line_id].narration).settlement_id is None


# --- what the round trip is worth against a null hypothesis ------------------


def test_a_damaged_export_would_be_caught(runs, tmp_path):
    """The control. If the metrics agreed no matter what the export wrote, the
    equality above would be worth nothing -- so this shifts one bank line's
    amount by one rupee and asserts the comparison notices."""
    out = runs[50]["out"]
    damaged = tmp_path / "damaged.csv"
    rows = (out / HDFC_FILE).read_text(encoding="utf-8").splitlines()
    header, first, rest = rows[0], rows[1].split(","), rows[2:]
    # column 5 is `Deposit Amt.`
    first[5] = f"{float(first[5]) + 1:.2f}"
    damaged.write_text(
        "\n".join([header, ",".join(first), *rest]) + "\n", encoding="utf-8"
    )

    results = dict(runs[50]["results"])
    results[HDFC_FILE] = registry.ingest(damaged)
    orders, psp, bank = _partition(results)
    metrics = score(run_match(orders, psp, bank), out / "truth.json")

    assert metrics.model_dump_json() != runs[50]["native"].model_dump_json()


# --- the quarantine path: damage that must not spread -----------------------
#
# The dirty fixtures prove the adapters quarantine correctly. What they cannot
# prove is containment -- that damage in a file costs the rows it is on and not
# the run. That is the claim a merchant actually cares about, and it is what
# `--export-as razorpay --dirty` measures: the same dataset, the same ground
# truth, four pieces of file-level mess, and the metrics must not move.
#
# **The second half is true by construction and the construction is stated
# rather than assumed.** Every injection is either a file-level encoding change
# or a row appended past the end of the data. There is no spare row in this
# dataset -- every order, every leg and every bank line is named in truth.json,
# so damaging one SHOULD move the metrics and the clean round trip's control
# test shows that it does. Appending is the only way to ask the containment
# question on its own.


@pytest.fixture(scope="module")
def dirty_run(tmp_path_factory) -> dict:
    from core.generator.export import dirty_export

    root = tmp_path_factory.mktemp("dirty-round-trip")
    out = _dataset(root, 500)
    clean = _ingested(out)
    clean_metrics = score(
        run_match(*_partition(clean)), out / "truth.json"
    )

    injections = dirty_export(out)
    results = _ingested(out)
    return {
        "out": out,
        "injections": injections,
        "results": results,
        "clean_metrics": clean_metrics,
        "metrics": score(run_match(*_partition(results)), out / "truth.json"),
    }


def test_quarantine_catches_exactly_the_injected_rows_and_nothing_else(dirty_run):
    expected: dict[str, list[str]] = {name: [] for name in EXPECTED_FORMATS}
    for injection in dirty_run["injections"]:
        if injection.reason is not None:
            expected[injection.file].append(injection.reason)

    actual = {
        name: [row.reason.value for row in result.quarantined]
        for name, result in dirty_run["results"].items()
    }
    assert actual == expected


def test_the_damaged_rows_are_the_rows_at_the_end_of_each_file(dirty_run):
    """Containment, stated as a fact about line numbers: nothing before the
    appended block is touched, so no record the run needs is in quarantine."""
    appended = {name: 0 for name in EXPECTED_FORMATS}
    for injection in dirty_run["injections"]:
        if injection.reason is not None:
            appended[injection.file] += 1

    for name, result in dirty_run["results"].items():
        lines = len(
            (dirty_run["out"] / name).read_text(encoding="latin-1").splitlines()
        )
        for row in result.quarantined:
            assert row.row_number > lines - appended[name], (name, row)


def test_a_damaged_upload_produces_the_same_metrics_as_a_clean_one(dirty_run):
    """The containment claim itself. Byte for byte, tier walk included."""
    assert (
        dirty_run["metrics"].model_dump_json()
        == dirty_run["clean_metrics"].model_dump_json()
    )


def test_the_bom_costs_nothing_but_the_hash(dirty_run):
    """A byte-order mark is a fact about the bytes, not a defect in a row. It
    must change the file hash -- the identity of an upload is its bytes -- and
    change nothing else, row numbers included."""
    result = dirty_run["results"][SHOPIFY_FILE]
    assert result.encoding == "utf-8-sig"
    assert result.quarantine_count == 0
    assert len(result.records) == 500


def test_a_latin_1_narration_does_not_stop_the_statement_being_read(dirty_run):
    """The whole file falls back to latin-1 because of one row, and every other
    line still parses -- an encoding fallback that dropped the file would cost a
    merchant their entire statement over one accented vendor name."""
    result = dirty_run["results"][HDFC_FILE]
    assert result.encoding == "latin-1"
    assert result.records
    assert len(result.quarantined) == 2


def test_nothing_is_dropped_silently_even_when_damaged(dirty_run):
    """`data rows == records + quarantined + skipped`, per file. A quarantine
    path that lost a row instead of recording it would satisfy every assertion
    above and still be the failure this layer exists to prevent."""
    for name, result in dirty_run["results"].items():
        text = (dirty_run["out"] / name).read_text(encoding="latin-1")
        body = text.lstrip("﻿").splitlines()
        data_rows = len(body) - 1  # the header
        assert (
            data_rows
            == len(result.records) + result.quarantine_count + result.skipped_rows
        ), name
