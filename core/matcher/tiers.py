"""The deterministic matching tiers, T0 to T3 (spec 7).

Every tier below T0 reconstructs a settlement from its legs. That is
unavoidable: a bank credit is always net of fees, so no tier can match on a raw
amount. **Method therefore cannot distinguish T1 from T2** -- the distinction is
payment-leg **cardinality**, and cardinality is a label, not a filter.

Three rules are non-negotiable, in the order they cost most if they are wrong.

**1. Candidacy is cardinality-blind, and more than one candidate matches
nothing.** A subject's candidate set is every unclaimed settlement that
satisfies the arithmetic and the date window, at any payment-leg count. The
ambiguity rule is applied to that whole set. Only once a single candidate
survives does its payment-leg count decide whether the match is labelled T1 or
T2. Partitioning the pool by cardinality first turns the split into a
tie-breaker: two settlements that close the same credit but differ in
cardinality would each look unique inside their own partition, both subjects
would be matched, and every individual rule would still read as correct while
the whole point of the exercise quietly disappeared.

The mirror image is enforced too: when one settlement is the only candidate of
two different subjects, matching either would make iteration order the
tie-breaker, so neither is matched.

**2. T0 needs the arithmetic as well as the reference.** A settlement id in a
narration is evidence of *identity*, never a substitute for the sum. A
reference match whose reconstructed net does not equal the bank credit falls
through -- reaching T3 if it is within tolerance, and an exception if it is
not. Without that clause T0 claims a rounding break at confidence 1.00 and
writes a match whose `net` is not the bank line credit, breaking the invariant
in core/models.py, spec 6.3 and api/openapi.yaml.

**3. `MatchGroup.net` is the bank line credit.** On a T3 break the
reconstruction and the credit disagree by design; the residual is recorded in
evidence, never folded into the net.

The residual convention is `net - credit`, in that order, everywhere: positive
means the bank credited less than the reconstruction.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from core.audit import AuditLog
from core.matcher.batch import BatchTotals
from core.matcher.pool import CandidatePool
from core.models import BankLine, MatchGroup

#: `settled_at` must sit within this many days of the bank line's `txn_date`
#: for a settlement to be a candidate at T1, T2 or T3 (spec 7).
WINDOW_DAYS = 2

#: T3's tolerance, in paise. A half-rupee rounding break is a match at 0.80; a
#: rupee-and-a-half break is an exception.
TOLERANCE_PAISE = 100


@dataclass(frozen=True)
class Candidate:
    settlement_id: str
    totals: BatchTotals
    delta: int  # net - credit, in that order
    payment_legs: int


class _Tier:
    """Shared machinery: candidate search, ambiguity, contest, match building.

    Subclasses supply the candidate search and, for T1 and T2, the cardinality
    *label* test that runs only after a single candidate has survived.
    """

    name: str
    confidence: float

    def _candidates(
        self, pool: CandidatePool, line: BankLine, log: AuditLog
    ) -> list[Candidate]:
        raise NotImplementedError

    def _accepts_cardinality(self, candidate: Candidate) -> bool:
        return True

    def _cardinality_rule(self) -> str:
        return "any payment-leg count"

    def _evidence(
        self, pool: CandidatePool, line: BankLine, candidate: Candidate
    ) -> list[str]:
        return []

    # --- the tier loop --------------------------------------------------------

    def match(self, pool: CandidatePool, log: AuditLog) -> list[MatchGroup]:
        proposals: dict[str, Candidate] = {}

        for line in pool.open_bank_lines():
            if line.credit is None:
                continue
            candidates = self._candidates(pool, line, log)

            if not candidates:
                self._record(
                    log,
                    line,
                    "no-candidate",
                    f"no unclaimed settlement satisfies {self.name}",
                )
                continue

            if len(candidates) > 1:
                ids = [c.settlement_id for c in candidates]
                pool.note_ambiguity(line.line_id, ids)
                # This is the line a maintainer reads to confirm that candidacy
                # is cardinality-blind, so it must show the candidates' ACTUAL
                # payment-leg counts. Appending the tier's own label rule to
                # this sentence read as though the set had been filtered by it,
                # which is the exact opposite of what happened -- and on the
                # trap the counts differ, so the claim was also false.
                shown = ", ".join(
                    f"{c.settlement_id} ({c.payment_legs} payment leg"
                    f"{'' if c.payment_legs == 1 else 's'})"
                    for c in sorted(candidates, key=lambda c: c.settlement_id)
                )
                self._record(
                    log,
                    line,
                    "ambiguous",
                    f"{len(ids)} equally valid candidates, searched at any "
                    f"payment-leg count: {shown}. None was filtered out by "
                    f"cardinality -- {self.name} labels {self._cardinality_rule()}, "
                    f"and that test runs only after a single candidate survives. "
                    f"More than one did, so this subject matches nothing",
                )
                continue

            candidate = candidates[0]
            if not self._accepts_cardinality(candidate):
                self._record(
                    log,
                    line,
                    "cardinality-declined",
                    f"unique candidate {candidate.settlement_id} has "
                    f"{candidate.payment_legs} payment leg(s); {self.name} labels "
                    f"{self._cardinality_rule()}",
                )
                continue

            proposals[line.line_id] = candidate

        return self._resolve(pool, proposals, log)

    def _resolve(
        self,
        pool: CandidatePool,
        proposals: dict[str, Candidate],
        log: AuditLog,
    ) -> list[MatchGroup]:
        """Reject any proposal whose settlement two subjects both want."""
        wanted: dict[str, list[str]] = defaultdict(list)
        for line_id, candidate in proposals.items():
            wanted[candidate.settlement_id].append(line_id)

        lines_by_id = {line.line_id: line for line in pool.bank_lines}
        matches: list[MatchGroup] = []

        for line_id, candidate in proposals.items():
            rivals = wanted[candidate.settlement_id]
            line = lines_by_id[line_id]
            if len(rivals) > 1:
                pool.note_contest(line_id, sorted(rivals))
                self._record(
                    log,
                    line,
                    "contested",
                    f"settlement {candidate.settlement_id} is the only candidate of "
                    f"{sorted(rivals)} -- matching nothing",
                )
                continue
            matches.append(self._build(pool, line, candidate, log))

        return matches

    def _build(
        self,
        pool: CandidatePool,
        line: BankLine,
        candidate: Candidate,
        log: AuditLog,
    ) -> MatchGroup:
        totals = candidate.totals
        narration = pool.narration(line)
        credit = line.credit or 0
        legs = pool.legs(candidate.settlement_id)

        evidence = [
            f"{self.name}: settlement {candidate.settlement_id} reconstructs to "
            f"{totals.net} paise from {len(legs)} legs",
            f"bank line {line.line_id} credit {credit} paise on "
            f"{line.txn_date.isoformat()}",
            f"payment legs {candidate.payment_legs} -- labelled {self.name}",
            *self._evidence(pool, line, candidate),
        ]
        if narration.entity:
            evidence.append(
                f"narration entity {narration.entity!r} recorded as evidence only; "
                f"it is not a matching criterion"
            )
        # Both of these used to be a linear filter of a whole-run list, run once
        # per match built: every order recovery, and every dangling reference
        # crossed with every leg of this settlement. The pool groups them by
        # settlement at construction instead, in the same order, so the evidence
        # block reads identically -- the audit trail is the product and its
        # wording is not an implementation detail.
        for recovery in pool.recoveries_for(candidate.settlement_id):
            evidence.append(recovery.evidence)
        for txn_id, dangling in pool.dangling_refs_for(candidate.settlement_id):
            evidence.append(
                f"{txn_id} references {dangling}, which is not in the order "
                f"register -- excluded from the order set"
            )

        match = MatchGroup(
            match_id=f"match-{line.line_id}",
            bank_line_id=line.line_id,
            settlement_id=candidate.settlement_id,
            psp_txn_ids=[t.txn_id for t in legs],
            order_ids=pool.order_ids(candidate.settlement_id),
            gross=totals.gross,
            fees=totals.fees,
            tax=totals.tax,
            refunds=totals.refunds,
            holds=totals.holds,
            # The invariant is `net == bank credit`. On a T3 break the
            # reconstruction is 50 paise away and the residual lives in
            # evidence -- it is never folded into this field.
            net=credit,
            tier=self.name,
            confidence=self.confidence,
            evidence=evidence,
        )
        pool.claim(line.line_id, candidate.settlement_id)
        log.record(
            line.line_id,
            "match",
            "deterministic",
            f"{self.name}:matched",
            "; ".join(evidence),
            self.confidence,
        )
        return match

    def _record(self, log: AuditLog, line: BankLine, rule: str, evidence: str) -> None:
        log.record(
            line.line_id, "match", "deterministic", f"{self.name}:{rule}", evidence, 0.0
        )


# --- T0 -----------------------------------------------------------------------


class _T0(_Tier):
    """Reference hit AND exact arithmetic. Confidence 1.00.

    T0 does not apply the date window: an explicit settlement reference plus an
    exactly closing sum is not made more or less certain by a late posting.
    """

    name = "T0"
    confidence = 1.00

    def _cardinality_rule(self) -> str:
        return "any payment-leg count"

    def _candidates(
        self, pool: CandidatePool, line: BankLine, log: AuditLog
    ) -> list[Candidate]:
        settlement_id = pool.referenced_settlement(line)
        if settlement_id is None:
            return []
        if pool.is_claimed(settlement_id):
            self._record(
                log,
                line,
                "reference-claimed",
                f"reference {settlement_id} hit but that settlement is already "
                f"matched to another bank line",
            )
            return []

        totals = pool.totals(settlement_id)
        delta = totals.net - (line.credit or 0)
        if delta != 0:
            self._record(
                log,
                line,
                "reference-hit-arithmetic-declined",
                f"reference {settlement_id} hit, but it reconstructs to {totals.net} "
                f"paise against credit {line.credit} -- residual delta={delta} paise "
                f"(net - credit). A settlement id proves identity, not arithmetic, "
                f"so {self.name} declines and the line falls through",
            )
            return []

        return [
            Candidate(
                settlement_id=settlement_id,
                totals=totals,
                delta=0,
                payment_legs=pool.payment_legs(settlement_id),
            )
        ]

    def _evidence(
        self, pool: CandidatePool, line: BankLine, candidate: Candidate
    ) -> list[str]:
        source = "narration" if pool.narration(line).settlement_id else "utr column"
        return [
            f"settlement reference {candidate.settlement_id} found in the {source} "
            f"and present in the PSP report",
            "reconstructed net equals the bank credit exactly (residual delta=0)",
        ]


# --- T1 / T2 / T3 -------------------------------------------------------------


class _ReconstructionTier(_Tier):
    """Shared candidate search for the tiers that rebuild from legs.

    The search is deliberately identical for T1, T2 and T3 apart from the
    tolerance. Cardinality is applied afterwards, to a set of exactly one.
    """

    tolerance: int

    def _candidates(
        self, pool: CandidatePool, line: BankLine, log: AuditLog
    ) -> list[Candidate]:
        """Every unclaimed settlement satisfying the arithmetic and the date
        window, at any payment-leg count.

        This used to walk the whole unclaimed pool for every bank line, and
        `unclaimed_settlements()` re-sorted that pool on every call -- three
        times per subject, once for each of T1, T2 and T3. It is an index
        lookup now (spec §8), and the set it produces is unchanged: the pool's
        `unclaimed_settlements_near_net` answers exactly the predicate the scan
        applied, inclusive at both tolerance edges, and returns **all** the
        settlements at a matching net rather than the first.

        That last part is the whole safety argument. The set this returns is
        what the ambiguity rule counts, so a lookup that stopped early would
        turn two candidates into one and a declined subject into a false match,
        with every rule below still reading as correct. The index is keyed on
        the net alone for the mirror-image reason: cardinality labels the
        winner and must never narrow the field.
        """
        credit = line.credit or 0
        found: list[Candidate] = []
        for settlement_id in pool.unclaimed_settlements_near_net(credit, self.tolerance):
            if not pool.within_window(settlement_id, line.txn_date, WINDOW_DAYS):
                continue
            totals = pool.totals(settlement_id)
            found.append(
                Candidate(
                    settlement_id=settlement_id,
                    totals=totals,
                    delta=totals.net - credit,
                    payment_legs=pool.payment_legs(settlement_id),
                )
            )
        return found

    def _evidence(
        self, pool: CandidatePool, line: BankLine, candidate: Candidate
    ) -> list[str]:
        settled = pool.settled_on(candidate.settlement_id)
        return [
            f"only unclaimed settlement within tolerance {self.tolerance} paise and "
            f"{WINDOW_DAYS} days, at any payment-leg count",
            f"settled_at {settled.isoformat() if settled else 'unknown'} is within "
            f"{WINDOW_DAYS} days of bank date {line.txn_date.isoformat()}",
            f"residual delta={candidate.delta} paise (net - credit)",
        ]


class _T1(_ReconstructionTier):
    """Exactly one `payment` leg; exact arithmetic; date window. Confidence 0.95.

    `fee`, `tax`, `refund`, `chargeback`, `reserve` and `adjustment` legs do not
    count toward the cardinality -- a settlement with one payment, one fee and
    one tax leg settles one order, and the deduction legs are the arithmetic,
    not the batch.
    """

    name = "T1"
    confidence = 0.95
    tolerance = 0

    def _accepts_cardinality(self, candidate: Candidate) -> bool:
        return candidate.payment_legs == 1

    def _cardinality_rule(self) -> str:
        return "exactly one payment leg"


class _T2(_ReconstructionTier):
    """Two or more `payment` legs; otherwise identical to T1. Confidence 0.99.

    The genuine many-to-one batch, and the core of the project: it is the only
    tier that solves the actual problem shape.
    """

    name = "T2"
    confidence = 0.99
    tolerance = 0

    def _accepts_cardinality(self, candidate: Candidate) -> bool:
        return candidate.payment_legs >= 2

    def _cardinality_rule(self) -> str:
        return "two or more payment legs"


class _T3(_ReconstructionTier):
    """Within tolerance at ANY payment-leg count. Confidence 0.80.

    Cardinality-agnostic on purpose: restricting T3 to T2's cardinality would
    strand a single-payment-leg settlement carrying a rounding break, which is
    exactly the shape the fixture's `rounding_break` defect has.
    """

    name = "T3"
    confidence = 0.80
    tolerance = TOLERANCE_PAISE


T0 = _T0()
T1 = _T1()
T2 = _T2()
T3 = _T3()

#: Tier order. A subject matched at an earlier tier is removed from the pool
#: before the next one runs.
TIERS: tuple[_Tier, ...] = (T0, T1, T2, T3)
