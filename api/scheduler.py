"""The monthly fetcher (spec 2026-09-02 section 4).

One `asyncio` task, started in the FastAPI lifespan, that wakes every
`RECON_SYNC_INTERVAL_SECONDS` and asks the `connections` table which mailboxes
are due. A due mailbox is fetched and ingested through the SAME `api/ingest`
path a manual upload uses -- `api/connections.run_sync` is the one
implementation and this module calls it -- and the outcome is written back to
the row.

**State is in the table, not in the task.** Nothing about "when did we last
fetch this" lives in memory. A restarted process reads `last_sync_at` and
reaches the same decision the old one would have: it neither re-fetches what
was just fetched nor waits a full interval for a cycle it has no record of.
That is also why the loop runs a pass at STARTUP and sleeps afterwards rather
than the other way round -- a deployment restarted daily would otherwise never
fetch at all.

**A failed sync does not advance `last_sync_at`.** It sets the status to
"failed", records a scrubbed reason, and leaves the timestamp exactly where it
was, so the next tick finds the connection still due and retries. The opposite
-- advancing on failure -- would be silent: the following tick would decide the
month was done, and a merchant would lose a statement to a password they could
have fixed the same day.

**One broken mailbox cannot stop the others.** Every per-connection error is
caught, scrubbed and recorded, and the loop moves on. A tenant whose statements
stopped arriving because another tenant's password expired is the failure this
structure exists to prevent, and it is asserted across two orgs in
`tests/api/test_scheduler.py`.

**Thirty days, not a calendar month.** A calendar month needs a policy for the
29th, 30th and 31st and gains nothing: statements do not arrive on a fixed day,
and the fetch window is a RANGE -- a sync asks for the last 45 days regardless,
so an overlap re-reads a statement the content hash already deduplicates.

**Not IMAP IDLE.** Monthly statements do not need a persistent socket, and
Gmail drops IDLE every ~29 minutes. Polling on a day timer has one failure
mode; IDLE has reconnection, backoff and a task per connection.

**The clock and the connection factory are parameters.** `run_due_syncs` takes
`now` and `imap_factory`, so every test here runs offline and instantly: a test
that wants to be thirty-one days later says so rather than waiting. A scheduler
whose tests had to sleep would be a scheduler nobody ran in CI.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from core.store.repo import Repo
from core.store.secretbox import SecretBox

from api import settings
from api.connections import (
    SYNC_WINDOW_DAYS,
    SyncFailed,
    default_imap_factory,
    scrubbed,
)
from api.deps import _repo_for
from api.jobs import utc_now

#: How long after a successful sync a connection becomes due again. See the
#: module docstring for why this is a duration and not a calendar rule.
SYNC_DUE_AFTER_DAYS = 30

#: The loop's own logger. It never formats a credential: everything it writes
#: about a failure has already been through `scrubbed`, and the connection is
#: named by its id.
log = logging.getLogger("api.scheduler")


@dataclass(frozen=True)
class TickReport:
    """What one pass did. Returned for tests and for the log line.

    Ids only -- no host, no user, nothing decrypted. A report that carried more
    would eventually be formatted into a log line, and a log line is the exact
    place a credential must never reach.
    """

    synced: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    #: Connections this pass left alone because they are NOT DUE. A different
    #: meaning from `SyncOutcome.skipped` in `api/connections.py`, which counts
    #: ATTACHMENTS that did not become an upload -- these are connections that
    #: were never opened.
    skipped: list[str] = field(default_factory=list)
    #: Why the pass did nothing, when that needs saying. Empty otherwise.
    detail: str = ""

    def summary(self) -> str:
        return (
            f"{len(self.synced)} synced, {len(self.failed)} failed, "
            f"{len(self.skipped)} not due"
        )


def is_due(last_sync_at: str | None, now: datetime) -> bool:
    """Whether a connection last synced at `last_sync_at` should fetch now.

    Three answers of "yes" and they are all deliberate:

    * **Never synced.** The first tick after a merchant saves their mailbox
      must fetch, or setting one up is followed by thirty days of nothing --
      indistinguishable, from the merchant's side, from a broken setup.
    * **Thirty days or more.** `>=` rather than `>`: a monthly cycle that
      needed thirty days *and one second* would drift a tick later every month.
    * **An unreadable timestamp.** "We cannot tell when this last ran" and
      "this ran recently" are different facts and only the second justifies
      doing nothing. Fetching again costs an overlapping window the content
      hash already deduplicates; not fetching costs every statement from here
      on.

    A naive timestamp is read as UTC. Everything this system writes is
    offset-aware, but a hand-edited row or an older database can hold one, and
    comparing it to an aware `now` would raise rather than answer -- which
    would take the whole pass down over one bad row if the caller were not
    catching.
    """
    if not last_sync_at:
        return True
    try:
        parsed = datetime.fromisoformat(last_sync_at)
    except (TypeError, ValueError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed) >= timedelta(days=SYNC_DUE_AFTER_DAYS)


def run_due_syncs(
    repo: Repo,
    *,
    now: datetime,
    box: SecretBox | None,
    imap_factory: Callable[..., object],
) -> TickReport:
    """One pass over every org's connections. Never raises for one mailbox.

    `repo` is the unscoped instance `api/deps.py` caches; this asks it which
    orgs hold connections and then works through `repo.scoped(org_id)`, so
    every read and write below is filtered by the same tenancy predicate a
    request would be. The background loop belongs to no tenant, which is why
    that one widening exists -- see `Repo.connection_org_ids`.

    `box=None` means the deployment has no `RECON_BLOB_KEY`. Nothing can be
    decrypted, so the pass reports that and stops rather than raising once a
    day into a log nobody reads.
    """
    if box is None:
        return TickReport(
            detail=(
                "no RECON_BLOB_KEY is configured, so no stored credential can "
                "be decrypted and nothing was fetched"
            )
        )

    report = TickReport()
    end = now.date()
    start = end - timedelta(days=SYNC_WINDOW_DAYS)

    for org_id in repo.connection_org_ids():
        scoped = repo.scoped(org_id)
        for summary in scoped.list_connections():
            if not is_due(summary.last_sync_at, now):
                # Decided BEFORE anything opens a socket. A loop that connected
                # and then decided would log in to every merchant's mailbox
                # every day, which is both a bill and the kind of pattern a
                # bank's monitoring notices.
                report.skipped.append(summary.id)
                continue

            credentials = scoped.connection_credentials(summary.id)
            if credentials is None:  # pragma: no cover -- deleted mid-pass
                continue
            try:
                outcome = _sync(
                    scoped,
                    credentials,
                    box=box,
                    imap_factory=imap_factory,
                    start=start,
                    end=end,
                    now=now,
                )
            except SyncFailed:
                # `run_sync` has already recorded the scrubbed reason on the
                # row. Swallowed here on purpose: one broken mailbox must not
                # cost the connections after it in this loop.
                report.failed.append(summary.id)
            except Exception as exc:  # noqa: BLE001 -- the loop cannot die
                # The belt for a failure `run_sync` did not classify -- a
                # locked database, a bug. Recorded the same way, through the
                # same scrub, so a background failure is not a second and
                # unwatched path to a credential in a column.
                detail = scrubbed(f"the scheduled fetch failed: {exc}")
                scoped.record_sync_failure(summary.id, error=detail)
                report.failed.append(summary.id)
            else:
                report.synced.append(summary.id)
                log.info(
                    "synced %s: %d uploads, %d skipped",
                    summary.id,
                    len(outcome.upload_ids),
                    outcome.skipped,
                )
    return report


def run_tick() -> TickReport:
    """One production pass: this process's database, key, clock and transport.

    Separated from `run_due_syncs` so the loop has something to call that reads
    configuration, and the pass itself has nothing to read -- which is what
    lets every test above hand it a clock.
    """
    report = run_due_syncs(
        _repo_for(str(settings.db_path())),
        now=utc_now(),
        box=_secret_box(),
        imap_factory=default_imap_factory(),
    )
    if report.synced or report.failed:
        log.info("scheduled sync pass: %s", report.summary())
    return report


async def sync_loop(
    *, interval_seconds: float, tick: Callable[[], object] = run_tick
) -> None:
    """Wake, run a pass, sleep. Forever, until the task is cancelled.

    **A pass first, then the sleep.** A loop that slept first would mean a
    process restarted daily never fetched anything -- and "resume from the
    table" is the whole point of keeping the state there.

    **The pass runs in a thread.** `run_tick` opens a mailbox and parses
    statements, both blocking, and doing that on the event loop would stall
    every request the API is serving for as long as a bank's mail server takes
    to answer.

    **A pass that raises does not end the loop.** `run_due_syncs` already
    catches per connection; this is the belt for what it could not. A loop that
    died on one bad pass would stop fetching for every merchant until somebody
    restarted the process, and nothing would say why.
    """
    while True:
        try:
            await asyncio.to_thread(tick)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- the loop never crashes the app
            log.exception("a scheduled sync pass failed; the loop continues")
        await asyncio.sleep(interval_seconds)


def start_sync_loop() -> asyncio.Task | None:
    """The task, or `None` when `RECON_SYNC_ENABLED` says off.

    `None` rather than a task that returns immediately: a task is a thing that
    can wake, and "disabled" has to mean nothing was scheduled at all. That is
    what the test suite relies on -- `tests/conftest.py` sets the variable for
    every test, and a timer waking beside 1,500 of them would be a source of
    flakiness nobody would attribute correctly.
    """
    if not settings.sync_enabled():
        return None
    return asyncio.create_task(
        sync_loop(interval_seconds=settings.sync_interval_seconds()),
        name="recon-sync-loop",
    )


async def stop_sync_loop(task: asyncio.Task | None) -> None:
    """Cancel the loop and wait for it. Idempotent, and never raises.

    Awaited rather than merely cancelled: a cancellation that is not collected
    leaves a task with a pending exception at interpreter exit, which surfaces
    as a warning on every shutdown and teaches everybody to ignore warnings on
    shutdown.
    """
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def _secret_box() -> SecretBox | None:
    """The vault's cipher for this process, or `None` when there is no key."""
    key = settings.blob_key()
    return None if key is None else SecretBox(key)


def _sync(repo, credentials, **kwargs):
    """`api/connections.run_sync`, imported at call time.

    Late so that this module and `api/connections.py` can import from each
    other's direction without a cycle at import: the router imports nothing
    from here, and this needs the router's one shared function.
    """
    from api.connections import run_sync

    return run_sync(repo, credentials, **kwargs)
