"""The audit log is what makes a match explainable after the fact.

Two properties carry the weight: ordering comes from a monotonic `sequence`
int and never from a clock (global constraint -- no wall-clock inside core/),
and the log is append-only, so a caller holding a snapshot cannot be surprised
by it growing underneath them.
"""

import pytest
from pydantic import ValidationError

from core.audit import AuditLog
from core.models import AuditEntry


def test_sequence_is_monotonic_and_starts_at_zero():
    log = AuditLog(run_id="run-1")
    log.record("BL-1", "match", "deterministic", "T0", "utr hit", 1.0)
    log.record("BL-2", "match", "deterministic", "T2", "net matched", 0.99)
    assert [e.sequence for e in log.entries()] == [0, 1]


def test_entries_are_append_only():
    log = AuditLog(run_id="run-1")
    log.record("BL-1", "match", "deterministic", "T0", "utr hit", 1.0)
    snapshot = log.entries()
    log.record("BL-2", "match", "deterministic", "T2", "net matched", 0.99)
    assert len(snapshot) == 1  # the returned list must be a copy


def test_mutating_the_returned_list_cannot_corrupt_the_log():
    log = AuditLog(run_id="run-1")
    log.record("BL-1", "match", "deterministic", "T0", "utr hit", 1.0)
    log.entries().clear()
    assert len(log.entries()) == 1


def test_record_returns_the_entry_it_appended():
    log = AuditLog(run_id="run-1")
    entry = log.record("BL-1", "ingest", "deterministic", "read", "12 rows", 1.0)
    assert isinstance(entry, AuditEntry)
    assert entry.sequence == 0
    assert entry.run_id == "run-1"
    assert entry.subject_id == "BL-1"
    assert entry.rule == "read"
    assert entry.evidence == "12 rows"


def test_entry_ids_are_unique_and_deterministic():
    """No uuid4, no clock: two runs with the same id and the same calls must
    produce byte-identical entry ids, or a run is not reproducible."""
    first = AuditLog(run_id="run-1")
    second = AuditLog(run_id="run-1")
    for log in (first, second):
        log.record("BL-1", "match", "deterministic", "T0", "hit", 1.0)
        log.record("BL-2", "match", "deterministic", "T3", "delta=50", 0.8)
    ids = [e.entry_id for e in first.entries()]
    assert ids == [e.entry_id for e in second.entries()]
    assert len(set(ids)) == 2


def test_the_log_carries_no_wall_clock_field():
    """Ordering is `sequence`. Timestamps are stamped at the API boundary."""
    log = AuditLog(run_id="run-1")
    entry = log.record("BL-1", "match", "deterministic", "T0", "hit", 1.0)
    assert isinstance(entry, AuditEntry)
    assert not [f for f in AuditEntry.model_fields if "time" in f or "stamp" in f]


def test_len_reports_the_entry_count():
    log = AuditLog(run_id="run-1")
    assert len(log) == 0
    log.record("BL-1", "match", "deterministic", "T0", "hit", 1.0)
    assert len(log) == 1


def test_an_invalid_stage_is_rejected_at_record_time():
    """AuditEntry.stage is a Literal on the frozen contract. Failing here, at
    the call site, beats failing later during serialisation."""
    log = AuditLog(run_id="run-1")
    with pytest.raises(ValidationError):
        log.record("BL-1", "not-a-stage", "deterministic", "T0", "hit", 1.0)


def test_entries_for_returns_only_one_subjects_trail():
    log = AuditLog(run_id="run-1")
    log.record("BL-1", "match", "deterministic", "T0", "hit", 1.0)
    log.record("BL-2", "match", "deterministic", "T0", "miss", 0.0)
    log.record("BL-1", "match", "deterministic", "T2", "hit", 0.99)
    assert [e.rule for e in log.entries_for("BL-1")] == ["T0", "T2"]
