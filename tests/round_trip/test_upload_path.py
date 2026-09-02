"""REAL FILE -> upload -> engine -> scorecard, against the fixtures run.

`test_round_trip.py` proves the adapters reproduce the generator-native metrics
when called directly. This proves the same thing through the **product**: the
files go in over HTTP as multipart uploads, are stored content-addressed,
detected by header shape, parsed, persisted as canonical records, and then
reconciled by `POST /api/runs {upload_ids}` -- and what comes out the far end
has to be the run the dataset path produces.

Two claims, and they are different claims:

* **The engine's output is identical.** Same match count, same exception count,
  the same settlements row for row, the same exceptions row for row, the same
  matches row for row. This is asserted over the API's own responses, because
  the API is what a merchant sees and a divergence introduced by the store or
  the route would be invisible to a test that stopped at `run_match`.

* **The scorecard is byte-identical.** `Metrics.model_dump_json()`, character
  for character, over the records the upload path persisted. Not "within
  tolerance" and not "the headline rate matches": there is no honest reason for
  a rate to move, because the two runs read two encodings of one dataset.

**The upload run itself reports `metrics: null`, and that is the point rather
than a gap.** Nobody knows the right answer to a merchant's own
reconciliation -- there is no `truth.json` for a file somebody exported from
their bank -- so the run reports what it found and not how well it did. The
byte-identity above is therefore established by scoring the upload run's
persisted records, read back over HTTP, against the dataset's ground truth: it
proves the records the upload path stored are exactly the records that produce
the fixtures run's scorecard, which is the strongest form of the claim
available without inventing a truth file the product would never have.

The one documented loss of the export survives here too: `BankLine.utr` has no
column in the HDFC layout (see `core/generator/export.py`), so the bank lines
read back carry `utr: null`. `test_round_trip.py` proves that costs no metric;
this file asserts it is the ONLY field that differs on the wire, so a second
loss introduced by the upload path could not hide behind the first.

**Why this file is not under `tests/api/`.** It imports `core.generator`, which
`tests/adapters/test_adapter_boundaries.py` forbids for adapter tests -- the
same reason `test_round_trip.py` lives here. One package holds the tests that
deliberately cross that line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.generator.emit import emit_dataset
from core.generator.export import (
    HDFC_FILE,
    RAZORPAY_FILE,
    SHOPIFY_FILE,
    export_dataset,
)
from core.generator.pipeline import build_dataset
from core.itc.invoice import load_invoice
from core.itc.reconcile import reconcile
from core.matcher.engine import run_match
from core.models import BankLine, Order, PSPTransaction
from scorer.score import score

#: The demo scale. 500 records carries every defect class the generator has,
#: including the ambiguity trap the export must not tidy away.
COUNT = 500
SEED = 42

#: A fixed directory name so `POST /api/runs {dataset_id}` can address it. The
#: id is the directory; `resolve_dataset` checks the three canonical files are
#: in it and nothing else.
DATASET_ID = "ds-upload-equivalence"

EXPORTED = (RAZORPAY_FILE, HDFC_FILE, SHOPIFY_FILE)

#: The one canonical field the HDFC layout has nowhere to carry. See the module
#: docstring and `core/generator/export.py`.
LOST_BANK_FIELDS = frozenset({"utr"})


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """One API over one database, one dataset, and the same dataset exported.

    Module-scoped: two 500-record runs and three uploads take a few seconds,
    and every test below reads the same two runs.
    """
    import os

    tmp = tmp_path_factory.mktemp("upload-equivalence")
    previous = {
        name: os.environ.get(name)
        for name in ("RECON_DB_PATH", "RECON_DATASETS_DIR", "RECON_UPLOADS_DIR")
    }
    os.environ["RECON_DB_PATH"] = str(tmp / "recon.db")
    os.environ["RECON_DATASETS_DIR"] = str(tmp / "datasets")
    os.environ["RECON_UPLOADS_DIR"] = str(tmp / "uploads")

    try:
        # The dataset is written where the API will look for it, and exported
        # into the same directory. Generation is deterministic on the seed, so
        # the canonical CSVs and the real-format files describe one dataset.
        directory = tmp / "datasets" / DATASET_ID
        batches, injections = build_dataset(seed=SEED, count=COUNT)
        emit_dataset(batches, injections, out_dir=directory, seed=SEED)
        export_dataset(batches, directory)

        from api.main import create_app

        with TestClient(create_app()) as client:
            dataset_run = _run_to_completion(
                client, {"dataset_id": DATASET_ID, "use_llm": False}
            )
            uploads = [_upload(client, directory / name) for name in EXPORTED]
            upload_run = _run_to_completion(
                client,
                {"upload_ids": [u["upload_id"] for u in uploads], "use_llm": False},
            )
            yield {
                "client": client,
                "directory": directory,
                "uploads": uploads,
                "dataset_run": dataset_run,
                "upload_run": upload_run,
            }
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _upload(client: TestClient, path: Path) -> dict:
    with open(path, "rb") as handle:
        response = client.post(
            "/api/uploads", files={"file": (path.name, handle, "text/csv")}
        )
    assert response.status_code == 200, response.text
    return response.json()


def _run_to_completion(client: TestClient, body: dict) -> str:
    created = client.post("/api/runs", json=body)
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    for _ in range(600):
        state = client.get(f"/api/runs/{run_id}/status").json()["state"]
        if state in ("completed", "failed"):
            break
    assert state == "completed", client.get(f"/api/runs/{run_id}/status").json()
    return run_id


def _pages(client: TestClient, path: str) -> list[dict]:
    """Every item of a paginated listing, walked to the end."""
    items: list[dict] = []
    page = 1
    while True:
        joiner = "&" if "?" in path else "?"
        body = client.get(f"{path}{joiner}page={page}&size=200").json()
        items += body["items"]
        if len(items) >= body["total"]:
            return items
        page += 1
        assert page < 200, path


# --- the uploads themselves ---------------------------------------------------


def test_each_exported_file_is_detected_from_its_bytes_alone(stack):
    """Nothing told the API which format it was sending: the filename is
    recorded and never consulted, and `sniff` has no parameter one could reach
    it through."""
    detected = {u["filename"]: u["format_id"] for u in stack["uploads"]}
    assert detected == {
        RAZORPAY_FILE: "razorpay-settlement-v2",
        HDFC_FILE: "bank-csv-hdfc-v1",
        SHOPIFY_FILE: "orders-csv-shopify-v1",
    }


def test_a_clean_export_quarantines_nothing(stack):
    """A clean export that quietly lost rows could still score identically if
    the rows it lost were ones no metric counts."""
    for upload in stack["uploads"]:
        assert upload["quarantine_count"] == 0, upload["filename"]
        assert upload["skipped_rows"] == 0, upload["filename"]
        assert upload["state"] == "ingested"


def test_the_uploads_cover_the_three_sources_a_run_needs(stack):
    by_source = {
        "order": sum(u["order_count"] for u in stack["uploads"]),
        "psp_txn": sum(u["psp_txn_count"] for u in stack["uploads"]),
        "bank_line": sum(u["bank_line_count"] for u in stack["uploads"]),
    }
    assert all(count > 0 for count in by_source.values()), by_source
    assert by_source["order"] == COUNT


# --- the claim: the two runs are the same run ---------------------------------


def test_the_two_runs_agree_on_what_the_engine_found(stack):
    client = stack["client"]
    mine = client.get(f"/api/runs/{stack['upload_run']}").json()
    theirs = client.get(f"/api/runs/{stack['dataset_run']}").json()

    assert mine["match_count"] == theirs["match_count"]
    assert mine["exception_count"] == theirs["exception_count"]
    assert mine["record_count"] == theirs["record_count"] == COUNT


def test_every_settlement_row_is_identical(stack):
    """The netting arithmetic of every batch, matched or not, row for row.

    `Settlement` carries six money fields, the payment-leg count, the bank line
    and the tier -- so this is the whole of what the engine decided about every
    batch, compared without a tolerance.
    """
    client = stack["client"]
    mine = _pages(client, f"/api/runs/{stack['upload_run']}/settlements")
    theirs = _pages(client, f"/api/runs/{stack['dataset_run']}/settlements")

    assert mine and mine == theirs


def test_every_match_is_identical_including_its_evidence(stack):
    """Not only the tier walk: the evidence lines too. A run that reached the
    same tiers by different reasoning would pass a count comparison."""
    client = stack["client"]
    mine = _pages(client, f"/api/runs/{stack['upload_run']}/matches")
    theirs = _pages(client, f"/api/runs/{stack['dataset_run']}/matches")

    assert mine and mine == theirs


def test_every_exception_is_identical_apart_from_the_field_hdfc_cannot_carry(
    stack,
):
    """Exceptions carry their subject record inline, so this is also a
    record-level comparison -- and the ONLY difference permitted is the `utr`
    the HDFC layout has no column for."""
    client = stack["client"]
    mine = _pages(client, f"/api/runs/{stack['upload_run']}/exceptions")
    theirs = _pages(client, f"/api/runs/{stack['dataset_run']}/exceptions")

    assert len(mine) == len(theirs) and mine
    for a, b in zip(mine, theirs, strict=True):
        assert a["exception_id"] == b["exception_id"]
        for field in LOST_BANK_FIELDS:
            a["subject"].pop(field, None)
            b["subject"].pop(field, None)
        assert a == b, a["exception_id"]


@pytest.mark.parametrize("source", ["order", "psp_txn", "bank_line"])
def test_every_ingested_record_survives_the_upload_path(stack, source):
    client = stack["client"]
    path = "/api/runs/{run}/records?source=" + source
    mine = _pages(client, path.format(run=stack["upload_run"]))
    theirs = _pages(client, path.format(run=stack["dataset_run"]))

    assert len(mine) == len(theirs) and mine
    for a, b in zip(mine, theirs, strict=True):
        if source == "bank_line":
            for field in LOST_BANK_FIELDS:
                a.pop(field, None)
                b.pop(field, None)
        assert a == b


def test_the_lost_field_is_the_only_one_and_it_is_lost_loudly(stack):
    """`utr` reads back null on every bank line, and the generator did write
    UTRs -- so this is a real representational loss and not a dataset that
    happened to have none. Asserted so a second loss could not hide behind it.
    """
    client = stack["client"]
    mine = _pages(
        client, f"/api/runs/{stack['upload_run']}/records?source=bank_line"
    )
    theirs = _pages(
        client, f"/api/runs/{stack['dataset_run']}/records?source=bank_line"
    )
    assert all(line["utr"] is None for line in mine)
    assert any(line["utr"] for line in theirs)


# --- the scorecard ------------------------------------------------------------


def test_the_scorecard_is_byte_identical(stack):
    """`Metrics.model_dump_json()`, character for character.

    The dataset run's metrics come off the API. The upload run's are computed
    here, over the records the upload path PERSISTED and served back -- because
    a run over a merchant's files has no ground truth and correctly reports
    none. What is being proved is that the records the upload path stored are
    the records that produce that scorecard.
    """
    client = stack["client"]
    theirs = client.get(f"/api/runs/{stack['dataset_run']}").json()["metrics"]
    assert theirs is not None

    orders = [
        Order.model_validate(row)
        for row in _pages(
            client, f"/api/runs/{stack['upload_run']}/records?source=order"
        )
    ]
    psp = [
        PSPTransaction.model_validate(row)
        for row in _pages(
            client, f"/api/runs/{stack['upload_run']}/records?source=psp_txn"
        )
    ]
    bank = [
        BankLine.model_validate(row)
        for row in _pages(
            client, f"/api/runs/{stack['upload_run']}/records?source=bank_line"
        )
    ]

    result = run_match(orders, psp, bank)
    # The ITC report is reconciled against the PSP's tax invoice, which is a
    # document nothing in this build has an adapter for -- a merchant would
    # upload it separately, and the upload run therefore has none. It is
    # assembled here exactly as `api/jobs.py` assembles it on the dataset path,
    # over the records the UPLOAD path stored: the three figures are then a
    # function of those records and the invoice, and comparing them is part of
    # the claim rather than an exemption from it.
    invoices = load_invoice(stack["directory"])
    assert invoices is not None
    itc = reconcile(result, bank, invoices)

    mine = score(
        result,
        stack["directory"] / "truth.json",
        itc_substantiated_paise=itc.substantiated_paise,
        itc_at_risk_paise=itc.at_risk_paise,
        itc_variance_paise=itc.variance_paise,
    )
    # `elapsed_seconds` is not passed, so throughput is 0.0 on both sides: the
    # dataset run's own metrics carry the API's measured throughput, which is a
    # wall clock and cannot be equal across two runs. Every other field must be.
    theirs = {**theirs, "throughput_records_per_sec": 0.0}
    assert json.loads(mine.model_dump_json()) == theirs


def test_a_run_over_uploads_reports_no_scorecard_and_says_why_with_its_seed(
    stack,
):
    """The honest state, asserted so nothing later "helpfully" fills it in.

    A merchant's files have no ground truth, so `metrics` is null; nothing
    generated the records, so `seed` is -1 rather than a number that reads as
    an experiment. Both are what the console renders as "run from uploaded
    files, no scorecard" instead of a page of zeroes.
    """
    client = stack["client"]
    summary = client.get(f"/api/runs/{stack['upload_run']}").json()
    assert summary["state"] == "completed"
    assert summary["metrics"] is None
    assert summary["seed"] == -1

    # And the dataset path is untouched: it still scores.
    assert (
        client.get(f"/api/runs/{stack['dataset_run']}").json()["metrics"]
        is not None
    )


# --- the control --------------------------------------------------------------


def test_a_damaged_upload_would_be_caught(stack):
    """If the runs agreed no matter what was uploaded, every equality above
    would be worth nothing. One bank line moved by one rupee, uploaded as a
    fourth file, and the resulting run must differ.

    The comparison is over the settlements and the matches rather than over the
    two counts: a credit that moves by a rupee stops one batch closing at its
    tier, which the listings show and which the headline counts can absorb.
    That is the same reason the round-trip test's control compares metrics
    rather than totals.
    """
    client = stack["client"]
    rows = (
        (stack["directory"] / HDFC_FILE).read_text(encoding="utf-8").splitlines()
    )
    header, first, rest = rows[0], rows[1].split(","), rows[2:]
    first[5] = f"{float(first[5]) + 1:.2f}"  # column 5 is `Deposit Amt.`
    damaged = ("\n".join([header, ",".join(first), *rest]) + "\n").encode("utf-8")

    response = client.post(
        "/api/uploads", files={"file": ("damaged.csv", damaged, "text/csv")}
    )
    assert response.status_code == 200, response.text
    damaged_id = response.json()["upload_id"]
    assert damaged_id != next(
        u["upload_id"] for u in stack["uploads"] if u["filename"] == HDFC_FILE
    )

    others = [
        u["upload_id"] for u in stack["uploads"] if u["filename"] != HDFC_FILE
    ]
    run_id = _run_to_completion(
        client, {"upload_ids": [*others, damaged_id], "use_llm": False}
    )
    assert _pages(client, f"/api/runs/{run_id}/settlements") != _pages(
        client, f"/api/runs/{stack['dataset_run']}/settlements"
    ) or _pages(client, f"/api/runs/{run_id}/matches") != _pages(
        client, f"/api/runs/{stack['dataset_run']}/matches"
    ), (
        "a bank credit moved by one rupee and the run came out identical, so "
        "the equalities above are comparing something that does not depend on "
        "what was uploaded"
    )
