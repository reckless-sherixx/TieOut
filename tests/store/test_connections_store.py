"""The `connections` table: tenancy, redaction, and what a failed sync does.

Four properties, and the first two are the ones a credential table has to earn
before anything else:

1. **Another org's connection is INVISIBLE, not merely unauthorised.** The
   repository is bound to one org and every read filters on it, exactly as
   every other table here does -- so `connection("con-a")` from org B is
   `None`, indistinguishable from an id that was never issued. A 403 would be
   a confirmation that the id exists.
2. **The ciphertext leaves the store only through the method named for it.**
   `ConnectionSummary` -- what listing and reading return -- carries
   `has_password` and no ciphertext at all, so a route cannot serialise one by
   accident. `connection_credentials` is the one door, and it is named so that
   a call to it is visible in review.
3. **A failed sync does not advance `last_sync_at`.** Advancing it would mean
   a failure silently skips a month, which is the defect the fetcher exists to
   avoid.
4. **Nothing here reads a clock.** `created_at` and the success timestamp are
   handed in, and a `datetime` is required rather than defaulted -- the same
   guard `create_run` and `record_upload` carry.

No real credential appears below. The "ciphertexts" are obvious byte literals:
what is under test is the column and the filter, not the cipher, and
`tests/store/test_secretbox.py` owns the cipher.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.store.repo import ConnectionSummary, Repo
from core.store.schema import DEFAULT_ORG_ID

SEALED = b"RCNSEC1-not-a-real-ciphertext"
SEALED_PDF = b"RCNSEC1-not-a-real-pdf-ciphertext"

STAMPED = datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(tmp_path / "recon.db")


def save(repo: Repo, connection_id: str = "con-1", **overrides) -> ConnectionSummary:
    fields = {
        "connection_id": connection_id,
        "kind": "imap",
        "imap_host": "imap.example.test",
        "imap_port": 993,
        "imap_user": "merchant@example.test",
        "secret_ciphertext": SEALED,
        "pdf_secret_ciphertext": None,
        "senders": "noreply@bank.example.test",
        "folder": "INBOX",
        "filename_pattern": None,
        "created_at": STAMPED,
    }
    fields.update(overrides)
    return repo.save_connection(**fields)


# --- shape --------------------------------------------------------------------


def test_a_saved_connection_reads_back_with_its_configuration(repo):
    saved = save(repo)
    assert saved.id == "con-1"
    assert saved.kind == "imap"
    assert saved.imap_host == "imap.example.test"
    assert saved.imap_port == 993
    assert saved.imap_user == "merchant@example.test"
    assert saved.senders == "noreply@bank.example.test"
    assert saved.folder == "INBOX"
    assert saved.created_at == STAMPED.isoformat()


def test_a_new_connection_has_never_synced(repo):
    saved = save(repo)
    assert saved.last_sync_status == "never"
    assert saved.last_sync_at is None
    assert saved.last_sync_error is None


def test_the_summary_carries_has_password_and_no_ciphertext(repo):
    """Rule 2 of the spec, enforced at the type rather than at the route.

    A route can only serialise what the store hands it, so the shape that
    leaves the store is where "the secret is never readable back" is cheapest
    to guarantee: there is no field to leak.
    """
    saved = save(repo, pdf_secret_ciphertext=SEALED_PDF)
    assert saved.has_password is True
    assert saved.has_pdf_password is True
    dumped = saved.model_dump()
    assert "secret_ciphertext" not in dumped
    assert "pdf_secret_ciphertext" not in dumped
    assert SEALED.decode("ascii") not in saved.model_dump_json()


def test_no_pdf_password_reads_back_as_false(repo):
    assert save(repo).has_pdf_password is False


def test_the_ciphertexts_come_back_only_from_the_method_named_for_them(repo):
    save(repo, pdf_secret_ciphertext=SEALED_PDF)
    credentials = repo.connection_credentials("con-1")
    assert credentials is not None
    assert credentials.secret_ciphertext == SEALED
    assert credentials.pdf_secret_ciphertext == SEALED_PDF


def test_created_at_must_be_stamped_by_the_caller(repo):
    """`core/` reads no clock, so a string is a TypeError rather than a parse."""
    with pytest.raises(TypeError):
        save(repo, created_at=STAMPED.isoformat())


# --- create or replace --------------------------------------------------------


def test_saving_the_same_id_replaces_rather_than_duplicates(repo):
    save(repo)
    save(repo, imap_user="other@example.test")
    connections = repo.list_connections()
    assert len(connections) == 1
    assert connections[0].imap_user == "other@example.test"


def test_replacing_a_connection_resets_its_sync_state(repo):
    """New credentials describe a different mailbox, so the old outcome lies.

    A replace that kept `last_sync_at` would leave the fetcher believing it had
    already read a mailbox it has never opened, and the merchant would wait up
    to thirty days to find out their fix worked.
    """
    save(repo)
    repo.record_sync_success("con-1", at=STAMPED)
    save(repo, imap_user="corrected@example.test")
    replaced = repo.connection("con-1")
    assert replaced.last_sync_status == "never"
    assert replaced.last_sync_at is None


# --- tenancy ------------------------------------------------------------------


def test_another_orgs_connection_is_invisible_rather_than_forbidden(repo):
    save(repo)
    other = repo.scoped("org-globex")
    assert other.connection("con-1") is None
    assert other.list_connections() == []
    assert other.connection_credentials("con-1") is None


def test_another_org_cannot_delete_a_connection_it_cannot_see(repo):
    save(repo)
    assert repo.scoped("org-globex").delete_connection("con-1") is False
    assert repo.connection("con-1") is not None


def test_another_org_cannot_write_a_sync_outcome_onto_it(repo):
    save(repo)
    other = repo.scoped("org-globex")
    assert other.record_sync_success("con-1", at=STAMPED) is False
    assert other.record_sync_failure("con-1", error="nope") is False
    assert repo.connection("con-1").last_sync_status == "never"


def test_two_orgs_may_hold_connections_with_the_same_id(repo):
    """The id is unique per org, not globally: a tenant's identifiers are
    theirs, and a collision must not be a leak or a refusal."""
    save(repo, imap_user="a@example.test")
    save(repo.scoped("org-globex"), imap_user="b@example.test")
    assert repo.connection("con-1").imap_user == "a@example.test"
    assert repo.scoped("org-globex").connection("con-1").imap_user == "b@example.test"


def test_connection_org_ids_reports_every_org_holding_one(repo):
    """The one deliberately cross-org read, and the only thing it returns.

    The fetcher belongs to no tenant, so it has to be able to ask which orgs
    have work; it then scopes a repository per org and every read after that is
    filtered again. Returning ids and nothing else is what keeps the widening
    to one line that a reviewer can see.
    """
    save(repo)
    save(repo.scoped("org-globex"), connection_id="con-2")
    assert sorted(repo.connection_org_ids()) == [DEFAULT_ORG_ID, "org-globex"]


def test_connection_org_ids_is_empty_when_nobody_has_one(repo):
    assert repo.connection_org_ids() == []


# --- sync outcomes ------------------------------------------------------------


def test_a_successful_sync_advances_the_clock_and_clears_the_error(repo):
    save(repo)
    repo.record_sync_failure("con-1", error="the mail server refused")
    assert repo.record_sync_success("con-1", at=STAMPED) is True
    connection = repo.connection("con-1")
    assert connection.last_sync_status == "ok"
    assert connection.last_sync_at == STAMPED.isoformat()
    assert connection.last_sync_error is None


def test_a_failed_sync_does_not_advance_last_sync_at(repo):
    """The rule the monthly fetcher is built on (spec 2026-09-02 section 4).

    A failure that advanced the clock would make the next tick decide there is
    nothing to do, and the merchant would silently lose a month of statements
    to a password they could have fixed the same day.
    """
    save(repo)
    repo.record_sync_success("con-1", at=STAMPED)
    later = STAMPED + timedelta(days=31)
    assert repo.record_sync_failure("con-1", error="login refused") is True
    connection = repo.connection("con-1")
    assert connection.last_sync_status == "failed"
    assert connection.last_sync_at == STAMPED.isoformat(), (
        "a failed sync must leave the last SUCCESSFUL sync time in place, or "
        "the next tick would decide the month is already done"
    )
    assert connection.last_sync_error == "login refused"
    assert later.isoformat() != connection.last_sync_at


def test_a_failure_on_a_connection_that_never_synced_leaves_it_never_synced(repo):
    save(repo)
    repo.record_sync_failure("con-1", error="login refused")
    connection = repo.connection("con-1")
    assert connection.last_sync_at is None
    assert connection.last_sync_status == "failed"


def test_a_success_timestamp_must_be_stamped_by_the_caller(repo):
    save(repo)
    with pytest.raises(TypeError):
        repo.record_sync_success("con-1", at=STAMPED.isoformat())


def test_recording_against_an_unknown_connection_reports_false(repo):
    assert repo.record_sync_success("con-nope", at=STAMPED) is False
    assert repo.record_sync_failure("con-nope", error="x") is False


# --- deletion -----------------------------------------------------------------


def test_deleting_removes_the_row_and_its_ciphertext(repo):
    save(repo, pdf_secret_ciphertext=SEALED_PDF)
    assert repo.delete_connection("con-1") is True
    assert repo.connection("con-1") is None
    assert repo.connection_credentials("con-1") is None
    assert repo.list_connections() == []


def test_deleting_an_unknown_connection_reports_false(repo):
    assert repo.delete_connection("con-nope") is False


# --- listing ------------------------------------------------------------------


def test_connections_list_in_a_deterministic_order(repo):
    """Ordered by creation then by id, which is total.

    The console renders this list; a page whose order depended on SQLite's
    rowid would reshuffle under the merchant every time a row was replaced.
    """
    save(repo, connection_id="con-b", created_at=STAMPED)
    save(repo, connection_id="con-a", created_at=STAMPED)
    save(repo, connection_id="con-c", created_at=STAMPED - timedelta(days=1))
    assert [c.id for c in repo.list_connections()] == ["con-c", "con-a", "con-b"]
