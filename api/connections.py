"""The credential vault's five routes (spec 2026-09-02 section 3).

`api/connectors.py` fetches from a mailbox this DEPLOYMENT configured, out of
environment variables an operator exported. This file is the other half: a
mailbox a MERCHANT configured, in a form, once, whose password this system then
holds so a fetcher can log in every month without asking again.

**That difference is the whole of the security story here.** An environment
variable lives in a process and dies with it; a stored credential lives in a
SQLite file that gets backed up, copied to a laptop, and read by whatever runs
next. And the credential in question is a Gmail App Password, which grants
**full mailbox read access** -- not access to statements, access to the
mailbox. Every rule below follows from that and from nothing else.

**1. No key, no storage.** `RECON_BLOB_KEY` is optional for blobs -- unset
means the blob store writes plaintext, and COMPLIANCE.md says so -- and is
REFUSED here: `POST /api/connections` answers 422 naming the variable. "We
would encrypt if configured" and "we encrypt" are different sentences to put in
front of a merchant, and a build that quietly stored a mailbox password in the
clear would make the second one false while every screen kept saying it.

**2. The secret is never readable back.** `core/store/repo.py` returns a
`ConnectionSummary` with `has_password: bool` and no ciphertext field at all,
so the shape a handler serialises cannot carry a credential. Reaching one takes
`Repo.connection_credentials`, which is called in exactly two places in this
file, and the plaintext exists only inside the function that is about to log in
with it. There is no route that returns it -- not to the owner, not to an
admin -- and none to be added later.

**3. It never reaches a log or an error.** `scrubbed()` extends
`api/settings.connector_secrets()` -- which covers the values this deployment
was CONFIGURED with -- to the values it has STORED, and every failure path
below goes through it before the text becomes either a response body or
`Connection.last_sync_error`. That is not paranoia about our own messages: the
realistic leak is a mail server quoting the password back in a refusal, which
is text nobody in this repository wrote and which the right instinct -- record
the reason -- would otherwise store verbatim.

**A fetched file gets no shortcut.** `_sync` calls `api.ingest.ingest_upload`,
the same function `POST /api/uploads` calls: the same content hash, the same
header-shape sniff, the same row-level quarantine, the same blob store. A
statement that arrived over IMAP is a statement a merchant dragged onto the
page, minus the dragging.

**Why `/test` exists.** "My password is wrong" and "my sender filter matches
nothing" both present to a merchant as zero files, and a zero cannot tell them
apart. So `/test` logs in, opens the folder read-only and issues no SEARCH and
no FETCH; a 200 from it means the credential works and any remaining zero is a
filter question.

**Where the socket is.** `get_imap_factory` is a FastAPI dependency returning
`imaplib.IMAP4_SSL`, on the seam `api/settings.razorpay_http()` already
establishes: the one function that opens a connection lives on this side of the
`core/` boundary and is injected, so every test in `tests/api/test_connections.py`
runs offline against a fake.

No clock is read below `api/`. `created_at` and `last_sync_at` are stamped here
from `api.jobs.utc_now`, the same boundary that stamps a run's `created_at`.
"""

from __future__ import annotations

import imaplib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Callable, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from core.connectors.base import ConnectorUnconfigured, FetchedFile
from core.connectors.imap_mailbox import ImapMailboxConnector
from core.store.repo import ConnectionCredentials, Repo
from core.store.secretbox import SecretBox, SecretRefused

from api import settings
from api.auth import require_principal
from api.deps import get_repo
from api.ingest import UploadRefused, UploadTooLarge, ingest_upload
from api.jobs import utc_now

#: Mounted behind `require_principal` on the router rather than per handler,
#: for the reason `api/routes.py` states: a route added tomorrow is
#: authenticated by having been added at all. It matters more here than
#: anywhere else in this API -- these routes hold the keys to a mailbox.
router = APIRouter(dependencies=[Depends(require_principal)], tags=["connections"])

RepoDep = Annotated[Repo, Depends(get_repo)]

#: The only kind this build stores. A column rather than an assumption, so the
#: OAuth token the vault is shaped to hold one day (spec section 8) is another
#: kind rather than another table.
IMAP_KIND = "imap"

#: The AAD field names. Spelled here, once, because they are half of what binds
#: a ciphertext to its column -- see `core/store/secretbox.py`. Changing either
#: string makes every stored credential unreadable, which is a deployment
#: outage rather than a wrong answer, so they are constants and not literals
#: scattered through the handlers.
SECRET_FIELD = "secret"
PDF_SECRET_FIELD = "pdf_secret"

#: What replaces a secret that reached a message it should not have. The same
#: token `api/connectors.py` uses, spelled again rather than imported: these
#: two files are owned by different lanes and a shared private constant would
#: couple them for no benefit.
REDACTED = "[redacted]"

#: How far back a sync asks for, every time, regardless of when the last one
#: ran. Deliberately wider than the 30-day interval in `api/scheduler.py`: the
#: overlap re-reads statements the content hash already deduplicates, and the
#: alternative -- a window that starts where the last one ended -- turns one
#: missed tick into a permanent hole.
SYNC_WINDOW_DAYS = 45


class SyncFailed(Exception):
    """A sync or a test could not complete. `detail` is ALREADY SCRUBBED.

    Carries the scrubbed text rather than the original because there is
    exactly one place the scrub can be forgotten -- the raiser -- and putting
    it in the constructor's contract means a handler cannot render an
    unscrubbed message even by accident.
    """

    def __init__(self, detail: str, *, status_code: int = 502) -> None:
        super().__init__(detail)
        self.detail = detail
        #: 502 for a counterparty that failed, 422 for configuration this
        #: deployment or this merchant has to fix. The split
        #: `api/connectors.py` makes, for the reason it makes it: telling
        #: somebody with a broken mail server to go and check their settings
        #: sends them to the wrong place.
        self.status_code = status_code


# --- secrets ------------------------------------------------------------------


def scrubbed(text: str, *stored: str | None) -> str:
    """`text` with every secret this process holds replaced by `REDACTED`.

    Two sources, and the second is what this file adds. `connector_secrets()`
    covers what the deployment was CONFIGURED with; `stored` covers what it has
    just decrypted out of the database in order to log in with it. A scrub that
    knew only about the environment would be blind to precisely the credential
    this feature exists to hold.

    Applied to error bodies and to `last_sync_error` alike, because the
    difference between the two is one HTTP response: a 5xx body is read by a
    browser and a column is read by a console, and a password is equally
    unwelcome in both.
    """
    for secret in (*settings.connector_secrets(), *stored):
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def get_secret_box() -> SecretBox:
    """The vault's cipher, or a 422 naming the variable that is missing.

    A dependency rather than a helper so the refusal is identical on every
    route that needs it, and so a route added later inherits it by asking for
    the box at all.
    """
    key = settings.blob_key()
    if key is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "this deployment has no RECON_BLOB_KEY set, so a mailbox "
                "password cannot be stored encrypted -- and it will not be "
                "stored any other way. Generate one with `python -c "
                '"import base64,os;print(base64.urlsafe_b64encode(os.urandom(32))'
                '.decode())"` and set RECON_BLOB_KEY in the environment, then '
                "add the connection again."
            ),
        )
    return SecretBox(key)


SecretBoxDep = Annotated[SecretBox, Depends(get_secret_box)]


def default_imap_factory() -> Callable[[str, int], imaplib.IMAP4]:
    """The real connection builder. `api/scheduler.py` uses this one directly."""
    return imaplib.IMAP4_SSL


def get_imap_factory() -> Callable[[str, int], imaplib.IMAP4]:
    """The injected seam. Overridden in tests; never overridden in a build."""
    return default_imap_factory()


ImapFactoryDep = Annotated[Callable[..., imaplib.IMAP4], Depends(get_imap_factory)]


# --- request bodies -----------------------------------------------------------


class ConnectionRequest(BaseModel):
    """Create or replace one mailbox connection.

    **This is the only body in the API that carries a secret**, and it carries
    two: the mailbox password and, optionally, the password an Indian bank puts
    on the statement PDF. Neither is echoed by any response.

    Everything is validated HERE rather than at the first sync. A connection
    that can only ever fail -- no sender filter, an uncompilable pattern -- is
    a failure the merchant would otherwise meet a month later, by which time
    the form they typed it into is long closed.
    """

    #: Absent to create, present to replace. Supplied by the client from a
    #: previous response rather than invented: it is scoped to the caller's
    #: org by the repository, so a guessed id from another tenant addresses
    #: nothing.
    id: str | None = None
    kind: Literal["imap"] = IMAP_KIND
    imap_host: str
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_user: str
    #: The app password. On Gmail it MUST be an App Password rather than the
    #: account password -- this connects over IMAP and Google refuses an
    #: account password there.
    password: str
    pdf_password: str | None = None
    #: Comma-separated. Never blank -- see the validator.
    senders: str
    folder: str = "INBOX"
    filename_pattern: str | None = None

    @field_validator("imap_host", "imap_user", "password")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("this field is required and cannot be blank")
        return value.strip() if value.strip() != value else value

    @field_validator("senders")
    @classmethod
    def _senders_are_required(cls, value: str) -> str:
        """An unfiltered mailbox search is a mailbox dump, not an input.

        `core/connectors/imap_mailbox.py` refuses one at fetch time. Refusing
        it here as well is not duplication: it is the difference between a
        merchant learning at the form and a merchant learning at the next
        monthly tick.
        """
        if not [part for part in value.split(",") if part.strip()]:
            raise ValueError(
                "senders must name at least one address or domain your bank "
                "sends statements from: searching a mailbox unfiltered would "
                "download every message in the window, which is not a "
                "reconciliation input"
            )
        return value

    @field_validator("filename_pattern")
    @classmethod
    def _pattern_compiles(cls, value: str | None) -> str | None:
        if value:
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError(
                    f"filename_pattern is not a valid regular expression "
                    f"({error}). It is optional -- leave it empty to fetch "
                    f"every statement attachment."
                ) from error
        return value


class SyncWindow(BaseModel):
    """An optional explicit window. Absent means the last `SYNC_WINDOW_DAYS`.

    Optional because the console's button says "Sync now" and a merchant
    pressing it has no window in mind; explicit because an operator
    backfilling a month they lost does.
    """

    start: date | None = None
    end: date | None = None


# --- routes -------------------------------------------------------------------


@router.post("/api/connections", response_model=None)
def save_connection(
    body: ConnectionRequest, repo: RepoDep, box: SecretBoxDep
) -> dict:
    """Create a connection, or replace the one this org holds under `body.id`.

    The response is a `ConnectionSummary`: `has_password`, never the password.
    The 422 for a keyless deployment is raised by `get_secret_box` before this
    function runs, so there is no path here that could store a plaintext
    credential even transiently.

    A replace resets the sync state, because the row now describes a different
    mailbox -- see `Repo.save_connection`.
    """
    connection_id = (body.id or f"con-{uuid4().hex[:12]}").strip()
    summary = repo.save_connection(
        connection_id=connection_id,
        kind=body.kind,
        imap_host=body.imap_host,
        imap_port=body.imap_port,
        imap_user=body.imap_user,
        secret_ciphertext=box.seal(
            body.password, connection_id=connection_id, field=SECRET_FIELD
        ),
        pdf_secret_ciphertext=(
            box.seal(
                body.pdf_password,
                connection_id=connection_id,
                field=PDF_SECRET_FIELD,
            )
            if body.pdf_password
            else None
        ),
        senders=body.senders,
        folder=body.folder or "INBOX",
        filename_pattern=body.filename_pattern or None,
        created_at=utc_now(),
    )
    return summary.model_dump(mode="json")


@router.get("/api/connections", response_model=None)
def list_connections(repo: RepoDep) -> list[dict]:
    """Every connection this org holds, redacted.

    Deliberately needs no `RECON_BLOB_KEY`: nothing here is decrypted, and a
    console that could not render its own list on a keyless build would be
    unable to explain why it cannot store a credential.
    """
    return [c.model_dump(mode="json") for c in repo.list_connections()]


@router.delete("/api/connections/{id}", response_model=None)
def delete_connection(id: str, repo: RepoDep) -> dict:
    """Remove the connection and its ciphertext. 404 if this org has none.

    A hard delete: a soft-deleted credential is one the merchant believes they
    revoked and this system still holds, which is the one shape of "deleted" a
    vault may not have.
    """
    if not repo.delete_connection(id):
        raise HTTPException(status_code=404, detail=_unknown(id))
    return {"id": id, "deleted": True}


@router.post("/api/connections/{id}/sync", response_model=None)
def sync_connection(
    id: str,
    repo: RepoDep,
    box: SecretBoxDep,
    imap_factory: ImapFactoryDep,
    body: SyncWindow | None = None,
) -> dict:
    """Fetch now, ingest everything, and record the outcome on the row.

    The outcome is written whether it succeeded or not, and a FAILURE DOES NOT
    ADVANCE `last_sync_at` -- so a merchant who presses this and sees it fail
    is still due at the next tick of `api/scheduler.py` rather than having
    quietly consumed their month.
    """
    now = utc_now()
    end = (body.end if body else None) or now.date()
    start = (body.start if body else None) or end - timedelta(days=SYNC_WINDOW_DAYS)
    if end < start:
        raise HTTPException(
            status_code=422,
            detail=f"end {end} precedes start {start}; a fetch window cannot "
            f"run backwards",
        )

    credentials = _credentials_or_404(repo, id)
    try:
        outcome = run_sync(
            repo,
            credentials,
            box=box,
            imap_factory=imap_factory,
            start=start,
            end=end,
            now=now,
        )
    except SyncFailed as failure:
        raise HTTPException(
            status_code=failure.status_code, detail=failure.detail
        ) from failure
    return outcome.as_dict()


@router.post("/api/connections/{id}/test", response_model=None)
def test_connection(
    id: str, repo: RepoDep, box: SecretBoxDep, imap_factory: ImapFactoryDep
) -> dict:
    """Authenticate and open the folder read-only. Fetch nothing.

    It writes NOTHING to the connection row. A connectivity check that stamped
    `last_sync_at` would mean a merchant verifying their setup had told the
    fetcher the month was done, and they would lose a statement to having
    checked.
    """
    credentials = _credentials_or_404(repo, id)
    password = None
    pdf_password = None
    try:
        password, pdf_password = _open_secrets(credentials, box)
        _authenticate(credentials, password=password, imap_factory=imap_factory)
    except SyncFailed as failure:
        raise HTTPException(
            status_code=failure.status_code, detail=failure.detail
        ) from failure
    except Exception as exc:  # noqa: BLE001 -- nothing reaches the wire raw
        detail = scrubbed(
            f"could not sign in to {credentials.imap_host}: {exc}",
            password,
            pdf_password,
        )
        raise HTTPException(status_code=502, detail=detail) from exc
    return {
        "ok": True,
        "detail": (
            f"signed in to {credentials.imap_host} as {credentials.imap_user} "
            f"and opened {credentials.folder} read-only. Nothing was fetched."
        ),
    }


# --- the shared sync, used by the route above and by api/scheduler.py ---------


@dataclass(frozen=True)
class SyncOutcome:
    """What one fetch produced. Returned to the route and to the scheduler.

    `skipped_names` is here for the reason section 2 gives about the fetcher's
    deny list: an attachment this system declined to read is a fact the
    merchant is entitled to, and a bare count cannot distinguish "your bank
    sent a credit report we refused" from "your bank sent nothing".
    """

    upload_ids: list[str]
    skipped: int
    skipped_names: list[str]
    quarantine_count: int
    window_start: date
    window_end: date

    def as_dict(self) -> dict:
        return {
            "upload_ids": list(self.upload_ids),
            "skipped": self.skipped,
            "skipped_names": list(self.skipped_names),
            "quarantine_count": self.quarantine_count,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
        }


def run_sync(
    repo: Repo,
    credentials: ConnectionCredentials,
    *,
    box: SecretBox,
    imap_factory,
    start: date,
    end: date,
    now: datetime,
) -> SyncOutcome:
    """Decrypt, fetch, ingest, and write the outcome back. One function.

    Shared by `POST /api/connections/{id}/sync` and by the monthly loop so the
    two cannot drift: a scheduled fetch and a pressed button must produce the
    same uploads, the same quarantine and the same row state, or "Sync now"
    stops being a way to find out whether the monthly one will work.

    **The plaintexts live only inside this call.** They are decrypted at the
    top, handed to the connector, and passed to `scrubbed` on every failure
    path so that neither can survive into `last_sync_error` or a response.

    Raises `SyncFailed` with an already-scrubbed detail. The failure is
    recorded on the row BEFORE it is raised, so a caller that drops the
    exception -- which the scheduler deliberately does, one broken mailbox must
    not stop the others -- still leaves the merchant a reason on the screen.
    """
    password: str | None = None
    pdf_password: str | None = None
    try:
        password, pdf_password = _open_secrets(credentials, box)
        connector = _connector(
            credentials,
            password=password,
            pdf_password=pdf_password,
            imap_factory=imap_factory,
        )
        fetched = connector.fetch(start, end)
        skipped_names = list(getattr(connector, "last_skipped", []))
    except SyncFailed as failure:
        repo.record_sync_failure(credentials.id, error=failure.detail)
        raise
    except ConnectorUnconfigured as exc:
        detail = scrubbed(f"{credentials.id} cannot fetch: {exc}", password, pdf_password)
        repo.record_sync_failure(credentials.id, error=detail)
        raise SyncFailed(detail, status_code=422) from exc
    except Exception as exc:  # noqa: BLE001 -- an upstream is not a 500 here
        detail = scrubbed(
            f"the fetch from {credentials.imap_host} failed: {exc}",
            password,
            pdf_password,
        )
        repo.record_sync_failure(credentials.id, error=detail)
        raise SyncFailed(detail) from exc

    upload_ids: list[str] = []
    quarantined = 0
    for file in fetched:
        upload = _ingest(repo, file)
        if upload is None:
            # Fetched and unreadable: a format nothing recognises, a statement
            # PDF still encrypted, a file past the size ceiling. Counted rather
            # than raised on, so one bad attachment does not cost the merchant
            # the statements beside it.
            skipped_names.append(file.suggested_name)
        else:
            upload_ids.append(upload.upload_id)
            quarantined += upload.quarantine_count

    repo.record_sync_success(credentials.id, at=now)
    return SyncOutcome(
        upload_ids=upload_ids,
        # ONE count over both reasons a file can fail to become an upload: the
        # fetcher declined to read it (a credit report, a name outside the
        # pattern) or the ingest path could not read it. A merchant asking
        # "why is this not two uploads" wants one number and the names behind
        # it, not a taxonomy of who said no first.
        skipped=len(skipped_names),
        skipped_names=skipped_names,
        quarantine_count=quarantined,
        window_start=start,
        window_end=end,
    )


# --- internals ----------------------------------------------------------------


def _unknown(connection_id: str) -> str:
    """The 404 detail. Identical for "never existed" and "belongs to another
    org", because the repository cannot tell them apart and neither may a
    caller: a distinguishable answer is a confirmation that the id is real."""
    return f"no connection with id {connection_id!r}"


def _credentials_or_404(repo: Repo, connection_id: str) -> ConnectionCredentials:
    credentials = repo.connection_credentials(connection_id)
    if credentials is None:
        raise HTTPException(status_code=404, detail=_unknown(connection_id))
    return credentials


def _open_secrets(
    credentials: ConnectionCredentials, box: SecretBox
) -> tuple[str, str | None]:
    """The two plaintexts, or a 422 naming the variable that would fix it.

    `SecretRefused` here means the ciphertext will not authenticate under this
    process's key, and by far the likeliest cause is that `RECON_BLOB_KEY` was
    rotated or lost. That is configuration rather than a counterparty, so it is
    a 422 and it names the variable -- and it says to re-enter the credential,
    because a rotated key makes the stored one unrecoverable by design.
    """
    try:
        password = box.unseal(
            credentials.secret_ciphertext,
            connection_id=credentials.id,
            field=SECRET_FIELD,
        )
        pdf_password = (
            box.unseal(
                credentials.pdf_secret_ciphertext,
                connection_id=credentials.id,
                field=PDF_SECRET_FIELD,
            )
            if credentials.pdf_secret_ciphertext
            else None
        )
    except SecretRefused as exc:
        raise SyncFailed(
            "this connection's stored credential will not decrypt under the "
            "current RECON_BLOB_KEY. The key has been changed or lost; the "
            "stored password cannot be recovered by design. Save the "
            "connection again with the password re-entered.",
            status_code=422,
        ) from exc
    return password, pdf_password


def _connector(
    credentials: ConnectionCredentials,
    *,
    password: str,
    pdf_password: str | None,
    imap_factory,
) -> ImapMailboxConnector:
    """The connector for one stored connection.

    Built per call and never cached: it holds a decrypted password, and a
    cached instance would keep one alive between requests for no benefit --
    the object opens no connection until `fetch`.
    """
    return ImapMailboxConnector(
        host=credentials.imap_host,
        port=credentials.imap_port,
        username=credentials.imap_user,
        password=password,
        sender_filter=credentials.senders,
        folder=credentials.folder,
        pdf_password=pdf_password,
        filename_pattern=credentials.filename_pattern,
        imap_factory=imap_factory,
    )


def _authenticate(
    credentials: ConnectionCredentials, *, password: str, imap_factory
) -> None:
    """Log in, open the folder read-only, log out. **No SEARCH, no FETCH.**

    Written here rather than as a method on `ImapMailboxConnector` because
    that module is another lane's this session -- but the shape is deliberate
    either way: `readonly=True` means the server cannot set `\\Seen`, so a
    merchant pressing "Test" cannot mark their own unread mail read, which is
    exactly the side effect that would make this feature untrustworthy.
    """
    connection = imap_factory(credentials.imap_host, credentials.imap_port)
    try:
        connection.login(credentials.imap_user, password)
        connection.select(credentials.folder, readonly=True)
    finally:
        for step in ("close", "logout"):
            try:
                getattr(connection, step)()
            except Exception:  # noqa: BLE001 -- a failed hang-up is not the answer
                pass


def _ingest(repo: Repo, file: FetchedFile):
    """One fetched file through the ordinary upload path, or `None`.

    `None` means "fetched and could not be ingested", which the caller counts
    and names. Deliberately not an exception: the loop has more files to place,
    and one unreadable attachment must not cost the merchant the statements
    beside it.
    """
    try:
        ingestion = ingest_upload(
            repo,
            filename=file.suggested_name,
            payload=file.content,
            uploaded_at=utc_now(),
        )
    except (UploadRefused, UploadTooLarge):
        return None
    return ingestion.upload
