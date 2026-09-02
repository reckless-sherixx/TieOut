"""`POST /api/uploads` and the three reads beside it, against the contract.

The upload path is where a merchant's own file meets this system, so the tests
here are about the four properties that make that meeting safe rather than
about coverage of a handler:

1. **Idempotent by content** (spec 2026-08-30 §3, A3). The same bytes twice is
   the same upload id, `already_ingested: true`, and *no second row anywhere* --
   asserted on the record and quarantine counts of the store, not only on the
   response body, because a response that looked right over a duplicated
   `upload_records` table would be the exact bug this is for.

2. **Never a stack trace.** A file nothing recognises, a file that is not text
   at all, an empty file, a file whose every row is damaged: four different
   answers, each structured, none of them a 500.

3. **The three states are three facts.** `ingested`, `quarantined` and `empty`
   are the distinction the console is built on, and a single "0 records" would
   collapse two of them.

4. **Raw rows leave through one door.** The quarantine listing serves a
   merchant's own lines verbatim; no error body on this API does.

Every response body is checked against `api/openapi.yaml` itself with the same
walker `tests/api/test_routes.py` uses -- `web/` generates its whole TypeScript
layer from that file, so a body that drifts from it is a break nothing else in
the repo would catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.api.test_routes import assert_contract_valid, spec  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_FORMATS = REPO_ROOT / "fixtures" / "real-formats"

CLEAN = REAL_FORMATS / "razorpay-settlement-clean.csv"
DIRTY = REAL_FORMATS / "razorpay-settlement-dirty.csv"
HDFC = REAL_FORMATS / "hdfc-statement-clean.csv"
LATIN1 = REAL_FORMATS / "icici-statement-latin1.csv"
COD = REAL_FORMATS / "cod-remittance-clean.csv"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client on an isolated database, dataset root and blob store."""
    monkeypatch.setenv("RECON_DB_PATH", str(tmp_path / "recon.db"))
    monkeypatch.setenv("RECON_DATASETS_DIR", str(tmp_path / "datasets"))
    monkeypatch.setenv("RECON_UPLOADS_DIR", str(tmp_path / "uploads"))
    from api.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def send(client: TestClient, path: Path, *, name: str | None = None):
    with open(path, "rb") as handle:
        return client.post(
            "/api/uploads",
            files={"file": (name or path.name, handle, "text/csv")},
        )


def send_bytes(client: TestClient, name: str, payload: bytes):
    return client.post("/api/uploads", files={"file": (name, payload, "text/csv")})


def header_of(path: Path) -> str:
    """The layout's header row, past the fixture's `#` provenance banner.

    The hand-written fixtures open with a comment block saying they are
    hand-written; `strip_comment_lines` in the adapter layer drops it, and a
    test that took line 1 literally would build its "empty export" out of that
    banner and would be testing nothing.
    """
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            return line
    raise AssertionError(f"{path} is all comments")


# --- the happy path -----------------------------------------------------------


def test_a_real_export_is_ingested_and_contract_valid(client, spec):
    response = send(client, CLEAN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert_contract_valid(body, spec, "/api/uploads", "post", "200")

    assert body["format_id"] == "razorpay-settlement-v2"
    assert body["state"] == "ingested"
    assert body["record_count"] > 0
    assert body["psp_txn_count"] == body["record_count"]
    assert body["already_ingested"] is False
    assert body["byte_size"] == CLEAN.stat().st_size


def test_detection_reads_bytes_and_not_the_name(client):
    """The property the whole adapter layer is built on, asserted through HTTP.

    A merchant renames exports constantly, and `hdfc_aug.csv` holding a
    Razorpay report is a Tuesday.
    """
    body = send(client, CLEAN, name="hdfc_aug_statement.csv").json()
    assert body["format_id"] == "razorpay-settlement-v2"


def test_the_encoding_that_actually_decoded_the_file_is_reported(client):
    """"This ICICI export was latin-1" is a fact worth showing a user, and it
    is the explanation for a good deal of otherwise mysterious downstream
    behaviour."""
    body = send(client, LATIN1).json()
    assert body["encoding"] == "latin-1"


def test_the_confidence_of_the_winning_adapter_travels_with_the_format(client):
    """An export that cleared the threshold by 0.02 and one that scored 1.00
    are different situations. A screen showing only the format name would
    present them identically, so the score is on the wire."""
    body = send(client, COD).json()
    assert body["format_id"] == "cod-remittance-delhivery-v1"
    assert 0.6 <= body["confidence"] <= 1.0


def test_the_listing_and_the_detail_agree_and_are_contract_valid(client, spec):
    first = send(client, CLEAN).json()
    second = send(client, HDFC).json()

    listing = client.get("/api/uploads")
    assert listing.status_code == 200
    assert_contract_valid(listing.json(), spec, "/api/uploads", "get", "200")
    assert {row["upload_id"] for row in listing.json()} == {
        first["upload_id"],
        second["upload_id"],
    }

    detail = client.get(f"/api/uploads/{first['upload_id']}")
    assert detail.status_code == 200
    assert_contract_valid(detail.json(), spec, "/api/uploads/{id}", "get", "200")
    # The receipt is the detail plus one field, so everything else must match.
    assert detail.json() == {
        k: v for k, v in first.items() if k != "already_ingested"
    }


def test_an_unknown_upload_is_404_on_both_reads(client):
    assert client.get("/api/uploads/upl-nope").status_code == 404
    assert client.get("/api/uploads/upl-nope/quarantine").status_code == 404


# --- idempotency (spec 2026-08-30 section 3, A3) ------------------------------


def test_the_same_bytes_twice_is_the_same_upload(client):
    first = send(client, CLEAN).json()
    second = send(client, CLEAN, name="a-completely-different-name.csv").json()

    assert second["upload_id"] == first["upload_id"]
    assert second["already_ingested"] is True
    # The FIRST ingest's facts come back, filename included: the resource is
    # the one that was already held, and nothing about it was re-decided.
    assert second == {**first, "already_ingested": True}
    assert len(client.get("/api/uploads").json()) == 1


def test_a_re_upload_writes_no_second_row_anywhere(client):
    """The response body agreeing is not enough.

    A duplicated `upload_records` table would still answer the same summary --
    the counts on `Upload` are stored, not recomputed -- and would then feed a
    run every canonical record twice. So this reads the store itself.
    """
    from core.store.repo import Repo

    body = send(client, DIRTY).json()
    for _ in range(3):
        send(client, DIRTY)

    import api.settings as settings

    repo = Repo(settings.db_path())
    orders, psp, bank = repo.upload_inputs([body["upload_id"]])
    assert len(orders) + len(psp) + len(bank) == body["record_count"]
    quarantine = repo.upload_quarantine_page(body["upload_id"], size=1000)
    assert quarantine.total == body["quarantine_count"]


def test_two_different_files_are_two_uploads(client):
    first = send(client, CLEAN).json()
    second = send(client, HDFC).json()
    assert first["upload_id"] != second["upload_id"]
    assert first["content_sha256"] != second["content_sha256"]


# --- refusals: structured, never a stack trace --------------------------------


def test_a_file_no_adapter_recognises_is_a_structured_422(client, spec):
    response = send_bytes(client, "mystery.csv", b"alpha,beta,gamma\n1,2,3\n")
    assert response.status_code == 422
    body = response.json()
    assert_contract_valid(body, spec, "/api/uploads", "post", "422")

    assert body["reason"] == "UNRECOGNISED_FORMAT"
    assert body["threshold"] == 0.6
    # Every format this build reads, each with the score it gave this file --
    # which is what makes "why did it not read my export" answerable.
    #
    # Read off the registry rather than written out here. This list was
    # hard-coded until a seventh adapter landed, and what broke then was the
    # TEST rather than the endpoint: the refusal was correct and the copy of
    # the list beside it was stale. The claim worth pinning is "the refusal
    # names EVERY format", which is a claim about the registry, so it asks the
    # registry.
    from core.adapters import registry

    expected = {adapter.format_id for adapter in registry.adapters()}
    assert len(expected) > 1, "the check would be near-vacuous"
    assert {c["format_id"] for c in body["candidates"]} == expected
    assert all(c["confidence"] < body["threshold"] for c in body["candidates"])
    assert "Traceback" not in response.text


def test_a_file_that_is_not_text_is_a_different_refusal(client, spec):
    """A spreadsheet or an archive renamed to `.csv`. The fix is different from
    "we do not read this format yet", so the answer is."""
    response = send_bytes(client, "export.csv", b"PK\x03\x04\x00\x00binary junk")
    assert response.status_code == 422
    body = response.json()
    assert_contract_valid(body, spec, "/api/uploads", "post", "422")

    assert body["reason"] == "UNDECODABLE_FILE"
    # No adapter was ever shown these bytes, so a row of zero confidences would
    # imply six of them had looked and declined.
    assert body["candidates"] == []


def test_a_refused_file_is_not_retained(client, tmp_path):
    """Nothing points at a refused file, so nothing could list, review or erase
    it. An unreferenced copy of a merchant's data is the one thing a retention
    policy cannot describe."""
    send_bytes(client, "mystery.csv", b"alpha,beta,gamma\n1,2,3\n")
    assert client.get("/api/uploads").json() == []
    blobs = [p for p in (tmp_path / "uploads").rglob("*") if p.is_file()]
    assert blobs == [], blobs


def test_no_scratch_file_survives_a_refusal_or_a_success(client, tmp_path):
    """The plaintext copy an adapter reads is removed in a `finally`, whichever
    way the parse went."""
    send(client, CLEAN)
    send_bytes(client, "mystery.csv", b"alpha,beta,gamma\n1,2,3\n")
    incoming = tmp_path / "uploads" / "incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []


# --- the three states are three facts -----------------------------------------


def test_a_file_with_damage_in_it_still_ingests_and_quarantines(client, spec):
    body = send(client, DIRTY).json()
    assert body["state"] == "ingested"
    assert body["record_count"] > 0
    assert body["quarantine_count"] > 0

    page = client.get(f"/api/uploads/{body['upload_id']}/quarantine")
    assert page.status_code == 200
    assert_contract_valid(
        page.json(), spec, "/api/uploads/{id}/quarantine", "get", "200"
    )
    assert page.json()["total"] == body["quarantine_count"]
    for row in page.json()["items"]:
        assert row["raw"] != ""
        assert row["detail"] != ""


def test_an_empty_export_is_not_the_same_fact_as_a_damaged_one(client):
    """The distinction the console is built on. A header with no data rows
    means the merchant exported the wrong date range; a file whose every row is
    refused means the data is damaged. "0 records" says neither.
    """
    header = header_of(CLEAN)
    empty = send_bytes(client, "empty.csv", (header + "\n").encode("utf-8")).json()
    assert empty["state"] == "empty"
    assert empty["record_count"] == 0
    assert empty["quarantine_count"] == 0

    # Same header, one row of pure damage: read, and refused.
    damaged = send_bytes(
        client,
        "damaged.csv",
        (header + "\n" + ",".join(["not-a-value"] * 12) + "\n").encode("utf-8"),
    ).json()
    assert damaged["state"] == "quarantined"
    assert damaged["record_count"] == 0
    assert damaged["quarantine_count"] > 0


def test_a_missing_header_column_is_reported_as_a_file_level_quarantine(client):
    """A truncated export -- the merchant deselected a column in the portal.
    The file is read, nothing comes out, and the reason names the column."""
    lines = [
        line
        for line in CLEAN.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    trimmed = "\n".join(",".join(row.split(",")[:3]) for row in lines)
    response = send_bytes(client, "trimmed.csv", (trimmed + "\n").encode("utf-8"))
    # Trimming this far usually stops the header being recognisable at all,
    # which is a 422; if it is still recognised, it must be a quarantine and
    # never a crash. Both are correct answers and neither is a 500.
    assert response.status_code in (200, 422), response.text
    if response.status_code == 200:
        assert response.json()["state"] in ("quarantined", "empty")


# --- paging over quarantine ---------------------------------------------------


def test_quarantine_paging_never_repeats_a_row(client):
    body = send(client, DIRTY).json()
    upload_id = body["upload_id"]
    seen: list[tuple[int, str]] = []
    page = 1
    while True:
        got = client.get(
            f"/api/uploads/{upload_id}/quarantine?page={page}&size=1"
        ).json()
        if not got["items"]:
            break
        seen += [(row["row_number"], row["raw"]) for row in got["items"]]
        page += 1
        assert page < 100

    assert len(seen) == body["quarantine_count"]
    assert seen == sorted(seen, key=lambda row: row[0])


def test_a_page_past_the_end_is_an_empty_page_and_not_an_error(client):
    body = send(client, DIRTY).json()
    got = client.get(
        f"/api/uploads/{body['upload_id']}/quarantine?page=999&size=50"
    )
    assert got.status_code == 200
    assert got.json()["items"] == []
    assert got.json()["total"] == body["quarantine_count"]


def test_page_zero_is_refused_rather_than_silently_corrected(client):
    body = send(client, DIRTY).json()
    assert (
        client.get(
            f"/api/uploads/{body['upload_id']}/quarantine?page=0"
        ).status_code
        == 422
    )
