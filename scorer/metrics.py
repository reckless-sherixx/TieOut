"""Ground truth, the per-subject comparison, and the metric arithmetic.

`scorer/` is the only module in the repository that opens the ground-truth
file. The matcher produces a `MatchResult` having never seen the answers, and
this module grades it. That one-directional dependency is the credibility
argument for every number the project reports, and `tests/test_boundaries.py`
proves the matcher end of it statically.

Definitions are spec section 9, copied here so they cannot drift:

| Metric | Definition |
|---|---|
| `auto_match_rate` | subjects matched by T0-T3 / total resolvable subjects |
| `assisted_match_rate` | subjects matched via an accepted LLM hypothesis / total resolvable |
| `exception_rate` | unmatched subjects / total subjects |
| `false_match_rate` | matches disagreeing with truth / total matches produced |
| `precision` | `1 - false_match_rate` |
| `recall_on_resolvable` | correctly matched / truth-resolvable subjects |
| `trap_capture_rate` | `unresolvable_ids` correctly left unmatched / total unresolvable |
| `llm_rejection_rate` | hypotheses rejected by the verifier / hypotheses proposed |
| `tier_counts` | matches produced per tier, all five keys always present |

Four conventions the table does not spell out:

* **The subject is the bank line.** Linkages and `unresolvable_ids` are both
  keyed on `bank_line_id`, so that is the unit the rates count. PSP-side and
  order-side exceptions are diagnostics; folding them into the denominator
  would let a single bank-line failure be counted twice.

  Truth keys linkages on `bank_line_id`, never on `settlement_id`, and that is
  load-bearing: `split_settlement` pays one settlement across two bank lines and
  emits **two linkage entries sharing one `settlement_id`** (CSV_SCHEMAS 5,
  "one entry per bank line"). A scorer keying them by settlement would silently
  collapse the pair and lose half the subjects.

* **Every denominator comes from truth, never from the result being graded.**
  A rate whose denominator is derived from the thing under test is not a
  measurement. `exception_rate` used to divide by
  `len(matched | bank_exceptions)`, so a subject the engine dropped from both
  sets left the denominator along with it and the rate *improved* -- an engine
  could raise its score by losing work.
* **A numerator counted over subjects must be restricted to the subjects in its
  own denominator.** The match rates divide by *resolvable* subjects, so a
  match on an unresolvable one cannot be in the numerator: counting raw matches
  let a run report `auto_match_rate = 1.5`, and `Metrics` promises 0.0-1.0 with
  no range check to enforce it.
* **Every rate with an empty denominator is 0.0, except `trap_capture_rate`,
  which is 1.0.** Declining to resolve a trap that does not exist is not a
  failure, and an empty run must not read as a hallucination.

`false_match_rate` follows the definition literally: a match is false when it
disagrees with the recorded linkage. A match on an *unresolvable* subject that
happens to agree is therefore not counted here -- it is caught by
`trap_capture_rate`, which counts only whether the trap lines were left alone.
That split is deliberate: the two metrics measure different failures, and
collapsing them would hide which one occurred.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DETERMINISTIC_TIERS = frozenset({"T0", "T1", "T2", "T3"})
ASSISTED_TIER = "LLM"


@dataclass(frozen=True)
class Linkage:
    """One bank line's recorded answer."""

    bank_line_id: str
    settlement_id: str
    psp_txn_ids: frozenset[str]
    order_ids: frozenset[str]


@dataclass(frozen=True)
class GroundTruth:
    seed: int
    record_count: int
    linkages: dict[str, Linkage]
    unresolvable_ids: frozenset[str]

    @property
    def resolvable_ids(self) -> frozenset[str]:
        return frozenset(self.linkages) - self.unresolvable_ids


def load(path: Path | str) -> GroundTruth:
    """Read the ground-truth file. The only file read in this package."""
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)

    linkages = {
        entry["bank_line_id"]: Linkage(
            bank_line_id=entry["bank_line_id"],
            settlement_id=entry["settlement_id"],
            psp_txn_ids=frozenset(entry["psp_txn_ids"]),
            order_ids=frozenset(entry["order_ids"]),
        )
        for entry in payload["linkages"]
    }
    return GroundTruth(
        seed=payload["seed"],
        record_count=payload["record_count"],
        linkages=linkages,
        unresolvable_ids=frozenset(payload["unresolvable_ids"]),
    )


@dataclass
class Comparison:
    """Per-subject verdicts, so a metric can always be traced to its subjects."""

    agreed: set[str] = field(default_factory=set)
    disagreed: dict[str, str] = field(default_factory=dict)
    matched_subjects: set[str] = field(default_factory=set)
    #: Subjects matched deterministically / by an accepted hypothesis. Sets, not
    #: counts, and intersected with truth before they become a rate: the
    #: denominators are subject counts, so the numerators must be too.
    deterministic_subjects: set[str] = field(default_factory=set)
    assisted_subjects: set[str] = field(default_factory=set)
    deterministic_matches: int = 0
    assisted_matches: int = 0
    total_matches: int = 0
    #: Subjects truth knows about that the run neither matched nor excepted.
    #: Not an error the engine reports -- an error in the engine's reporting.
    unaccounted_subjects: set[str] = field(default_factory=set)
    total_subjects: int = 0
    excepted_subjects: int = 0
    traps_left_alone: int = 0
    total_traps: int = 0

    def unmatched_in_truth(self, truth: GroundTruth) -> set[str]:
        """Every subject truth records that the run did not match.

        This is `exception_rate`'s numerator, and it deliberately does not ask
        the result whether it raised an exception. A subject the engine excepts
        and a subject the engine silently loses are the same failure from the
        outside: the bank line was not reconciled. Counting only the ones the
        engine chose to admit to lets it improve its score by losing work.
        """
        return set(truth.linkages) - self.matched_subjects


def disagreement(match, truth: GroundTruth) -> str | None:
    """Why this match disagrees with the recorded linkage, or None if it agrees.

    All three of settlement, PSP legs and orders must agree. The order set is
    part of the test on purpose: a matcher that scrapes `order_id` off the PSP
    rows omits an order recovered from a leg that names none, and would
    otherwise be scored correct for producing an incomplete answer.
    """
    linkage = truth.linkages.get(match.bank_line_id)
    if linkage is None:
        return f"no recorded linkage for {match.bank_line_id}"
    if match.settlement_id != linkage.settlement_id:
        return (
            f"settlement {match.settlement_id!r} != {linkage.settlement_id!r}"
        )
    if frozenset(match.psp_txn_ids) != linkage.psp_txn_ids:
        return (
            f"psp legs differ: extra {sorted(frozenset(match.psp_txn_ids) - linkage.psp_txn_ids)}, "
            f"missing {sorted(linkage.psp_txn_ids - frozenset(match.psp_txn_ids))}"
        )
    if frozenset(match.order_ids) != linkage.order_ids:
        return (
            f"order set differs: extra {sorted(frozenset(match.order_ids) - linkage.order_ids)}, "
            f"missing {sorted(linkage.order_ids - frozenset(match.order_ids))}"
        )
    return None


def compare(result, truth: GroundTruth) -> Comparison:
    comparison = Comparison(total_traps=len(truth.unresolvable_ids))

    for match in result.matches:
        comparison.total_matches += 1
        comparison.matched_subjects.add(match.bank_line_id)
        if match.tier in DETERMINISTIC_TIERS:
            comparison.deterministic_matches += 1
            comparison.deterministic_subjects.add(match.bank_line_id)
        elif match.tier == ASSISTED_TIER:
            comparison.assisted_matches += 1
            comparison.assisted_subjects.add(match.bank_line_id)

        reason = disagreement(match, truth)
        if reason is None:
            comparison.agreed.add(match.bank_line_id)
        else:
            comparison.disagreed[match.bank_line_id] = reason

    bank_exceptions = {
        exception.subject_id
        for exception in result.exceptions
        if exception.subject_type == "bank_line"
    }
    comparison.excepted_subjects = len(bank_exceptions)
    # Truth is the universe, not the result. `len(matched | excepted)` was
    # derived from the very thing being graded, so a subject the engine dropped
    # from both sets left the denominator with it and the rate improved.
    comparison.total_subjects = len(truth.linkages)
    comparison.unaccounted_subjects = (
        set(truth.linkages) - comparison.matched_subjects - bank_exceptions
    )
    comparison.traps_left_alone = len(
        truth.unresolvable_ids - comparison.matched_subjects
    )
    return comparison


def rate(numerator: float, denominator: float, *, empty: float = 0.0) -> float:
    """A rate, with the empty denominator spelled out rather than assumed."""
    if denominator == 0:
        return empty
    return numerator / denominator
