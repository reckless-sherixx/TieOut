"""A pulled file is an uploaded file that nobody had to drag.

The one property this endpoint exists to preserve is that a file gets **no
shortcut for arriving over the wire**. It goes through `api/ingest.py` -- the
same content hash, the same header-shape sniff, the same quarantine, the same
blob store -- as a file a merchant dropped on the page. So the tests below
assert on the *upload* a sync produced, not merely on the sync's status code:
an endpoint that recorded a row without parsing it would pass the second and
fail the fourth.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.api.test_routes import assert_contract_valid, spec  # noqa: F401

#: An HDFC statement, header-shape valid, one clean row. Written inline rather
#: than copied from `fixtures/real-formats/` because what is under test is the
#: route, and a fixture path in this file would make a fixture edit able to
#: break a routing test for reasons that have nothing to do with routing.
HDFC_BYTES = (
    b"Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
    b"01/08/26,NEFT-RAZORPAY,0.00,1000.00,1000.00\n"
)

#: Every connector variable, cleared for every test here. The suite must not
#: behave differently on a developer's machine because their `.env` happens to
#: hold a mailbox -- and one of these tests would otherwise try to open it.
CONNECTOR_ENV = (
    "RECON_RAZORPAY_KEY_ID",
    "RECON_RAZORPAY_KEY_SECRET",
    "RECON_IMAP_HOST",
    "RECON_IMAP_PORT",
    "RECON_IMAP_USER",
    "RECON_IMAP_PASSWORD",
    "RECON_IMAP_SENDERS",
    "RECON_IMAP_FOLDER",
    "RECON_PDF_PASSWORD",
    "RECON_WATCH_DIR",
)

WINDOW = {"start": "2026-08-01", "end": "2026-08-31"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client on an isolated database, dataset root and blob store."""
    monkeypatch.setenv("RECON_DB_PATH", str(tmp_path / "recon.db"))
    monkeypatch.setenv("RECON_DATASETS_DIR", str(tmp_path / "datasets"))
    monkeypatch.setenv("RECON_UPLOADS_DIR", str(tmp_path / "uploads"))
    for name in CONNECTOR_ENV:
        monkeypatch.delenv(name, raising=False)
    from api.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def watched(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "watch"
    root.mkdir()
    monkeypatch.setenv("RECON_WATCH_DIR", str(root))
    return root


# --- listing ------------------------------------------------------------------


def test_listing_connectors_reports_configured_state_not_an_error(client, spec):
    r = client.get("/api/connectors")
    assert r.status_code == 200
    assert_contract_valid(r.json(), spec, "/api/connectors", "get", "200")

    names = {c["name"]: c for c in r.json()}
    assert set(names) == {"razorpay-api", "imap-mailbox", "watched-folder"}
    for connector in names.values():
        assert connector["available"] in (True, False)


def test_an_unconfigured_deployment_lists_every_connector_as_off(client):
    """Absent is the DEFAULT. A connector missing from the list would be
    indistinguishable from one this build does not ship."""
    connectors = client.get("/api/connectors").json()
    assert connectors, "an empty list would make the assertion below vacuous"
    assert all(c["available"] is False for c in connectors)


def test_a_configured_connector_reports_itself_available(client, watched):
    connectors = {c["name"]: c for c in client.get("/api/connectors").json()}
    assert connectors["watched-folder"]["available"] is True
    assert connectors["razorpay-api"]["available"] is False


def test_the_listing_never_carries_a_credential(client, monkeypatch):
    """The one thing this response must never contain. `available` is a
    boolean for exactly this reason."""
    monkeypatch.setenv("RECON_RAZORPAY_KEY_ID", "kid-9f3c")
    monkeypatch.setenv("RECON_RAZORPAY_KEY_SECRET", "sec-4a17")
    body = client.get("/api/connectors").text
    assert "kid-9f3c" not in body and "sec-4a17" not in body


# --- refusals -----------------------------------------------------------------


def test_syncing_an_unconfigured_connector_is_422_and_names_the_variable(client):
    r = client.post("/api/connectors/razorpay-api/sync", json=WINDOW)
    assert r.status_code == 422
    assert "RECON_RAZORPAY_KEY_ID" in r.json()["detail"]


def test_syncing_an_unconfigured_mailbox_names_its_variables_too(client):
    r = client.post("/api/connectors/imap-mailbox/sync", json=WINDOW)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "RECON_IMAP_HOST" in detail
    assert "RECON_IMAP_USER" in detail
    assert "RECON_IMAP_PASSWORD" in detail


def test_a_refusal_names_the_variable_and_never_its_value(client, monkeypatch):
    """A 422 is read by a person who is about to paste a fix into a shell. It
    tells them which variable; it must never hand back the value it already
    has."""
    monkeypatch.setenv("RECON_IMAP_HOST", "imap.example.test")
    monkeypatch.setenv("RECON_IMAP_USER", "merchant@example.test")
    monkeypatch.setenv("RECON_IMAP_PASSWORD", "app-password-9f3c")
    r = client.post("/api/connectors/imap-mailbox/sync", json=WINDOW)
    assert r.status_code == 422
    assert "RECON_IMAP_SENDERS" in r.json()["detail"]
    assert "app-password-9f3c" not in r.text


def test_an_upstream_failure_is_a_502_that_carries_no_secret(client, monkeypatch):
    """The 502 forwards text that came from somewhere else.

    Every refusal written in `core/connectors/` names a variable and never a
    value, and its own tests hold it to that. This covers the other half: a
    mail server's own error string, which nobody in this repository wrote and
    which is not trusted not to quote back what it was sent.
    """
    monkeypatch.setenv("RECON_IMAP_HOST", "imap.invalid.test")
    monkeypatch.setenv("RECON_IMAP_USER", "merchant@example.test")
    monkeypatch.setenv("RECON_IMAP_PASSWORD", "app-password-9f3c")
    monkeypatch.setenv("RECON_IMAP_SENDERS", "alerts@slice.co")

    from core.connectors import imap_mailbox

    def exploding_factory(host, port):
        # The shape of the risk, made concrete: an upstream that repeats what
        # it was handed. A real server does not do this; the point is that
        # nothing here depends on that.
        raise OSError("login rejected for app-password-9f3c")

    monkeypatch.setattr(
        imap_mailbox.imaplib, "IMAP4_SSL", exploding_factory, raising=True
    )
    r = client.post("/api/connectors/imap-mailbox/sync", json=WINDOW)
    assert r.status_code == 502
    assert "app-password-9f3c" not in r.text
    assert "[redacted]" in r.json()["detail"]


def test_an_unknown_connector_is_404(client):
    r = client.post("/api/connectors/nope/sync", json=WINDOW)
    assert r.status_code == 404


def test_a_window_that_ends_before_it_starts_is_refused(client, watched):
    r = client.post(
        "/api/connectors/watched-folder/sync",
        json={"start": "2026-08-31", "end": "2026-08-01"},
    )
    assert r.status_code == 422


# --- the point: a fetched file is an uploaded file -----------------------------


def test_a_synced_file_becomes_an_upload_with_the_same_sniff(client, watched, spec):
    """The whole point: no shortcut for arriving over the wire."""
    (watched / "hdfc.csv").write_bytes(HDFC_BYTES)

    r = client.post("/api/connectors/watched-folder/sync", json=WINDOW)
    assert r.status_code == 200, r.text
    assert_contract_valid(
        r.json(), spec, "/api/connectors/{name}/sync", "post", "200"
    )
    ids = r.json()["upload_ids"]
    assert len(ids) == 1
    assert r.json()["skipped"] == 0

    got = client.get(f"/api/uploads/{ids[0]}").json()
    assert got["format_id"] == "bank-csv-hdfc-v1"
    assert got["state"] == "ingested"
    assert got["filename"] == "hdfc.csv"


def test_syncing_the_same_file_twice_is_idempotent_by_content(client, watched):
    """`api/ingest.py` keys an upload on its bytes. A sync that bypassed it
    would produce a second upload here, which is the cheapest possible proof
    that it did not bypass it."""
    (watched / "hdfc.csv").write_bytes(HDFC_BYTES)

    first = client.post("/api/connectors/watched-folder/sync", json=WINDOW).json()
    second = client.post("/api/connectors/watched-folder/sync", json=WINDOW).json()
    assert first["upload_ids"] == second["upload_ids"]
    assert len(client.get("/api/uploads").json()) == 1


def test_a_file_no_adapter_reads_is_counted_as_skipped_not_a_500(client, watched):
    """A watched folder is a directory a human drops things into, so it will
    contain things this build cannot read. That is a count on the response,
    not a stack trace, and it must not stop the files beside it."""
    (watched / "hdfc.csv").write_bytes(HDFC_BYTES)
    (watched / "notes.csv").write_bytes(b"Txn Ref,Particulars,Amount\n1,X,10.00\n")

    body = client.post("/api/connectors/watched-folder/sync", json=WINDOW).json()
    assert len(body["upload_ids"]) == 1
    assert body["skipped"] == 1


def test_an_empty_watched_folder_syncs_nothing_and_says_so(client, watched):
    body = client.post("/api/connectors/watched-folder/sync", json=WINDOW).json()
    assert body == {"upload_ids": [], "skipped": 0}


def test_a_synced_file_is_quarantined_the_same_way_an_uploaded_one_is(
    client, watched
):
    """The sharper half of "the same sniff": a file that IS recognised but
    whose rows are damaged lands in quarantine, reachable at the same endpoint
    as any upload's."""
    (watched / "hdfc.csv").write_bytes(
        b"Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        b"01/08/26,NEFT-RAZORPAY,0.00,1000.00,1000.00\n"
        b"02/08/26,BROKEN,0.00,12.3x,1012.30\n"
    )
    upload_id = client.post(
        "/api/connectors/watched-folder/sync", json=WINDOW
    ).json()["upload_ids"][0]

    page = client.get(f"/api/uploads/{upload_id}/quarantine?page=1&size=5").json()
    assert page["total"] == 1
    assert page["items"][0]["reason"] == "BAD_DECIMAL"
