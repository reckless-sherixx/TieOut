"""The deterministic verifier (spec §8.2) -- the load-bearing component.

The project's whole claim is that the deterministic engine does all money
arithmetic and the LLM never touches it. This module is the half that makes
that true: every hypothesis an analyst proposes is re-checked here, in pure
Python, before anything is accepted.

Six checks over the five frozen labels, in this order, and **all must hold**:

    existence -> exclusivity -> causality -> arithmetic -> coherence -> uniqueness

`existence` runs first so no later check can index the context and raise;
`uniqueness` runs last because it is the only check that is not about the
proposed set at all, and there is no point enumerating alternatives to a
hypothesis that already failed on its own terms.

`coherence` sits between `arithmetic` and `uniqueness` and reports under the
`existence` label. It requires the proposal to BE a settlement -- one
settlement, all of its legs -- because every other check is blind to a
cherry-picked leg set that closes the arithmetic without corresponding to
anything the PSP actually paid out. See `_coherence` for why it cannot report
`uniqueness` instead.

`Hypothesis.self_confidence` is **never read**. It is not an input to
acceptance, not a tie-breaker and not a threshold -- there is a test pinning
that a hypothesis with `self_confidence=1.0` is rejected exactly as hard as one
with 0.1.

A rejected hypothesis is a feature, not a failure: it is the visible evidence
that the guardrail fires, and `llm_rejection_rate` reports it explicitly.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from core.matcher.batch import reconstruct  # arithmetic only; no matcher state
from core.models import BankLine, Hypothesis, PSPTransaction

#: The T3 break tolerance, in paise, and the same integer as
#: `core.matcher.tiers.TOLERANCE_PAISE`. An LLM proposal does not get a wider
#: AMOUNT window than deterministic code would have taken.
#:
#: This comment used to claim the verifier was "exactly as forgiving as the
#: loosest deterministic tier and no more", full stop. That was false, and
#: stating the scope is the fix: it is true of the amount tolerance, which is
#: this constant, and it is NOT true of the date window, which `_causality`
#: deliberately does not apply -- see that function, and `DEFECT-CLOSEOUT-REPORT.md`
#: D1 for the measurement behind the word "deliberately".
#:
#: The two paths are not ordered by permissiveness in either direction. This one
#: is wider on the date and strictly narrower on structure: no tier requires a
#: proposal to be a complete settlement (`_coherence`), and no tier counts
#: competing candidates across the whole file rather than within its own window
#: (`_uniqueness`). Claiming a blanket "no more permissive" in either direction
#: is the thing to keep out of this file.
TOLERANCE_PAISE = 100

#: The five check names, spelled as `ReconException.failed_check` spells them.
#: Lane D copies `VerifyResult.failed_check` straight onto the exception and
#: Lane E renders it as a typed field, so a synonym or a capitalisation
#: difference here is a silent integration break.
CheckName = Literal["existence", "exclusivity", "causality", "arithmetic", "uniqueness"]

#: One check: takes the hypothesis and the context, returns (passed, reason).
Check = Callable[["Hypothesis", "VerifyContext"], tuple[bool, str]]


@dataclass(frozen=True)
class VerifyResult:
    accepted: bool
    reason: str  #: free text, for a human
    failed_check: CheckName | None  #: machine-readable; None iff accepted


@dataclass(frozen=True)
class VerifyContext:
    """Everything the verifier is allowed to see, assembled by the caller.

    The verifier never reaches into the engine -- no `MatchResult`, no tiers, no
    pool. Four fields, not three: `txns_by_settlement` maps every
    `settlement_id` in the ingested PSP data to that settlement's legs, and it
    is what makes `uniqueness` expressible. Populate it from the ingested rows
    including already-matched settlements; `claimed_txn_ids` is what excludes
    those, and doing the exclusion twice in two places is how two lanes drift
    apart. Populated as `{}`, `uniqueness` passes vacuously on every hypothesis
    with no test anywhere going red.
    """

    txns_by_id: dict[str, PSPTransaction]
    bank_lines_by_id: dict[str, BankLine]
    claimed_txn_ids: set[str] = field(default_factory=set)
    txns_by_settlement: dict[str, list[PSPTransaction]] = field(default_factory=dict)


def _existence(h: Hypothesis, ctx: VerifyContext) -> tuple[bool, str]:
    """Runs FIRST so every later check can index `ctx` without a KeyError -- and
    that guarantee only holds if the BANK LINE is validated here too.

    `_causality`, `_arithmetic` and `_uniqueness` all do
    `ctx.bank_lines_by_id[h.proposed_bank_line_id]`, and
    `Hypothesis.proposed_bank_line_id` is `str | None`. A null or hallucinated
    bank line id must be REJECTED here, not raised three checks later: a crash
    in the verifier takes the whole reconciliation run down over one bad
    hypothesis, and an invented id is exactly what a model produces.
    """
    line_id = h.proposed_bank_line_id
    if line_id is None:
        return False, "no proposed_bank_line_id"
    if line_id not in ctx.bank_lines_by_id:
        return False, f"unknown bank line id: {line_id!r}"
    # `credit` is `Money | None`. Reading it as `credit or 0` made a debit-only
    # line a target of zero, which an empty proposal closes exactly -- and the
    # prompt renders debit lines, so the analyst is invited to propose against
    # them. There is nothing for a settlement to reconcile against here.
    # Rejecting it also lets `_arithmetic` and `_uniqueness` treat the credit as
    # an int rather than coercing a None away.
    line = ctx.bank_lines_by_id[line_id]
    if line.credit is None:
        return False, f"{line_id} has no credit to reconcile against"
    # A zero or negative credit is a zero-or-worse target: a settlement whose
    # legs cancel closes it exactly, as does a negative net against a negative
    # credit. `BankLine` carries no non-negative validator and the ingest reader
    # accepts "0", so nothing upstream forbids either.
    if line.credit <= 0:
        return False, f"{line_id} credits {line.credit}, which is not a positive amount"
    if not h.proposed_psp_txn_ids:
        return False, "the proposal is empty: no transaction ids"
    missing = [i for i in h.proposed_psp_txn_ids if i not in ctx.txns_by_id]
    if missing:
        return False, f"unknown transaction ids: {missing}"
    # A repeated id passes every membership test trivially and then gets counted
    # again by `reconstruct`, so naming one leg three times triples the net.
    # `claimed_txn_ids` cannot catch this: it holds ids claimed by previously
    # accepted matches, not ids repeated inside the hypothesis under test.
    repeated = sorted(
        i for i, n in Counter(h.proposed_psp_txn_ids).items() if n > 1
    )
    if repeated:
        return False, f"transaction ids proposed more than once: {repeated}"
    return True, "every proposed id exists exactly once"


def _exclusivity(h: Hypothesis, ctx: VerifyContext) -> tuple[bool, str]:
    taken = [i for i in h.proposed_psp_txn_ids if i in ctx.claimed_txn_ids]
    if taken:
        return False, f"already claimed by an accepted match: {taken}"
    return True, "no proposed transaction is already claimed"


def _causality(h: Hypothesis, ctx: VerifyContext) -> tuple[bool, str]:
    """Money cannot arrive in the bank before the settlement that produced it,
    money that never settled at all cannot have funded a credit either, and
    money that settled long enough ago is no longer plausibly this credit.

    `settled_at` is `date | None` on the frozen model and unsettled legs are live
    on shipped data. Treating `None` as "not late" waves through precisely the
    leg that provably did not fund the line.

    **Bounded on the late side only, and that asymmetry is deliberate.** The
    upper bound is causal and absolute. There is NO lower bound: a settlement
    that settled thirty days before the credit passes here, where T1, T2 and T3
    would have refused it on `CandidatePool.within_window`'s symmetric ±2 days.

    That gap is not an oversight and closing it was tried and reverted -- see
    `DEFECT-CLOSEOUT-REPORT.md` D1 and the pinning test
    `test_a_stale_settlement_passes_causality_by_design`. The tiers need a date
    window because they identify a settlement by its AMOUNT alone, and at that
    tolerance a two-day window is the only thing standing between T3 and every
    coincidental amount collision in the file. This module identifies one by
    structure instead, and substitutes two constraints no tier has:
    `_coherence` requires the proposal to BE one complete settlement, and
    `_uniqueness` requires it to be the ONLY unclaimed settlement closing the
    line across every ingested settlement -- unfiltered by date, so it counts
    strictly more competitors than T3's ambiguity rule does.

    Applying the tiers' window here would leave the analyst layer with no
    admissible input at all: the band it would then accept is a strict subset of
    what T3 already matches deterministically, so every hypothesis it could
    still accept is one the engine had matched anyway, and every subject it
    could still be asked about is one T3 declined for ambiguity that
    `_uniqueness` declines too. Measured, not argued: it takes 13 tests red
    across `tests/llm/test_pipeline.py` and `tests/llm/test_obfuscated_ref.py`,
    the second of which is the shipped late-posting capability.
    """
    line = ctx.bank_lines_by_id[h.proposed_bank_line_id]
    unsettled = [
        i for i in h.proposed_psp_txn_ids if ctx.txns_by_id[i].settled_at is None
    ]
    if unsettled:
        return False, f"never settled, so cannot have funded a credit: {unsettled}"
    late = [
        i for i in h.proposed_psp_txn_ids if ctx.txns_by_id[i].settled_at > line.txn_date
    ]
    if late:
        return False, f"settled after bank date {line.txn_date}: {late}"
    return True, f"every settled_at is on or before {line.txn_date}"


def _arithmetic(h: Hypothesis, ctx: VerifyContext) -> tuple[bool, str]:
    """The re-check. `reconstruct` is the matcher's own arithmetic, imported
    rather than re-implemented so the two can never disagree."""
    line = ctx.bank_lines_by_id[h.proposed_bank_line_id]
    net = reconstruct([ctx.txns_by_id[i] for i in h.proposed_psp_txn_ids]).net
    credit = line.credit  # never None: _existence rejects a line with no credit
    delta = abs(net - credit)
    return (
        delta <= TOLERANCE_PAISE,
        f"net {net} vs credit {credit}, delta {delta} (tolerance {TOLERANCE_PAISE})",
    )


def _uniqueness(h: Hypothesis, ctx: VerifyContext) -> tuple[bool, str]:
    """Runs LAST. More than one unclaimed candidate settlement closing the same
    bank line means the data does not determine an answer -- the same ambiguity
    rule the deterministic tiers obey (spec §7).

    `exclusivity` cannot catch this and it is worth being precise about why: in
    an ambiguous pair the competing candidate sets are **disjoint** and neither
    has been claimed by anything, so nothing blocks anything. Every id exists,
    every leg settles on the bank date, and both nets close exactly -- all four
    earlier checks pass on both hypotheses, both are accepted, and
    `trap_capture_rate` silently goes to zero.
    """
    line = ctx.bank_lines_by_id[h.proposed_bank_line_id]
    credit = line.credit  # never None: _existence rejects a line with no credit
    closers = []
    for setl_id, legs in ctx.txns_by_settlement.items():
        if any(t.txn_id in ctx.claimed_txn_ids for t in legs):
            continue
        if abs(reconstruct(legs).net - credit) <= TOLERANCE_PAISE:
            closers.append(setl_id)
    return (
        len(closers) <= 1,
        f"{len(closers)} candidate settlements close {line.line_id}: "
        f"{sorted(closers)}",
    )


def _coherence(h: Hypothesis, ctx: VerifyContext) -> tuple[bool, str]:
    """The proposed set must BE a settlement -- one settlement, all of its legs.

    Runs between `arithmetic` and `uniqueness`. Without it the other checks are
    each blind in the same way: the first four test the proposed set in
    isolation, and `uniqueness` enumerates whole entries of `txns_by_settlement`
    but never asks whether the PROPOSED set is one of them. So any cherry-picked
    leg set that happens to close the arithmetic sails through with zero or one
    closer -- and two mutually exclusive hypotheses can be accepted for the same
    bank line, which is `trap_capture_rate` at zero by a route no other check
    can see. Unconstrained subset-sum over the leg pool is not reconciliation.

    It runs AFTER `arithmetic` deliberately: a proposal that is both incoherent
    and wrong should be reported as wrong, which is the more useful diagnosis.

    **It reports `existence`**, because the settlement the hypothesis implicitly
    names does not exist as proposed. `CheckName` is set-identical to
    `ReconException.failed_check` on the frozen contract, so a sixth spelling
    would be a contract change; and `uniqueness` is the wrong label here because
    Lane E renders it as "more than one candidate settlement satisfied the
    arithmetic" (LANE-E-web.md §7.2), which is false of a cherry-picked set.
    """
    ids = h.proposed_psp_txn_ids
    settlement_ids = {ctx.txns_by_id[i].settlement_id for i in ids}

    if not settlement_ids:
        # Defence in depth: `_existence` rejects an empty proposal first.
        return False, "the proposal names no transactions"
    if None in settlement_ids:
        loose = sorted(i for i in ids if ctx.txns_by_id[i].settlement_id is None)
        return False, f"proposed legs carry no settlement_id: {loose}"
    if len(settlement_ids) > 1:
        return False, (
            f"proposed legs span {len(settlement_ids)} settlements: "
            f"{sorted(settlement_ids)}"
        )

    setl_id = next(iter(settlement_ids))
    if setl_id not in ctx.txns_by_settlement:
        return False, f"settlement {setl_id!r} is not in the ingested data"

    # The expected leg set comes from `txns_by_id`, NOT from `txns_by_settlement`.
    # The map answers only "was this settlement ingested"; it is not the authority
    # on what the settlement contains. A caller that builds it from unclaimed legs
    # -- the mistake VerifyContext's docstring warns about -- would otherwise
    # present a partly-claimed settlement as a complete one, and the remainder
    # would be laundered into a whole settlement that closes the gross. An empty
    # map fails closed and loudly; a partially populated one fails open silently,
    # so the check leans on the source `_existence` already forces to be complete.
    expected = {
        t.txn_id for t in ctx.txns_by_id.values() if t.settlement_id == setl_id
    }
    proposed = set(ids)
    if proposed != expected:
        return False, (
            f"not the complete leg set of {setl_id}: "
            f"missing {sorted(expected - proposed)}, "
            f"unexpected {sorted(proposed - expected)}"
        )
    return True, f"the proposal is exactly settlement {setl_id}"


#: The checks, in order. Six entries over five spellings: `_coherence` reports
#: `existence` because the spelling set is frozen on `ReconException`. Anything
#: needing a sixth label is a contract change and halts the lanes.
CHECKS: tuple[tuple[CheckName, Check], ...] = (
    ("existence", _existence),
    ("exclusivity", _exclusivity),
    ("causality", _causality),
    ("arithmetic", _arithmetic),
    ("existence", _coherence),
    ("uniqueness", _uniqueness),
)


def verify(h: Hypothesis, ctx: VerifyContext) -> VerifyResult:
    """Re-check one hypothesis. Every check must hold, in order.

    Returns a verdict for every input, including a malformed one. It never
    raises on a bad hypothesis: that is the point.
    """
    for name, check in CHECKS:
        ok, reason = check(h, ctx)
        if not ok:
            return VerifyResult(accepted=False, reason=reason, failed_check=name)
    return VerifyResult(accepted=True, reason="all checks passed", failed_check=None)
