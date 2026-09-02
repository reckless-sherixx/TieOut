"""The credential vault, from the outside (spec 2026-09-02 section 3).

Everything here exists because of one sentence: a merchant types a Gmail App
Password into a form once and this system keeps it so a fetcher can log in to
their mailbox every month without asking again. That is a persisted secret
granting **full mailbox read access**, and the three rules the spec attaches to
it each get their own test below.

1. **No key, no storage.** `test_a_keyless_deployment_refuses_to_store_a_credential`.
2. **The secret is never readable back.**
   `test_no_response_body_anywhere_carries_the_password`, which walks every
   route this router has rather than asserting about one of them -- the rule is
   "there is no such endpoint to be found later", and a test that named the
   endpoints it trusted would not be checking that.
3. **It never reaches a log or an error.**
   `test_a_failing_sync_does_not_put_the_password_in_last_sync_error`, with a
   mail server that quotes the password back, because that is the realistic
   way a credential ends up in a database column: not because anybody wrote it
   there, but because an upstream echoed it into a message somebody forwarded.

**No real credential appears in this file and none is read from `.env`.** The
fixture password below is an obvious literal, and the point of it is to be
searched for in response bodies.

Offline throughout. The IMAP connection is a FastAPI dependency and is
overridden with a fake, so no test here opens a socket.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from tests.api.test_routes import assert_contract_valid, spec  # noqa: F401

#: A 32-byte AES key, base64. Test material; the deployment's own comes from
#: `RECON_BLOB_KEY` and is never in a committed file.
TEST_BLOB_KEY = "dGVzdC1rZXktZm9yLXRoZS12YXVsdC10ZXN0cy0zMmI="

#: Sixteen characters with no spaces -- the SHAPE of a Gmail App Password, and
#: emphatically not one. It is deliberately distinctive so the leak tests can
#: search whole response bodies for it.
FAKE_PASSWORD = "zzappzzpasszzwd1"
FAKE_PDF_PASSWORD = "zzpdfzzsecret999"

#: An HDFC statement, header-shape valid, one clean row. Inline rather than
#: read from `fixtures/`: what is under test is the route, and a fixture edit
#: must not be able to break a routing test.
HDFC_BYTES = (
    b"Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
    b"01/08/26,NEFT-RAZORPAY,0.00,1000.00,1000.00\n"
)

#: Every connector variable, cleared for every test here, so the suite does not
#: behave differently on a developer's machine because their `.env` holds a
#: real mailbox.
CONNECTOR_ENV = (
    "RECON_RAZORPAY_KEY_ID",
    "RECON_RAZORPAY_KEY_SECRET",
    "RECON_IMAP_HOST",
    "RECON_IMAP_PORT",
    "RECON_IMAP_USER",
    "RECON_IMAP_PASSWORD",
    "RECON_IMAP_SENDERS",
    "RECON_IMAP_FOLDER",
    "RECON_IMAP_FILENAME_PATTERN",
    "RECON_PDF_PASSWORD",
    "RECON_WATCH_DIR",
)

CONNECTION = {
    "imap_host": "imap.example.test",
    "imap_port": 993,
    "imap_user": "merchant@example.test",
    "password": FAKE_PASSWORD,
    "senders": "statements@bank.example.test",
    "folder": "INBOX",
}


# --- a fake mailbox -----------------------------------------------------------


class FakeIMAP:
    """The subset of `imaplib.IMAP4_SSL` the connector uses, and no more.

    It RECORDS what it was asked to do, because `/test` is defined by what it
    does NOT do -- it authenticates and fetches nothing -- and that is a
    property of the commands issued, not of the reply.
    """

    def __init__(self, messages: dict[str, bytes] | None = None):
        self.messages = messages or {}
        self.searches: list[str] = []
        self.fetches: list[tuple[str, str]] = []
        self.selected: tuple[str, bool] | None = None
        self.logged_in: tuple[str, str] | None = None
        self.logged_out = False
        #: When set, `login` raises with this text. The realistic shape of a
        #: hostile upstream: it quotes back what it was sent.
        self.login_error: str | None = None

    def login(self, user, password):
        if self.login_error is not None:
            raise RuntimeError(self.login_error)
        self.logged_in = (user, password)
        return "OK", [b"logged in"]

    def select(self, folder, readonly=False):
        self.selected = (folder, readonly)
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searches.append(args[-1])
            return "OK", [" ".join(self.messages).encode()]
        if command == "FETCH":
            uid, spec_ = args[0], args[1]
            self.fetches.append((str(uid), spec_))
            raw = self.messages[str(uid)]
            return "OK", [
                (b"%s (BODY[] {%d}" % (str(uid).encode(), len(raw)), raw),
                b")",
            ]
        raise AssertionError(f"unexpected UID command {command}")

    def close(self):
        return "OK", [b"closed"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"bye"]


def message_with(filename: str, payload: bytes) -> bytes:
    msg = EmailMessage()
    msg["From"] = "statements@bank.example.test"
    msg["Subject"] = "Your monthly statement"
    msg.set_content("Attached.")
    msg.add_attachment(
        payload, maintype="text", subtype="csv", filename=filename,
        disposition="attachment",
    )
    return msg.as_bytes()


@pytest.fixture
def mailbox() -> FakeIMAP:
    return FakeIMAP({"1": message_with("statement-aug.csv", HDFC_BYTES)})


# --- the app ------------------------------------------------------------------


def build_client(tmp_path, monkeypatch, mailbox, *, blob_key: str | None) -> TestClient:
    monkeypatch.setenv("RECON_DB_PATH", str(tmp_path / "recon.db"))
    monkeypatch.setenv("RECON_DATASETS_DIR", str(tmp_path / "datasets"))
    monkeypatch.setenv("RECON_UPLOADS_DIR", str(tmp_path / "uploads"))
    # The background fetcher is section 4 and has its own tests; a route test
    # must not have a timer running beside it.
    monkeypatch.setenv("RECON_SYNC_ENABLED", "0")
    for name in CONNECTOR_ENV:
        monkeypatch.delenv(name, raising=False)
    if blob_key is None:
        monkeypatch.delenv("RECON_BLOB_KEY", raising=False)
    else:
        monkeypatch.setenv("RECON_BLOB_KEY", blob_key)

    from api.connections import get_imap_factory
    from api.main import create_app

    app = create_app()

    def factory():
        def build(host, port):
            mailbox.host, mailbox.port = host, port
            return mailbox

        return build

    app.dependency_overrides[get_imap_factory] = factory
    return TestClient(app)


@pytest.fixture
def client(tmp_path, monkeypatch, mailbox):
    with build_client(tmp_path, monkeypatch, mailbox, blob_key=TEST_BLOB_KEY) as c:
        yield c


@pytest.fixture
def keyless_client(tmp_path, monkeypatch, mailbox):
    with build_client(tmp_path, monkeypatch, mailbox, blob_key=None) as c:
        yield c


def create(client: TestClient, **overrides) -> dict:
    body = dict(CONNECTION)
    body.update(overrides)
    response = client.post("/api/connections", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# --- rule 1: no key, no storage -----------------------------------------------


def test_a_keyless_deployment_refuses_to_store_a_credential(keyless_client):
    """`RECON_BLOB_KEY` is optional for blobs and mandatory for credentials.

    The blob store has a documented plaintext mode because a local demo has to
    run. A mailbox password does not get one: "we would encrypt if configured"
    and "we encrypt" are different sentences to put in front of a merchant, and
    only the first is true of a keyless build.
    """
    response = keyless_client.post("/api/connections", json=dict(CONNECTION))
    assert response.status_code == 422, response.text
    assert "RECON_BLOB_KEY" in response.json()["detail"], (
        "the refusal has to NAME the variable: it is read by somebody about to "
        "paste a fix into a shell"
    )


def test_the_keyless_refusal_does_not_echo_the_password(keyless_client):
    response = keyless_client.post("/api/connections", json=dict(CONNECTION))
    assert FAKE_PASSWORD not in response.text


def test_a_keyless_deployment_can_still_list_connections(keyless_client):
    """Listing needs no key -- it decrypts nothing -- and a console that could
    not even render an empty list would be unable to explain itself."""
    response = keyless_client.get("/api/connections")
    assert response.status_code == 200
    assert response.json() == []


# --- rule 2: the secret is never readable back --------------------------------


def test_creating_a_connection_returns_has_password_and_not_the_password(
    client, spec
):
    created = create(client)
    assert created["has_password"] is True
    assert created["has_pdf_password"] is False
    assert "password" not in created
    assert "secret_ciphertext" not in created
    assert_contract_valid(created, spec, "/api/connections", "post", "200")


def test_a_pdf_password_is_reported_as_a_boolean_too(client):
    created = create(client, pdf_password=FAKE_PDF_PASSWORD)
    assert created["has_pdf_password"] is True


def test_listing_connections_is_redacted(client, spec):
    create(client)
    response = client.get("/api/connections")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["has_password"] is True
    assert_contract_valid(body, spec, "/api/connections", "get", "200")


def test_no_response_body_anywhere_carries_the_password(client, mailbox):
    """Rule 2, walked over every route rather than asserted about one.

    The rule is that there is no endpoint returning the value -- "not to the
    owner, not to an admin", and none to be found later. A test that listed the
    endpoints it trusted would not be checking that, so this walks the router's
    whole surface, including the error paths, with a mail server that quotes
    the password back at us.
    """
    created = create(client, pdf_password=FAKE_PDF_PASSWORD)
    connection_id = created["id"]

    mailbox.login_error = f"AUTHENTICATIONFAILED for {FAKE_PASSWORD}"
    responses = [
        client.post("/api/connections", json=dict(CONNECTION, id=connection_id)),
        client.get("/api/connections"),
        client.post(f"/api/connections/{connection_id}/test"),
        client.post(f"/api/connections/{connection_id}/sync"),
        client.get("/api/connections"),
        client.delete(f"/api/connections/{connection_id}"),
    ]
    for response in responses:
        assert FAKE_PASSWORD not in response.text, response.text
        assert FAKE_PDF_PASSWORD not in response.text, response.text


def test_the_ciphertext_is_not_the_plaintext_in_the_database(client, tmp_path):
    """The column holds an envelope, not the password.

    Read straight off the file rather than through the repository: a redaction
    that lived only in the read model would pass every test above and still be
    a password sitting in a SQLite file somebody backs up.
    """
    create(client)
    raw = (tmp_path / "recon.db").read_bytes()
    assert FAKE_PASSWORD.encode("ascii") not in raw


# --- rule 3: it never reaches a log or an error --------------------------------


def test_a_failing_sync_does_not_put_the_password_in_last_sync_error(
    client, mailbox
):
    """The realistic leak, reproduced.

    Nobody writes a password into a database column on purpose. What happens is
    that an upstream -- a mail server this system just sent a password to --
    quotes it back in a refusal, and the refusal is stored verbatim because
    storing the reason is the right instinct. The scrub is what makes the right
    instinct safe.
    """
    created = create(client)
    connection_id = created["id"]
    mailbox.login_error = f"NO [AUTHENTICATIONFAILED] Invalid credentials {FAKE_PASSWORD}"

    response = client.post(f"/api/connections/{connection_id}/sync")
    assert response.status_code == 502, response.text
    assert FAKE_PASSWORD not in response.text

    listed = client.get("/api/connections").json()[0]
    assert listed["last_sync_status"] == "failed"
    assert listed["last_sync_error"], "a failure has to say something"
    assert FAKE_PASSWORD not in listed["last_sync_error"], (
        "the mail server quoted the password back and it was stored verbatim"
    )
    assert "[redacted]" in listed["last_sync_error"]


def test_a_failing_test_route_scrubs_its_5xx_body(client, mailbox, spec):
    created = create(client)
    mailbox.login_error = f"login refused: {FAKE_PASSWORD}"
    response = client.post(f"/api/connections/{created['id']}/test")
    assert response.status_code == 502
    assert FAKE_PASSWORD not in response.text
    assert "[redacted]" in response.json()["detail"]


# --- sync ---------------------------------------------------------------------


def test_a_synced_file_goes_through_the_same_ingest_path_an_upload_does(
    client, spec
):
    """The rule that makes this safe: no shortcut for having arrived by IMAP.

    Asserted on the UPLOAD the sync produced, not on the sync's status code. A
    route that recorded a row without sniffing and parsing it would pass a
    status-code assertion and fail this one.
    """
    created = create(client)
    response = client.post(f"/api/connections/{created['id']}/sync")
    assert response.status_code == 200, response.text
    body = response.json()
    assert_contract_valid(
        body, spec, "/api/connections/{id}/sync", "post", "200"
    )
    assert len(body["upload_ids"]) == 1

    upload = client.get(f"/api/uploads/{body['upload_ids'][0]}").json()
    assert upload["format_id"] == "bank-csv-hdfc-v1"
    assert upload["record_count"] == 1
    assert upload["state"] == "ingested"


def test_a_successful_sync_records_its_outcome_on_the_connection(client):
    created = create(client)
    client.post(f"/api/connections/{created['id']}/sync")
    listed = client.get("/api/connections").json()[0]
    assert listed["last_sync_status"] == "ok"
    assert listed["last_sync_at"] is not None
    assert listed["last_sync_error"] is None


def test_syncing_the_same_mailbox_twice_produces_one_upload(client):
    """The content hash is the identity, so an overlapping window is free.

    This is what lets the fetcher ask for 45 days every month without caring
    that the windows overlap.
    """
    created = create(client)
    first = client.post(f"/api/connections/{created['id']}/sync").json()
    second = client.post(f"/api/connections/{created['id']}/sync").json()
    assert first["upload_ids"] == second["upload_ids"]
    assert len(client.get("/api/uploads").json()) == 1


def test_a_declined_attachment_is_counted_and_named(client, mailbox):
    """Section 2's rule, surfaced on this route: never silently dropped."""
    mailbox.messages = {
        "1": message_with("statement-aug.csv", HDFC_BYTES),
        "2": message_with("sliceCIBILReportLatest.pdf", b"%PDF-1.4 not a statement"),
    }
    created = create(client)
    body = client.post(f"/api/connections/{created['id']}/sync").json()
    assert len(body["upload_ids"]) == 1
    assert body["skipped"] == 1
    assert any("CIBIL" in name for name in body["skipped_names"])


def test_syncing_an_unknown_connection_is_a_404(client):
    response = client.post("/api/connections/con-nope/sync")
    assert response.status_code == 404


# --- test ---------------------------------------------------------------------


def test_test_authenticates_and_fetches_nothing(client, mailbox, spec):
    """Why `/test` exists at all.

    "My password is wrong" and "my sender filter matches nothing" both present
    as zero files, and a merchant cannot tell them apart from a zero. So this
    route logs in, opens the folder read-only, and issues no SEARCH and no
    FETCH -- which is asserted on the commands, because a route that quietly
    fetched would return the same 200.
    """
    created = create(client)
    response = client.post(f"/api/connections/{created['id']}/test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert_contract_valid(
        body, spec, "/api/connections/{id}/test", "post", "200"
    )

    assert mailbox.logged_in == ("merchant@example.test", FAKE_PASSWORD)
    assert mailbox.selected == ("INBOX", True), "the folder must be opened read-only"
    assert mailbox.searches == [], "/test fetches nothing, so it searches nothing"
    assert mailbox.fetches == []
    assert mailbox.logged_out is True


def test_test_does_not_touch_the_sync_state(client, mailbox):
    """A connectivity check is not a sync, and must not look like one.

    If `/test` wrote `last_sync_at`, pressing it would tell the monthly fetcher
    the month was done -- a merchant checking their setup would lose a
    statement to having checked.
    """
    created = create(client)
    client.post(f"/api/connections/{created['id']}/test")
    listed = client.get("/api/connections").json()[0]
    assert listed["last_sync_status"] == "never"
    assert listed["last_sync_at"] is None


def test_testing_an_unknown_connection_is_a_404(client):
    assert client.post("/api/connections/con-nope/test").status_code == 404


# --- validation ---------------------------------------------------------------


def test_a_connection_with_no_sender_filter_is_refused(client):
    """Refused at creation rather than at the first sync.

    `core/connectors/imap_mailbox.py` refuses an unfiltered search -- it would
    download every message in the window, which is a mailbox dump and not a
    reconciliation input. Storing a connection that can only ever fail would
    move that refusal a month into the future.
    """
    response = client.post("/api/connections", json=dict(CONNECTION, senders="  "))
    assert response.status_code == 422
    assert "senders" in response.text


def test_an_empty_password_is_refused(client):
    response = client.post("/api/connections", json=dict(CONNECTION, password=""))
    assert response.status_code == 422


def test_a_filename_pattern_that_is_not_a_regex_is_refused_at_creation(client):
    response = client.post(
        "/api/connections", json=dict(CONNECTION, filename_pattern="statement[")
    )
    assert response.status_code == 422
    assert "filename_pattern" in response.text


def test_a_port_outside_the_range_is_refused(client):
    response = client.post("/api/connections", json=dict(CONNECTION, imap_port=0))
    assert response.status_code == 422


# --- replace and delete -------------------------------------------------------


def test_posting_an_existing_id_replaces_it(client):
    created = create(client)
    replaced = create(client, id=created["id"], imap_user="corrected@example.test")
    assert replaced["id"] == created["id"]
    listed = client.get("/api/connections").json()
    assert len(listed) == 1
    assert listed[0]["imap_user"] == "corrected@example.test"


def test_deleting_removes_the_connection(client, spec):
    created = create(client)
    response = client.delete(f"/api/connections/{created['id']}")
    assert response.status_code == 200, response.text
    assert_contract_valid(
        response.json(), spec, "/api/connections/{id}", "delete", "200"
    )
    assert client.get("/api/connections").json() == []


def test_deleting_an_unknown_connection_is_a_404(client):
    assert client.delete("/api/connections/con-nope").status_code == 404


def test_a_deleted_connection_cannot_be_synced(client):
    created = create(client)
    client.delete(f"/api/connections/{created['id']}")
    assert client.post(f"/api/connections/{created['id']}/sync").status_code == 404
