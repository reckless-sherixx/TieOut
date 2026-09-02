"""Append-only audit log.

Every rule a tier fires -- matched or not -- lands here, which is what lets the
UI explain a decision after the fact and what lets a reviewer check that a
declined match was declined for the stated reason rather than by accident.

Two invariants:

* **Ordering is a monotonic `sequence` int, never a clock.** No wall-clock
  inside `core/` (global constraint): timestamps are stamped at the API
  boundary. `entry_id` is derived from `run_id` and `sequence` rather than
  from `uuid4`, so replaying a run reproduces the log byte for byte.
* **`entries()` returns a copy.** A caller holding a snapshot must not see it
  grow, and mutating what they were handed must not corrupt the log.
"""

from __future__ import annotations

from core.models import AuditEntry


class AuditLog:
    """A run's ordered trail of deterministic decisions."""

    def __init__(self, run_id: str, start_sequence: int = 0) -> None:
        """`start_sequence` lets a second stage CONTINUE a run's trail.

        The LLM pass runs after the engine has returned, so its entries need
        sequences that carry on from the engine's rather than restarting at 0 --
        `entry_id` is derived from `run_id` and `sequence`, so a restart would
        mint duplicate ids within one run and put two entries at the same point
        in an ordering that is supposed to be total. Defaults to 0, which is
        every existing caller.
        """
        self.run_id = run_id
        self._start = start_sequence
        self._entries: list[AuditEntry] = []

    def record(
        self,
        subject_id: str,
        stage: str,
        actor: str,
        rule: str,
        evidence: str,
        confidence: float,
    ) -> AuditEntry:
        """Append one entry and return it.

        `stage` and `actor` are validated against the Literals on the frozen
        `AuditEntry` contract at the call site, so a typo fails here rather
        than at serialisation time three layers away.
        """
        sequence = self._start + len(self._entries)
        entry = AuditEntry(
            entry_id=f"{self.run_id}-{sequence:06d}",
            run_id=self.run_id,
            subject_id=subject_id,
            stage=stage,
            actor=actor,
            rule=rule,
            evidence=evidence,
            confidence=float(confidence),
            sequence=sequence,
        )
        self._entries.append(entry)
        return entry

    def entries(self) -> list[AuditEntry]:
        """A copy of the trail, in `sequence` order."""
        return list(self._entries)

    def entries_for(self, subject_id: str) -> list[AuditEntry]:
        """The trail for one subject, in `sequence` order."""
        return [e for e in self._entries if e.subject_id == subject_id]

    def __len__(self) -> int:
        return len(self._entries)
