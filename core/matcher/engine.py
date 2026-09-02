"""The deterministic reconciliation engine.

`run_match` builds the candidate pool, runs the tiers in order, and then
partitions everything that is left into typed exceptions.

**The partition invariant is what makes the metrics mean anything.** Every
subject is either matched or excepted, exactly once, never both and never
neither. If a subject can quietly fall out of both sets the rates stop summing
to 1 and the headline number becomes a claim rather than a measurement.

There are three subject universes, and they are deliberately separate:

* **bank lines** -- the reconciliation subjects. These drive the metrics.
* **PSP transactions** -- a suppressed duplicate, a leg in no settlement, or a
  payment leg whose order could not be recovered.
* **orders** -- reserved; the fixture's orders are all reachable through their
  settlements, and an order-side exception would double-count a bank-line
  failure.

The engine takes no clock reading and consults no wall-clock anywhere: audit
ordering is the log's monotonic `sequence`, and a run replays byte for byte.
Throughput is measured by the caller at the boundary, never in here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.audit import AuditLog
from core.canonicalize.txn_types import PAYMENT_TYPE
from core.matcher.pool import CandidatePool
from core.matcher.tiers import TIERS, WINDOW_DAYS
from core.models import (
    AuditEntry,
    BankLine,
    MatchGroup,
    Order,
    PSPTransaction,
    ReasonCode,
    ReconException,
)

DEFAULT_RUN_ID = "run-local"

#: The audit `rule` a suppressed duplicate leg is logged under.
#:
#: Named rather than spelled twice because the trail is the only *persisted*
#: record of which legs the pool discounted: `MatchResult` carries the matches
#: and the exceptions, not the pool, so a reader that has to rebuild a
#: settlement from the stored records -- `core/store/repo.py`, for the
#: settlements listing -- recovers the suppression from here. Two spellings of
#: the string would be two spellings of that fact.
DEDUP_SUPPRESSED_RULE = "dedup:suppressed"


@dataclass
class MatchResult:
    """One run's output: what matched, what did not, and why."""

    run_id: str
    matches: list[MatchGroup] = field(default_factory=list)
    exceptions: list[ReconException] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)
    record_count: int = 0

    @property
    def tier_breakdown(self) -> dict[str, int]:
        counts = {"T0": 0, "T1": 0, "T2": 0, "T3": 0, "LLM": 0}
        for match in self.matches:
            counts[match.tier] += 1
        return counts


def run_match(
    orders: list[Order],
    psp_txns: list[PSPTransaction],
    bank_lines: list[BankLine],
    *,
    run_id: str = DEFAULT_RUN_ID,
) -> MatchResult:
    pool = CandidatePool(orders=orders, psp_txns=psp_txns, bank_lines=bank_lines)
    log = AuditLog(run_id)

    _record_preparation(pool, log)

    matches: list[MatchGroup] = []
    for tier in TIERS:
        matches.extend(tier.match(pool, log))

    exceptions = [
        *_bank_line_exceptions(pool, log),
        *_psp_exceptions(pool, log),
    ]

    return MatchResult(
        run_id=run_id,
        matches=matches,
        exceptions=exceptions,
        audit=log.entries(),
        record_count=len(orders),
    )


# --- preparation --------------------------------------------------------------


def _record_preparation(pool: CandidatePool, log: AuditLog) -> None:
    """Everything the pool derived before a tier ran, into the trail.

    A recovery is recorded whether or not it succeeded: the attempt is part of
    the argument, and a declined recovery is what licenses `MISSING_ORDER_REF`.
    """
    for finding in pool.duplicates:
        log.record(
            finding.duplicate_id,
            "ingest",
            "deterministic",
            DEDUP_SUPPRESSED_RULE,
            finding.evidence,
            1.0,
        )
    for twin in pool.cross_settlement_twins:
        # Not an exception: neither row is wrong. Logged so that declining to
        # suppress is a stated decision rather than a silence.
        log.record(
            twin.txn_ids[0],
            "ingest",
            "deterministic",
            "dedup:cross-settlement-kept",
            twin.evidence,
            1.0,
        )
    for recovery in pool.order_recoveries:
        log.record(
            recovery.txn_id,
            "canonicalize",
            "deterministic",
            "order-recovery:recovered"
            if recovery.recovered
            else "order-recovery:declined",
            recovery.evidence,
            1.0 if recovery.recovered else 0.0,
        )
    for txn_id, dangling in pool.dangling_order_refs.items():
        log.record(
            txn_id,
            "canonicalize",
            "deterministic",
            "order-ref:dangling",
            f"{txn_id} references {dangling}, which is not in the order register; "
            f"excluded from the settlement's order set",
            0.0,
        )
    for line in pool.bank_lines:
        narration = pool.narration(line)
        log.record(
            line.line_id,
            "canonicalize",
            "deterministic",
            "narration:parsed",
            f"squashed {narration.squashed!r}; settlement reference "
            f"{narration.settlement_id!r}; utr {line.utr!r}; entity "
            f"{narration.entity!r} (evidence only, never a matching criterion)",
            1.0,
        )


# --- exception partition ------------------------------------------------------


def _bank_line_exceptions(pool: CandidatePool, log: AuditLog) -> list[ReconException]:
    out: list[ReconException] = []
    for line in pool.bank_lines:
        if pool.is_matched(line.line_id):
            continue
        reason, evidence = _classify(pool, line)
        log.record(
            line.line_id, "match", "deterministic", f"exception:{reason.value}", evidence, 0.0
        )
        out.append(
            _exception(
                subject_type="bank_line",
                subject_id=line.line_id,
                reason=reason,
                amount=line.credit if line.credit is not None else (line.debit or 0),
            )
        )
    return out


def _classify(pool: CandidatePool, line: BankLine) -> tuple[ReasonCode, str]:
    """Why this bank line was not matched.

    The ladder is ordered from most to least specific, and the first rung is
    the one that matters: a subject the data could not decide must never be
    reported as anything softer, because that is the honesty metric.
    """
    if pool.was_undecidable(line.line_id):
        candidates = pool.ambiguous_candidates(line.line_id)
        rivals = pool.contested_with(line.line_id)
        if candidates:
            detail = (
                f"{len(candidates)} settlements {sorted(candidates)} satisfy the "
                f"arithmetic and the date window; the data does not determine which"
            )
        else:
            detail = (
                f"the only candidate settlement is also the only candidate of "
                f"{sorted(rivals)}; matching either would make iteration order the "
                f"tie-breaker"
            )
        return ReasonCode.AMBIGUOUS_MULTI_CANDIDATE, detail

    referenced = pool.referenced_settlement(line)
    if referenced is not None:
        net = pool.totals(referenced).net
        delta = net - (line.credit or 0)
        return (
            ReasonCode.AMOUNT_MISMATCH,
            f"reference {referenced} hit, but it reconstructs to {net} paise "
            f"against credit {line.credit} -- residual delta={delta} paise "
            f"(net - credit), outside every tier's tolerance",
        )

    if line.credit is None:
        return (
            ReasonCode.ORPHAN_BANK_LINE,
            f"line carries a debit of {line.debit} paise, not a settlement credit",
        )

    # The same predicate the scan applied -- `within_window` over the unclaimed
    # set -- asked of the pool's settled-date index instead of tested against
    # every settlement. This ran for every unmatched bank line, so it was the
    # last quadratic term left in a run after the tier indexes landed, and
    # `len(nearby)` is quoted verbatim in two reason-code evidence lines below:
    # the count has to be the same count, not a similar one.
    nearby = pool.unclaimed_settlements_in_window(line.txn_date, WINDOW_DAYS)
    if not nearby:
        return (
            ReasonCode.ORPHAN_BANK_LINE,
            f"no unclaimed settlement settles within {WINDOW_DAYS} days of "
            f"{line.txn_date.isoformat()}",
        )

    narration = pool.narration(line)
    if narration.is_unparseable:
        return (
            ReasonCode.UNPARSEABLE_NARRATION,
            f"narration {narration.squashed!r} yields no settlement reference, no "
            f"utr and no entity, so nothing narrows the {len(nearby)} settlement(s) "
            f"in the date window",
        )
    return (
        ReasonCode.NO_SETTLEMENT_REF,
        f"narration parsed (entity {narration.entity!r}) but carries no settlement "
        f"reference present in the PSP report; {len(nearby)} settlement(s) sit in "
        f"the date window and none reconstructs to {line.credit} paise",
    )


def _psp_exceptions(pool: CandidatePool, log: AuditLog) -> list[ReconException]:
    """One exception per PSP row that could not be placed.

    A row gets at most one, in priority order: a duplicate is a duplicate first
    (its twin is already in a batch, so calling it an orphan would be wrong),
    then an unsettled row is an orphan, then a settled `payment` leg whose
    order could not be recovered is a missing reference.
    """
    out: list[ReconException] = []
    seen: set[str] = set()

    for finding in pool.duplicates:
        seen.add(finding.duplicate_id)
        out.append(
            _exception(
                subject_type="psp_txn",
                subject_id=finding.duplicate_id,
                reason=ReasonCode.DUPLICATE_PSP_TXN,
                amount=pool.txns_by_id[finding.duplicate_id].amount,
            )
        )
        log.record(
            finding.duplicate_id,
            "match",
            "deterministic",
            "exception:DUPLICATE_PSP_TXN",
            finding.evidence,
            0.0,
        )

    for txn in pool.active_txns():
        if txn.txn_id in seen or txn.settlement_id:
            continue
        seen.add(txn.txn_id)
        out.append(
            _exception(
                subject_type="psp_txn",
                subject_id=txn.txn_id,
                reason=ReasonCode.ORPHAN_PSP_TXN,
                amount=txn.amount,
            )
        )
        log.record(
            txn.txn_id,
            "match",
            "deterministic",
            "exception:ORPHAN_PSP_TXN",
            f"{txn.txn_id} carries no settlement_id, so it is in no batch and no "
            f"bank line can account for it",
            0.0,
        )

    for recovery in pool.order_recoveries:
        if recovery.recovered or recovery.txn_id in seen:
            continue
        txn = pool.txns_by_id[recovery.txn_id]
        if txn.txn_type != PAYMENT_TYPE:
            continue
        seen.add(recovery.txn_id)
        out.append(
            _exception(
                subject_type="psp_txn",
                subject_id=recovery.txn_id,
                reason=ReasonCode.MISSING_ORDER_REF,
                amount=txn.amount,
            )
        )
        log.record(
            recovery.txn_id,
            "match",
            "deterministic",
            "exception:MISSING_ORDER_REF",
            recovery.evidence,
            0.0,
        )

    return out


def _exception(
    *, subject_type: str, subject_id: str, reason: ReasonCode, amount: int
) -> ReconException:
    return ReconException(
        exception_id=f"exc-{subject_id}",
        subject_type=subject_type,
        subject_id=subject_id,
        reason_code=reason,
        amount=amount,
        llm_hypothesis=None,
        verifier_verdict="not_attempted",
        verifier_reason=None,
        failed_check=None,
    )
