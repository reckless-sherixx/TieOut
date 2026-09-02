"""`GET /api/connectors` and `POST /api/connectors/{name}/sync`.

**The rule that makes this safe, and the only interesting thing in the file:** a
fetched file enters through the *same* `api/ingest.py` path an uploaded file
does -- hashed for idempotency, sniffed by header shape, quarantined row by row,
stored in the same blob store. It gets no shortcut for having arrived over
HTTPS or IMAP. A sync is a merchant dragging a file onto the page, minus the
dragging.

Two consequences follow and both are asserted in `tests/api/test_connectors.py`:
syncing the same statement twice produces one upload, because the content hash
is what identifies it; and a file this build cannot read is a `skipped` count on
the response rather than a 500, because a watched folder is a directory a human
drops things into and it will contain things nobody can read.

**Refusals are split in two**, because the fixes are: a `ConnectorUnconfigured`
is a 422 naming the variable to set, and any other upstream failure is a 502
naming the connector. Collapsing them would tell somebody with a broken mail
server to go and check their environment.

No clock is read below `api/`. `uploaded_at` is stamped here from
`api.jobs.utc_now`, the same boundary that stamps a run's `created_at`.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, model_validator

from core.connectors.base import ConnectorUnconfigured, FetchedFile
from core.store.repo import Repo

from api import settings
from api.auth import require_principal
from api.deps import get_repo
from api.ingest import UploadRefused, UploadTooLarge, ingest_upload
from api.jobs import utc_now

#: Mounted behind `require_principal` on the router rather than per handler, for
#: the reason `api/routes.py` states: a route added tomorrow is authenticated by
#: having been added at all.
router = APIRouter(dependencies=[Depends(require_principal)], tags=["connectors"])

RepoDep = Annotated[Repo, Depends(get_repo)]


#: What replaces a secret that reached a message it should not have.
REDACTED = "[redacted]"


def redacted(text: str) -> str:
    """No configured secret survives into a response body.

    The 502 below forwards text that ORIGINATED UPSTREAM -- a mail server's
    refusal, a payment gateway's error -- and no upstream is trusted not to
    quote back something it was sent. Every refusal written in
    `core/connectors/` already names the variable and never the value, and its
    tests hold it to that; this is the belt for the message nobody wrote.
    """
    for secret in settings.connector_secrets():
        text = text.replace(secret, REDACTED)
    return text


class SyncRequest(BaseModel):
    """The window to fetch. Dates, not timestamps: a settlement report and a
    bank statement are both period documents, and an hour has no meaning in
    either."""

    start: date
    end: date

    @model_validator(mode="after")
    def _window_is_ordered(self) -> "SyncRequest":
        if self.end < self.start:
            raise ValueError(
                f"end {self.end} precedes start {self.start}; a fetch window "
                f"cannot run backwards"
            )
        return self


@router.get("/api/connectors", response_model=None)
def list_connectors() -> list[dict]:
    """Every connector this build ships, and whether it is configured.

    **`available` is a boolean and never a credential.** It answers the only
    question a console has -- can this button do anything -- and a response
    that echoed a key id "for diagnostics" would put one in a browser's network
    tab, in a screenshot, and in whatever the console logs.

    Unconfigured is the default and is reported as `false`, not omitted: a
    connector missing from this list would be indistinguishable from one this
    build does not have, and the console has different things to say about the
    two.
    """
    return [
        {"name": name, "available": connector.available()}
        for name, connector in settings.connectors().items()
    ]


@router.post("/api/connectors/{name}/sync", response_model=None)
def sync_connector(name: str, body: SyncRequest, repo: RepoDep) -> dict:
    """Fetch the window from one connector and ingest everything it returns.

    404 for a connector this build does not have; 422 for one nobody has
    configured, naming the variables to set; 502 for a counterparty that failed;
    200 with `upload_ids` and `skipped` otherwise.

    `skipped` counts files that were fetched and could not be turned into an
    upload -- a format nothing recognises, an encrypted PDF, a file past the
    size ceiling. They are counted rather than raised on: one unreadable file in
    a watched folder must not cost the merchant the statements beside it, and a
    count is what makes "nothing appeared" distinguishable from "nothing was
    there".
    """
    connectors = settings.connectors()
    connector = connectors.get(name)
    if connector is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no connector named {name!r}; this build ships "
                f"{sorted(connectors)}"
            ),
        )

    try:
        fetched = connector.fetch(body.start, body.end)
    except ConnectorUnconfigured as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - an upstream is not a 500 here
        # The connector's own message, which by contract names a variable and
        # never a value. A 502 rather than a 500: the failure is the
        # counterparty's, and the distinction is what tells an operator whether
        # to look at their configuration or at their bank.
        raise HTTPException(
            status_code=502,
            detail=redacted(f"{name} could not complete the fetch: {exc}"),
        ) from exc

    upload_ids: list[str] = []
    skipped = 0
    for file in fetched:
        upload_id = _ingest(repo, file)
        if upload_id is None:
            skipped += 1
        else:
            upload_ids.append(upload_id)

    return {"upload_ids": upload_ids, "skipped": skipped}


def _ingest(repo: Repo, file: FetchedFile) -> str | None:
    """One fetched file through the ordinary upload path, or `None`.

    `None` is "this file was fetched and could not be ingested", which the
    caller counts. It is deliberately not an exception: the loop above has more
    files to place, and a refusal on one of them is information rather than a
    failure of the sync.
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
    return ingestion.upload.upload_id
