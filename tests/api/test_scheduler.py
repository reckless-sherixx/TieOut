"""The monthly fetcher (spec 2026-09-02 section 4).

**Nothing here sleeps and nothing here opens a socket.** The clock is a
parameter and the IMAP connection is a factory, both injected, so a test that
wants to be thirty-one days later says so instead of waiting. A scheduler whose
tests had to sleep would be a scheduler nobody ran in CI.

Four properties, one test each, and the middle two are the ones that decide
whether a merchant ever loses a month:

* **State is in the table, not in the task.** A fresh process reading the same
  database reaches the same decision -- so a restart neither re-fetches nor
  waits another cycle.
* **A failed sync does not advance `last_sync_at`.** The next tick retries. A
  failure that advanced the clock would tell the following tick the month was
  done, and the statement would be lost silently, which is the expensive part.
* **One broken mailbox cannot stop the others.** Asserted across two orgs,
  because that is also the only path that exercises the deliberately
  cross-org `connection_org_ids` read.
* **`RECON_SYNC_ENABLED=0` starts nothing.** Not "starts and does nothing" --
  no task is created at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from api import scheduler
from api.connections import SYNC_WINDOW_DAYS
from core.store.repo import Repo
from core.store.secretbox import SecretBox

#: A 32-byte AES key. Test material.
KEY = b"k" * 32
BLOB_KEY_B64 = "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="

FAKE_PASSWORD = "zzappzzpasszzwd1"

NOW = datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc)

HDFC_BYTES = (
    b"Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
    b"01/08/26,NEFT-RAZORPAY,0.00,1000.00,1000.00\n"
)


# --- a fake mailbox -----------------------------------------------------------


class FakeIMAP:
    """The subset of `imaplib.IMAP4_SSL` the connector uses.

    `login_error` is how a mailbox is made to fail. It raises rather than
    returning a status because that is what a refused TLS handshake or a
    rejected password actually does through `imaplib`.
    """

    def __init__(self, messages=None, *, login_error: str | None = None):
        self.messages = messages or {}
        self.login_error = login_error
        self.logins = 0
        self.searches: list[str] = []

    def login(self, user, password):
        self.logins += 1
        if self.login_error is not None:
            raise RuntimeError(self.login_error)
        return "OK", [b"ok"]

    def select(self, folder, readonly=False):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searches.append(args[-1])
            return "OK", [" ".join(self.messages).encode()]
        if command == "FETCH":
            raw = self.messages[str(args[0])]
            return "OK", [
                (b"%s (BODY[] {%d}" % (str(args[0]).encode(), len(raw)), raw),
                b")",
            ]
        raise AssertionError(command)

    def close(self):
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


def statement_message() -> bytes:
    msg = EmailMessage()
    msg["From"] = "statements@bank.example.test"
    msg["Subject"] = "Statement"
    msg.set_content("Attached.")
    msg.add_attachment(
        HDFC_BYTES, maintype="text", subtype="csv",
        filename="statement-aug.csv", disposition="attachment",
    )
    return msg.as_bytes()


def factory_for(mailboxes: dict[str, FakeIMAP]):
    """One fake per host, so two connections can behave differently.

    That is what makes "one broken mailbox does not stop the others" testable:
    the two connections have to be able to disagree about whether they work.
    """

    def build(host, port):
        return mailboxes[host]

    return build


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(KEY)


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Repo:
    monkeypatch.setenv("RECON_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("RECON_BLOB_KEY", BLOB_KEY_B64)
    for name in ("RECON_IMAP_PASSWORD", "RECON_PDF_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    return Repo(tmp_path / "recon.db")


def store(
    repo: Repo,
    box: SecretBox,
    connection_id: str,
    *,
    host: str = "imap.example.test",
    created_at: datetime = NOW,
):
    return repo.save_connection(
        connection_id=connection_id,
        kind="imap",
        imap_host=host,
        imap_port=993,
        imap_user="merchant@example.test",
        secret_ciphertext=box.seal(
            FAKE_PASSWORD, connection_id=connection_id, field="secret"
        ),
        pdf_secret_ciphertext=None,
        senders="statements@bank.example.test",
        folder="INBOX",
        filename_pattern=None,
        created_at=created_at,
    )


# --- is_due -------------------------------------------------------------------


def test_a_connection_that_has_never_synced_is_due():
    """The first tick after a merchant saves their mailbox must fetch.

    Otherwise setting up a connection would be followed by thirty days of
    nothing, which is indistinguishable from a broken setup.
    """
    assert scheduler.is_due(None, NOW) is True


def test_a_connection_synced_yesterday_is_not_due():
    yesterday = (NOW - timedelta(days=1)).isoformat()
    assert scheduler.is_due(yesterday, NOW) is False


def test_a_connection_synced_twenty_nine_days_ago_is_not_due():
    assert scheduler.is_due((NOW - timedelta(days=29)).isoformat(), NOW) is False


def test_a_connection_synced_thirty_days_ago_is_due():
    """Thirty days, not a calendar month.

    A calendar month needs a policy for the 29th, 30th and 31st and gains
    nothing: statements do not arrive on a fixed day, and the fetch window is a
    range anyway -- the overlap re-reads a statement the content hash already
    deduplicates.
    """
    assert scheduler.is_due((NOW - timedelta(days=30)).isoformat(), NOW) is True


def test_an_unreadable_timestamp_is_treated_as_due():
    """A corrupt column must not become a permanent hole.

    "We cannot tell when this last ran" and "this ran recently" are different
    facts, and only one of them justifies doing nothing. Fetching again costs
    an overlapping window the content hash deduplicates; not fetching costs the
    merchant every statement from here on.
    """
    assert scheduler.is_due("not a timestamp", NOW) is True


def test_a_naive_timestamp_is_read_as_utc():
    """Everything this system writes is offset-aware, but a hand-edited row or
    an older database can hold a naive one, and comparing it to an aware `now`
    would raise rather than answer."""
    naive = (NOW - timedelta(days=40)).replace(tzinfo=None).isoformat()
    assert scheduler.is_due(naive, NOW) is True


# --- one pass -----------------------------------------------------------------


def test_a_tick_fetches_a_due_connection_and_records_the_outcome(repo, box):
    mailbox = FakeIMAP({"1": statement_message()})
    report = scheduler.run_due_syncs(
        repo,
        now=NOW,
        box=box,
        imap_factory=factory_for({"imap.example.test": mailbox}),
    )
    assert report.synced == [] and report.failed == []

    store(repo, box, "con-1")
    report = scheduler.run_due_syncs(
        repo,
        now=NOW,
        box=box,
        imap_factory=factory_for({"imap.example.test": mailbox}),
    )
    assert report.synced == ["con-1"]
    connection = repo.connection("con-1")
    assert connection.last_sync_status == "ok"
    assert connection.last_sync_at == NOW.isoformat()
    assert len(repo.list_uploads()) == 1


def test_the_window_is_the_last_forty_five_days(repo, box):
    """Wider than the interval, deliberately.

    The overlap re-reads statements the content hash already deduplicates; a
    window that started where the last one ended would turn one missed tick
    into a permanent hole.
    """
    mailbox = FakeIMAP({"1": statement_message()})
    store(repo, box, "con-1")
    scheduler.run_due_syncs(
        repo,
        now=NOW,
        box=box,
        imap_factory=factory_for({"imap.example.test": mailbox}),
    )
    since = (NOW.date() - timedelta(days=SYNC_WINDOW_DAYS)).strftime("%d-%b-%Y")
    assert mailbox.searches, "the connector never searched"
    assert since in mailbox.searches[0], mailbox.searches


def test_a_connection_that_is_not_due_is_not_even_connected_to(repo, box):
    """Due-ness is decided before anything opens a socket.

    A loop that connected and then decided would log in to every merchant's
    mailbox every day, which is both a bill and a security event a bank's
    monitoring would notice.
    """
    mailbox = FakeIMAP({"1": statement_message()})
    store(repo, box, "con-1")
    repo.record_sync_success("con-1", at=NOW - timedelta(days=1))
    report = scheduler.run_due_syncs(
        repo,
        now=NOW,
        box=box,
        imap_factory=factory_for({"imap.example.test": mailbox}),
    )
    assert report.skipped == ["con-1"]
    assert mailbox.logins == 0


def test_a_failed_sync_does_not_advance_the_clock_and_the_next_tick_retries(
    repo, box
):
    """The rule this whole design turns on.

    A failure that advanced `last_sync_at` would make the following tick decide
    there was nothing to do. The merchant would lose a month to a password they
    could have fixed the same day, and nothing would say so.
    """
    broken = FakeIMAP(login_error=f"NO [AUTHENTICATIONFAILED] {FAKE_PASSWORD}")
    store(repo, box, "con-1")
    repo.record_sync_success("con-1", at=NOW - timedelta(days=40))
    before = repo.connection("con-1").last_sync_at

    report = scheduler.run_due_syncs(
        repo, now=NOW, box=box, imap_factory=factory_for({"imap.example.test": broken})
    )
    assert report.failed == ["con-1"]
    after = repo.connection("con-1")
    assert after.last_sync_status == "failed"
    assert after.last_sync_at == before, (
        "a failed sync advanced the clock, so the next tick will skip the month"
    )

    # The next tick, a day later. Still due, and it tries again.
    tomorrow = NOW + timedelta(days=1)
    again = scheduler.run_due_syncs(
        repo,
        now=tomorrow,
        box=box,
        imap_factory=factory_for({"imap.example.test": broken}),
    )
    assert again.failed == ["con-1"]
    assert broken.logins == 2, "the next tick did not retry"


def test_a_failure_recorded_by_the_loop_is_scrubbed(repo, box):
    """The loop writes to the same column the route does, through the same
    scrub -- a background failure is not a second, unwatched path to a
    credential in the database."""
    broken = FakeIMAP(login_error=f"refused: {FAKE_PASSWORD}")
    store(repo, box, "con-1")
    scheduler.run_due_syncs(
        repo, now=NOW, box=box, imap_factory=factory_for({"imap.example.test": broken})
    )
    error = repo.connection("con-1").last_sync_error
    assert error and FAKE_PASSWORD not in error
    assert "[redacted]" in error


def test_one_broken_mailbox_does_not_stop_the_others(repo, box):
    """Two orgs, one broken. The second still gets its statement.

    Across orgs rather than within one, because this is also the only test that
    exercises `Repo.connection_org_ids` -- the single deliberately cross-org
    read in the store, and the one thing standing between the fetcher and a
    tenant whose statements never arrive because somebody else's password
    expired.
    """
    other = repo.scoped("org-globex")
    store(repo, box, "con-a", host="broken.example.test")
    store(other, box, "con-b", host="working.example.test")

    mailboxes = {
        "broken.example.test": FakeIMAP(login_error="connection reset"),
        "working.example.test": FakeIMAP({"1": statement_message()}),
    }
    report = scheduler.run_due_syncs(
        repo, now=NOW, box=box, imap_factory=factory_for(mailboxes)
    )

    assert report.failed == ["con-a"]
    assert report.synced == ["con-b"]
    assert repo.connection("con-a").last_sync_status == "failed"
    assert other.connection("con-b").last_sync_status == "ok"
    assert len(other.list_uploads()) == 1
    assert repo.list_uploads() == [], "the working org's upload landed in the wrong org"


def test_a_restart_reaches_the_same_decision_from_the_table(repo, box, tmp_path):
    """State in the table, not in the task.

    A second `Repo` over the same file is what a restarted process sees. It
    must neither re-fetch what was just fetched nor wait a cycle it has no
    record of.
    """
    mailbox = FakeIMAP({"1": statement_message()})
    store(repo, box, "con-1")
    scheduler.run_due_syncs(
        repo, now=NOW, box=box, imap_factory=factory_for({"imap.example.test": mailbox})
    )
    assert mailbox.logins == 1

    restarted = Repo(tmp_path / "recon.db")
    report = scheduler.run_due_syncs(
        restarted,
        now=NOW + timedelta(days=1),
        box=box,
        imap_factory=factory_for({"imap.example.test": mailbox}),
    )
    assert report.skipped == ["con-1"]
    assert mailbox.logins == 1

    later = scheduler.run_due_syncs(
        restarted,
        now=NOW + timedelta(days=31),
        box=box,
        imap_factory=factory_for({"imap.example.test": mailbox}),
    )
    assert later.synced == ["con-1"]


def test_a_tick_with_no_key_does_nothing_rather_than_raising(repo):
    """A deployment that lost `RECON_BLOB_KEY` has a fetcher that cannot
    decrypt anything. It reports that and stops, rather than raising once a day
    into a log nobody reads."""
    report = scheduler.run_due_syncs(
        repo, now=NOW, box=None, imap_factory=factory_for({})
    )
    assert report.synced == [] and report.failed == []
    assert "RECON_BLOB_KEY" in report.detail


# --- the loop -----------------------------------------------------------------


def test_the_loop_is_not_started_when_it_is_disabled(tmp_path, monkeypatch):
    """`RECON_SYNC_ENABLED=0` creates no task at all.

    Not "creates a task that does nothing": a task is a thing that can wake, an
    absent task is not, and the test suite runs with this set for exactly that
    reason.
    """
    monkeypatch.setenv("RECON_DB_PATH", str(tmp_path / "recon.db"))
    monkeypatch.setenv("RECON_DATASETS_DIR", str(tmp_path / "datasets"))
    monkeypatch.setenv("RECON_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("RECON_SYNC_ENABLED", "0")
    from api.main import create_app

    with TestClient(create_app()) as client:
        assert client.app.state.sync_task is None


def test_the_loop_is_started_and_stopped_by_the_lifespan(tmp_path, monkeypatch):
    """The default is ON, because the loop IS the feature.

    A very long interval, so the pass this asserts on is the one the loop runs
    at STARTUP -- which is the behaviour a restart depends on: resume now, from
    the table, rather than after another full interval.
    """
    monkeypatch.setenv("RECON_DB_PATH", str(tmp_path / "recon.db"))
    monkeypatch.setenv("RECON_DATASETS_DIR", str(tmp_path / "datasets"))
    monkeypatch.setenv("RECON_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.delenv("RECON_SYNC_ENABLED", raising=False)
    monkeypatch.setenv("RECON_SYNC_INTERVAL_SECONDS", "86400")
    from api.main import create_app

    with TestClient(create_app()) as client:
        task = client.app.state.sync_task
        assert task is not None
    assert task.cancelled() or task.done(), "the lifespan left the loop running"


def test_the_loop_runs_a_pass_immediately_and_then_waits():
    """One pass at startup, then the interval. Asserted without a sleep.

    The tick is injected and signals a `threading.Event`, so this waits on the
    pass having happened rather than on a duration -- and returns the moment it
    has.
    """
    passes: list[int] = []
    ran = threading.Event()

    def tick():
        passes.append(1)
        ran.set()

    async def scenario():
        task = asyncio.create_task(
            scheduler.sync_loop(interval_seconds=86_400, tick=tick)
        )
        assert await asyncio.to_thread(ran.wait, 10), "the loop never ran a pass"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert passes == [1], "the loop should wait the interval before the next pass"


def test_a_pass_that_raises_does_not_kill_the_loop():
    """The loop never crashes the app.

    `run_due_syncs` already catches per connection; this is the belt for
    whatever it could not -- a database that is momentarily locked, a bug. A
    loop that died on one bad pass would stop fetching for every merchant until
    somebody restarted the process, and nothing would say why.
    """
    attempts: list[int] = []
    recovered = threading.Event()

    def tick():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("the database was locked")
        recovered.set()

    async def scenario():
        task = asyncio.create_task(scheduler.sync_loop(interval_seconds=0, tick=tick))
        assert await asyncio.to_thread(recovered.wait, 10), (
            "the loop stopped after a pass raised"
        )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(attempts) >= 2
