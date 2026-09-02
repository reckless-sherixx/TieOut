"""The accept loop -- the one place an LLM hypothesis can become a match.

`core/llm/analyst.py` proposes and `core/llm/verifier.py` decides; this module
is the loop that runs between them and turns a verdict into a `MatchGroup` or
into a rejected exception. It is a pure function of ingested records, a
`MatchResult` and an injected client: no clock, no web framework, no I/O of its
own beyond the client call the caller handed it.

It exists as `core/` rather than as a block inside `api/jobs.py` for one
reason: there must be exactly ONE accept loop. A second caller that assembles
`VerifyContext` by hand is a second place the three rules below can be got
wrong, and each of them was demonstrated by exploit before it was written down.

**1. `claimed_txn_ids` is updated between verifications. Nothing is batched.**
`_uniqueness` asks how many settlements close a given bank line. Nothing asks
how many bank lines one settlement closes -- that dual is open, and it becomes
live the moment a batch of hypotheses is verified against one frozen context:
two hypotheses naming the same settlement for two different lines would both
pass every check and both be accepted. So the loop verifies one hypothesis,
accepts it, adds its legs to the claimed set, and only then looks at the next.
`_exclusivity` closes the dual as a side effect, which is why it must see an
up-to-date set rather than the snapshot the pass started from.

**2. `subject_id` is tied to `proposed_bank_line_id` before acceptance.**
The verifier cannot know a subject's type, so it never checks this: a
hypothesis carrying `subject_id="BL-TRAP"` and `proposed_bank_line_id="BL-REAL"`
verifies clean and would credit a resolution to a subject the data does not
determine -- moving `trap_capture_rate`, the project's headline honesty metric.
`_subject_tie` is that check, and it runs on hypotheses the verifier has
already accepted (see its docstring for why last and not first).

**3. `txns_by_settlement` is built from every ingested transaction.**
Not from the unclaimed ones. `_coherence` derives its expected leg set from
`txns_by_id` precisely because a partly-populated map fails OPEN -- an empty map
rejects everything loudly, a partial one silently launders a partly-claimed
settlement into a complete one. That hardening holds regardless, but exclusion
belongs in `claimed_txn_ids`, in one place, and this is that place.

The model's `self_confidence` is not read here either. An accepted match
carries `LLM_CONFIDENCE`, a constant below every deterministic tier's, because
what licenses the match is the verifier's six checks and not the model's
opinion of itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from core.audit import AuditLog
from core.llm.analyst import AnalystClient, analyse
from core.llm.prompts import AnalystContext
from core.llm.verifier import VerifyContext, VerifyResult, verify
from core.matcher.batch import reconstruct
from core.matcher.engine import MatchResult
from core.matcher.pool import CandidatePool
from core.models import (
    AuditEntry,
    BankLine,
    Hypothesis,
    MatchGroup,
    Order,
    PSPTransaction,
    ReconException,
)

#: The tier label an accepted hypothesis is recorded under. One of the five
#: frozen `MatchGroup.tier` values, and the one `Metrics.tier_counts["LLM"]`
#: counts.
LLM_TIER = "LLM"

#: Below T3's 0.80, which is the loosest deterministic tier. An LLM-assisted
#: match is the least certain kind of match this system makes, and it says so.
#: It is a CONSTANT: `Hypothesis.self_confidence` is never read, here or in the
#: verifier, so a confident model cannot buy a higher number.
LLM_CONFIDENCE = 0.70


@dataclass(frozen=True)
class LLMPass:
    """What one analyst-plus-verifier pass produced.

    `exceptions` is the WHOLE replacement exception list, not a delta: accepted
    subjects have been removed and rejected ones carry the verifier's verdict.
    Returning the whole list keeps the engine's partition invariant checkable in
    one place -- every subject is matched or excepted, never both.
    """

    matches: list[MatchGroup] = field(default_factory=list)
    exceptions: list[ReconException] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)
    proposed: int = 0
    accepted: int = 0
    rejected: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    #: Reasoning tokens, a SUBSET of `tokens` and never an addition to it.
    #: Zero from a client that does not count them -- `AnthropicAnalystClient`
    #: exposes no such field, and a stub exposes nothing at all. Worth carrying
    #: because on a thinking model it is most of the bill: `gemini-3.5-flash`
    #: spent 8,900-10,102 of ~11,000 output tokens on reasoning, at the output
    #: rate (`OBFUSCATED-REF-REPORT.md` 9.5).
    thoughts_tokens: int = 0


def run_llm_pass(
    result: MatchResult,
    orders: Sequence[Order],
    psp_txns: Sequence[PSPTransaction],
    bank_lines: Sequence[BankLine],
    *,
    client: AnalystClient,
) -> LLMPass:
    """Analyse the deterministic residue, verify every hypothesis, accept none
    that fails.

    `result` is read, never mutated. Use `merge` to fold the pass back into a
    `MatchResult` the scorer can grade.
    """
    txns_by_id = {t.txn_id: t for t in psp_txns}
    bank_lines_by_id = {b.line_id: b for b in bank_lines}
    orders_by_id = {o.order_id: o for o in orders}

    txns_by_settlement = _txns_by_settlement(psp_txns)

    # The deterministic path's answer to "what orders is this settlement's",
    # borrowed rather than reimplemented. See `_order_ids` for why an accept
    # loop that scraped `txn.order_id` off the legs graded correct matches as
    # false ones.
    #
    # A second pool over the same ingested rows, not the engine's: `MatchResult`
    # does not carry one, and threading it through `run_llm_pass` would put
    # mutable match state -- a claimed set that has already advanced -- into a
    # function whose whole contract is to be a pure function of the ingested
    # records. Recovery reads only `orders` and `psp_txns`, both of which are
    # exactly what the engine was handed, so the two pools agree by
    # construction; `test_the_llm_and_deterministic_order_sets_cannot_disagree`
    # asserts that against the pool rather than against a literal.
    pool = CandidatePool(
        orders=list(orders), psp_txns=list(psp_txns), bank_lines=list(bank_lines)
    )

    claimed: set[str] = {
        txn_id for match in result.matches for txn_id in match.psp_txn_ids
    }

    # The audit trail continues the engine's, so `entry_id` stays unique within
    # the run and `sequence` stays monotonic across both halves.
    log = AuditLog(result.run_id, start_sequence=len(result.audit))

    # Positional, with an accepted subject blanked to None rather than removed,
    # so `open_subjects`' indices stay valid for the whole pass. The Nones are
    # dropped once, at the end.
    exceptions: list[ReconException | None] = list(result.exceptions)
    #: The subjects still open, by id. Shrinks as hypotheses are accepted, which
    #: is what makes a second hypothesis for an already-resolved subject a
    #: rejection rather than a second match on one bank line.
    open_subjects = {
        e.subject_id: index
        for index, e in enumerate(exceptions)
        if e is not None
    }

    context = _analyst_context(
        orders_by_id, txns_by_settlement, bank_lines_by_id, claimed, exceptions
    )
    hypotheses = analyse(exceptions, context, client)

    matches: list[MatchGroup] = []
    accepted = 0
    rejected = 0

    for hypothesis in hypotheses:
        log.record(
            hypothesis.subject_id,
            "llm",
            "llm",
            "analyst:proposed",
            _hypothesis_text(hypothesis),
            hypothesis.self_confidence,
        )

        # Rule 1: a FRESH context per hypothesis, carrying everything accepted
        # so far. Hoisting this out of the loop is the batch-accept bug.
        verdict = verify(
            hypothesis,
            VerifyContext(
                txns_by_id=txns_by_id,
                bank_lines_by_id=bank_lines_by_id,
                claimed_txn_ids=set(claimed),
                txns_by_settlement=txns_by_settlement,
            ),
        )
        if verdict.accepted:
            # Rule 2. Runs on an already-verified hypothesis, so a proposal that
            # is both mis-tied and arithmetically wrong is reported as wrong --
            # the more useful diagnosis, and the same ordering rule `_coherence`
            # follows in the verifier.
            verdict = _subject_tie(hypothesis, exceptions, open_subjects)

        if not verdict.accepted:
            rejected += 1
            _record_rejection(exceptions, open_subjects, hypothesis, verdict, log)
            continue

        index = open_subjects.pop(hypothesis.subject_id)
        match = _match_from(hypothesis, txns_by_id, bank_lines_by_id, pool, verdict)
        matches.append(match)
        accepted += 1
        claimed.update(match.psp_txn_ids)
        exceptions[index] = None
        log.record(
            hypothesis.subject_id,
            "verify",
            "verifier",
            "verifier:accepted",
            "; ".join(match.evidence),
            LLM_CONFIDENCE,
        )

    tokens, cost_usd, thoughts_tokens = _client_usage(client)
    return LLMPass(
        matches=matches,
        exceptions=[e for e in exceptions if e is not None],
        audit=log.entries(),
        proposed=len(hypotheses),
        accepted=accepted,
        rejected=rejected,
        tokens=tokens,
        cost_usd=cost_usd,
        thoughts_tokens=thoughts_tokens,
    )


def merge(result: MatchResult, outcome: LLMPass) -> MatchResult:
    """Fold a pass back into a `MatchResult`, without mutating the original.

    `tier_breakdown` counts `matches`, so appending here is what makes
    `Metrics.tier_counts["LLM"]` report accepted hypotheses.
    """
    return MatchResult(
        run_id=result.run_id,
        matches=[*result.matches, *outcome.matches],
        exceptions=outcome.exceptions,
        audit=[*result.audit, *outcome.audit],
        record_count=result.record_count,
    )


def _txns_by_settlement(
    psp_txns: Sequence[PSPTransaction],
) -> dict[str, list[PSPTransaction]]:
    """Rule 3: every ingested settlement, with ALL of its legs.

    Built from the ingested rows and nothing else -- not from the unclaimed
    ones, and not from match state. Already-matched settlements belong in here;
    `claimed_txn_ids` is what excludes them, and doing the exclusion twice in two
    places is how two callers drift apart.

    The failure mode this shape avoids is asymmetric. An EMPTY map fails closed
    and loudly: `_coherence` rejects every proposal. A PARTIAL one fails open and
    silently, which is why a filtered build is the dangerous one rather than the
    merely incomplete one.
    """
    grouped: dict[str, list[PSPTransaction]] = {}
    for txn in psp_txns:
        if txn.settlement_id is not None:
            grouped.setdefault(txn.settlement_id, []).append(txn)
    return grouped


# --- the two caller-owned checks ----------------------------------------------


def _subject_tie(
    hypothesis: Hypothesis,
    exceptions: list[ReconException | None],
    open_subjects: dict[str, int],
) -> VerifyResult:
    """A resolution may only close the bank line it was proposed for.

    Three ways to fail, all reported as `existence`: the subject is not an open
    exception of this run (already resolved, or invented); the subject is not a
    bank line, so no `MatchGroup` could name it; or the proposal closes a
    DIFFERENT line than the one it claims to resolve.

    `existence` is the label because `CheckName` is set-identical to
    `ReconException.failed_check` on the frozen contract and a sixth spelling
    would be a contract change -- the same ruling `_coherence` is made under.
    The subject the hypothesis claims to resolve does not exist as proposed.
    """
    index = open_subjects.get(hypothesis.subject_id)
    if index is None:
        return VerifyResult(
            accepted=False,
            reason=(
                f"{hypothesis.subject_id!r} is not an open subject of this run: "
                f"it was never an exception, or an earlier hypothesis already "
                f"resolved it"
            ),
            failed_check="existence",
        )
    subject = exceptions[index]
    assert subject is not None  # open_subjects only indexes unresolved entries
    if subject.subject_type != "bank_line":
        return VerifyResult(
            accepted=False,
            reason=(
                f"subject {hypothesis.subject_id} is a {subject.subject_type}, and "
                f"a settlement-to-bank-line resolution cannot close one"
            ),
            failed_check="existence",
        )
    if hypothesis.proposed_bank_line_id != hypothesis.subject_id:
        return VerifyResult(
            accepted=False,
            reason=(
                f"proposed for subject {hypothesis.subject_id} but closes "
                f"{hypothesis.proposed_bank_line_id}: a resolution may only close "
                f"the bank line it was proposed for"
            ),
            failed_check="existence",
        )
    return VerifyResult(
        accepted=True,
        reason=f"all checks passed, and the proposal closes {hypothesis.subject_id} itself",
        failed_check=None,
    )


# --- building the analyst's view ----------------------------------------------


def _analyst_context(
    orders_by_id: dict[str, Order],
    txns_by_settlement: dict[str, list[PSPTransaction]],
    bank_lines_by_id: dict[str, BankLine],
    claimed: set[str],
    exceptions: Sequence[ReconException],
) -> AnalystContext:
    """The residue, and only the residue.

    Subjects are the excepted bank lines; candidates are the settlements no
    deterministic match claimed. The whole batch is never rendered: a prompt
    carrying the 151 settlements the engine already matched would be mostly
    noise and would invite proposals that exist only to be rejected on
    `exclusivity`.

    Candidates are filtered **per settlement, not per leg**. `_coherence`
    requires a proposal to be one complete settlement, so a prompt that showed
    half of a partly-claimed one would be inviting a rejection it manufactured
    itself.

    By the same rule, legs carrying no `settlement_id` are **not candidates at
    all**. This function used to append them, and `render_prompt` used to give
    them a section of their own -- but `_coherence` rejects a proposal whose
    legs carry no `settlement_id` by construction, so every hypothesis they
    could appear in was refused before it was read. That is the identical
    manufactured rejection the per-settlement filter above exists to prevent,
    and it was two sections further down the same prompt (ARCHITECTURE.md 7.2).
    Orders reachable only through such a leg leave scope with it, for the same
    reason: they belong to nothing the model may propose.
    """
    subject_lines = [
        bank_lines_by_id[e.subject_id]
        for e in exceptions
        if e.subject_type == "bank_line" and e.subject_id in bank_lines_by_id
    ]

    candidates: list[PSPTransaction] = []
    for legs in txns_by_settlement.values():
        if any(t.txn_id in claimed for t in legs):
            continue
        candidates.extend(legs)

    order_ids = {
        t.order_id for t in candidates if t.order_id and t.order_id in orders_by_id
    }
    return AnalystContext(
        bank_lines=subject_lines,
        psp_txns=candidates,
        orders=[orders_by_id[i] for i in sorted(order_ids)],
    )


# --- turning a verdict into a record -------------------------------------------


def _match_from(
    hypothesis: Hypothesis,
    txns_by_id: dict[str, PSPTransaction],
    bank_lines_by_id: dict[str, BankLine],
    pool: CandidatePool,
    verdict: VerifyResult,
) -> MatchGroup:
    """The accepted hypothesis as a `MatchGroup`.

    Every money field is recomputed by `reconstruct` from the legs -- the model
    proposed identities, never amounts, and nothing it said is copied into a
    number. `order_ids` is likewise derived from the data rather than taken from
    `proposed_order_ids`, which is model-supplied and unverified.
    """
    line = bank_lines_by_id[hypothesis.proposed_bank_line_id]
    legs = [txns_by_id[i] for i in hypothesis.proposed_psp_txn_ids]
    totals = reconstruct(legs)
    # `_coherence` has already established that the legs are exactly one
    # settlement and that its id is not None.
    settlement_id = legs[0].settlement_id
    credit = line.credit
    assert credit is not None  # `_existence` rejects a line with no credit

    return MatchGroup(
        match_id=f"match-{line.line_id}",
        bank_line_id=line.line_id,
        settlement_id=settlement_id,
        psp_txn_ids=[t.txn_id for t in legs],
        order_ids=_order_ids(settlement_id, pool),
        gross=totals.gross,
        fees=totals.fees,
        tax=totals.tax,
        refunds=totals.refunds,
        holds=totals.holds,
        # The invariant every tier obeys: `net` IS the bank line credit. Within
        # the verifier's tolerance the reconstruction may differ by up to a
        # rupee; that residual lives in evidence and is never folded in here.
        net=credit,
        tier=LLM_TIER,
        confidence=LLM_CONFIDENCE,
        evidence=[
            f"LLM analyst proposed settlement {settlement_id} for {line.line_id}",
            f"analyst reasoning (a claim, not evidence): {hypothesis.reasoning}",
            f"self_confidence {hypothesis.self_confidence:.2f}, recorded for the "
            f"audit trail and read by no check",
            f"deterministic verifier: {verdict.reason}",
            f"reconstructed net {totals.net} against credit {credit} -- residual "
            f"delta={totals.net - credit} paise (net - credit)",
        ],
    )


def _order_ids(settlement_id: str, pool: CandidatePool) -> list[str]:
    """The settlement's order set -- `CandidatePool.order_ids`, not a copy of it.

    This used to walk the proposed legs and scrape `txn.order_id` off each one,
    which agreed with the deterministic path on every settlement except the
    ones that matter. `CandidatePool.order_ids` also RECOVERS the order behind a
    `payment` leg that names none (the `missing_order_ref` defect), by unique
    remainder over the register: candidates at the leg's gross that no leg names
    outright, narrowed by `ORDER_RECOVERY_WINDOW_DAYS` only if ambiguous, and
    accepted only as an uncontested singleton. A settlement's true order set is
    every order whose economic event is in the batch, not the set of ids the PSP
    rows happen to spell -- so scraping the rows omitted the recovered order and
    the scorer graded the match FALSE with all six verifier checks passed.

    `OBFUSCATED-REF-REPORT.md` 9.1 recorded that as the only demonstrable
    false-match route on the LLM path. It is closed by delegation rather than by
    a second implementation of those three steps: two accept paths agreeing on
    the money and disagreeing on the answer is exactly the drift this project
    cannot afford, and reimplementing the recovery here would leave the two free
    to drift again on the next change to either.

    Dangling references are still excluded -- an id a leg names but the register
    does not hold is not a real order -- because that exclusion lives in
    `CandidatePool.order_ids` too. It is the same function; there is now no
    second policy to keep in step.
    """
    return pool.order_ids(settlement_id)


def _record_rejection(
    exceptions: list[ReconException | None],
    open_subjects: dict[str, int],
    hypothesis: Hypothesis,
    verdict: VerifyResult,
    log: AuditLog,
) -> None:
    """Preserve a rejection on its subject's exception.

    A rejected hypothesis is the project's evidence that the guardrail fires, so
    it is recorded, never swallowed: the proposal text, the free-text reason and
    the machine-readable `failed_check` all survive onto the exception the UI
    renders.

    A hypothesis whose subject names no open exception has nowhere to be
    recorded -- it still counts toward `llm_rejection_rate`, and the audit trail
    is where it lands.
    """
    log.record(
        hypothesis.subject_id,
        "verify",
        "verifier",
        f"verifier:rejected:{verdict.failed_check}",
        verdict.reason,
        0.0,
    )
    index = open_subjects.get(hypothesis.subject_id)
    if index is None:
        return
    subject = exceptions[index]
    assert subject is not None
    exceptions[index] = subject.model_copy(
        update={
            "llm_hypothesis": _hypothesis_text(hypothesis),
            "verifier_verdict": "rejected",
            "verifier_reason": verdict.reason,
            "failed_check": verdict.failed_check,
        }
    )


def _hypothesis_text(hypothesis: Hypothesis) -> str:
    """The proposal as one line a human can read, for `llm_hypothesis`."""
    legs = ", ".join(hypothesis.proposed_psp_txn_ids) or "(none)"
    return (
        f"{hypothesis.subject_id} <- bank line {hypothesis.proposed_bank_line_id} "
        f"via [{legs}] (self_confidence {hypothesis.self_confidence:.2f}): "
        f"{hypothesis.reasoning}"
    )


def _client_usage(client: AnalystClient) -> tuple[int, float, int]:
    """Token, cost and reasoning accounting, if the client keeps any.

    `AnalystClient` is a one-method Protocol on purpose, so usage is read
    defensively: the real `AnthropicAnalystClient` counts what the API billed,
    and a stub in a test reports nothing rather than being forced to fake it.
    Absent accounting means zero -- never an estimate.

    `thoughts_tokens` is read the same way and for a sharper reason: only ONE of
    the two shipped clients has it. `GeminiAnalystClient` breaks reasoning out of
    `output_tokens`; `AnthropicAnalystClient` has no such field. Requiring it on
    the Protocol would force the Anthropic client to carry a permanent zero and
    every stub to fake one, which is how a defensive read becomes a hard-coded
    number nobody notices is wrong.
    """
    return (
        int(getattr(client, "tokens", 0) or 0),
        float(getattr(client, "cost_usd", 0.0) or 0.0),
        int(getattr(client, "thoughts_tokens", 0) or 0),
    )
