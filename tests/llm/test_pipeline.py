"""Integration task I.1 -- the accept loop, end to end, with a stubbed client.

Every test here runs **offline**: no `ANTHROPIC_API_KEY`, no network, no live
model call. The analyst is exercised through the same injected-client seam
`tests/llm/test_analyst.py` uses, so what is under test is the wiring -- residue
in, hypotheses through the verifier, accepted out as a `MatchGroup` and rejected
out as an exception that kept its verdict.

Three of these pin the rules adversarial review produced, and each of them was
demonstrated by exploit before it was written down:

* `test_the_same_settlement_cannot_be_accepted_for_two_bank_lines` -- the accept
  loop updates `claimed_txn_ids` between verifications and never batch-accepts.
* `test_a_hypothesis_that_closes_a_different_line_than_its_subject_is_rejected`
  -- `subject_id` is tied to `proposed_bank_line_id` before acceptance, so a
  resolution cannot be credited to a subject it did not resolve.
* `test_the_settlement_map_carries_every_ingested_leg` -- `txns_by_settlement`
  is built from all ingested transactions, so a partly-claimed settlement can
  never be presented to `_coherence` as a complete one.

The fixtures deliberately produce a residue the deterministic engine cannot
resolve but the verifier can: the settlements settle SEVEN days before the bank
credit, outside the tiers' two-day window (`WINDOW_DAYS`), while `_causality`
asks only that money did not arrive before it settled. That gap is the whole
reason an analyst layer exists, and it is the only thing these fixtures exploit
-- no tier is weakened to make room for one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.llm import pipeline
from core.llm.pipeline import LLM_CONFIDENCE, merge, run_llm_pass
from core.llm.verifier import VerifyContext
from core.matcher.engine import run_match
from core.models import BankLine, Order, PSPTransaction
from core.store.repo import Repo

# --- the fixture records -------------------------------------------------------

SETTLED = "2026-07-10"
#: Seven days after `SETTLED`: outside `core.matcher.tiers.WINDOW_DAYS`, so no
#: deterministic tier will look at these settlements at all.
BANK_DATE = "2026-07-17"

#: 5_000_000 - 118_000 - 21_240. One settlement, uniquely closing one line.
LATE_NET = 4_860_760
#: 3_000_000 - 70_800 - 12_744, and TWO settlements reconstruct to it.
TWIN_NET = 2_916_456

RESOLVABLE_LINE = "BL-0001"
AMBIGUOUS_LINE = "BL-0002"
#: A second line the single `setl_LATE` also closes. Present only in the tests
#: that need it, because it is what makes the batch-accept exploit expressible.
RIVAL_LINE = "BL-0003"


def _order(order_id: str, gross: int) -> Order:
    return Order(
        order_id=order_id,
        order_date="2026-07-08",
        customer_ref=f"CUST-{order_id[-3:]}",
        gross_amount=gross,
        currency="INR",
        status="paid",
    )


def _txn(
    txn_id: str, txn_type: str, order_id: str | None, amount: int, settlement_id: str
) -> PSPTransaction:
    return PSPTransaction(
        txn_id=txn_id,
        txn_type=txn_type,
        order_id=order_id,
        captured_at="2026-07-08T10:00:00",
        amount=amount,
        settlement_id=settlement_id,
        settled_at=SETTLED,
    )


def _line(line_id: str, credit: int) -> BankLine:
    return BankLine(
        line_id=line_id,
        txn_date=BANK_DATE,
        # No settlement reference and no UTR: nothing for T0 to hit, so the only
        # reason these lines are exceptions is the date window.
        narration="NEFT CR RAZORPAY SOFTWARE PVT LTD BULK PAYOUT",
        credit=credit,
        debit=None,
        balance=99_000_000,
        utr=None,
    )


ORDERS = [_order("ORD-100", 5_000_000), _order("ORD-200", 3_000_000), _order("ORD-300", 3_000_000)]

PSP_TXNS = [
    _txn("pay_100", "payment", "ORD-100", 5_000_000, "setl_LATE"),
    _txn("fee_100", "fee", None, -118_000, "setl_LATE"),
    _txn("tax_100", "tax", None, -21_240, "setl_LATE"),
    _txn("pay_200", "payment", "ORD-200", 3_000_000, "setl_TWIN_A"),
    _txn("fee_200", "fee", None, -70_800, "setl_TWIN_A"),
    _txn("tax_200", "tax", None, -12_744, "setl_TWIN_A"),
    _txn("pay_300", "payment", "ORD-300", 3_000_000, "setl_TWIN_B"),
    _txn("fee_300", "fee", None, -70_800, "setl_TWIN_B"),
    _txn("tax_300", "tax", None, -12_744, "setl_TWIN_B"),
]

LATE_LEGS = ["pay_100", "fee_100", "tax_100"]
TWIN_A_LEGS = ["pay_200", "fee_200", "tax_200"]


def _hypothesis(
    subject_id: str, line_id: str | None, legs: list[str], *, confidence: float = 0.9
) -> dict:
    return {
        "subject_id": subject_id,
        "proposed_bank_line_id": line_id,
        "proposed_psp_txn_ids": legs,
        "proposed_order_ids": [],
        "reasoning": "the settlement reconstructs to this credit and settled before it",
        "self_confidence": confidence,
    }


class StubAnalystClient:
    """The injected client, mirroring the real one's surface.

    `call` is the whole `AnalystClient` Protocol; `tokens` and `cost_usd` are the
    accounting `AnthropicAnalystClient` also exposes and the accept loop reads
    defensively. They are set explicitly by each test rather than invented, and
    a run through this stub is a run that made no live model call.

    `thoughts_tokens` is set **only when a test passes one**, so the default stub
    has no such attribute at all -- which is the shape of the real
    `AnthropicAnalystClient`, where `GeminiAnalystClient` is the one that counts
    reasoning. A stub that always carried the field could not tell the accept
    loop's defensive read from a hard-coded zero.
    """

    def __init__(
        self,
        payload,
        *,
        tokens: int = 0,
        cost_usd: float = 0.0,
        thoughts_tokens: int | None = None,
    ):
        self.payload = payload
        self.prompts: list[str] = []
        self.tokens = tokens
        self.cost_usd = cost_usd
        if thoughts_tokens is not None:
            self.thoughts_tokens = thoughts_tokens

    def call(self, prompt: str, schema: dict):
        self.prompts.append(prompt)
        return self.payload


def _result(lines: list[BankLine]):
    """The deterministic run these fixtures produce: no matches, one exception
    per bank line. Asserted rather than assumed -- a fixture that accidentally
    matched deterministically would make every test below vacuous."""
    result = run_match(ORDERS, PSP_TXNS, lines, run_id="run-pipeline")
    assert result.matches == [], "the fixture must leave the whole batch to the analyst"
    assert {e.subject_id for e in result.exceptions} == {line.line_id for line in lines}
    return result


# --- residue -> analyst -> verifier -> match / exception -----------------------


def test_an_accepted_hypothesis_becomes_a_match_and_a_rejected_one_stays_an_exception():
    """The whole path, in one assertion set.

    `BL-0001` is resolvable and is accepted; `BL-0002` is the ambiguity trap and
    is rejected on `uniqueness`, which is the strongest sentence in the demo.
    The rejected subject must still be an exception afterwards -- carrying the
    verdict, the failing check and the reason -- because a rejection that
    vanished would be indistinguishable from a subject the analyst never saw.
    """
    lines = [_line(RESOLVABLE_LINE, LATE_NET), _line(AMBIGUOUS_LINE, TWIN_NET)]
    result = _result(lines)
    client = StubAnalystClient(
        [
            _hypothesis(RESOLVABLE_LINE, RESOLVABLE_LINE, LATE_LEGS),
            _hypothesis(AMBIGUOUS_LINE, AMBIGUOUS_LINE, TWIN_A_LEGS),
        ]
    )

    outcome = run_llm_pass(result, ORDERS, PSP_TXNS, lines, client=client)

    assert (outcome.proposed, outcome.accepted, outcome.rejected) == (2, 1, 1)

    (match,) = outcome.matches
    assert match.bank_line_id == RESOLVABLE_LINE
    assert match.settlement_id == "setl_LATE"
    assert match.tier == "LLM"
    assert match.psp_txn_ids == LATE_LEGS
    # Derived from the legs, never from the model's `proposed_order_ids` -- which
    # this hypothesis deliberately left empty.
    assert match.order_ids == ["ORD-100"]
    assert match.net == LATE_NET, "MatchGroup.net IS the bank line credit"
    assert match.gross == 5_000_000 and match.fees == 118_000 and match.tax == 21_240
    assert match.confidence == LLM_CONFIDENCE
    assert match.confidence < 0.80, "an LLM match is never as certain as T3"

    subjects = {e.subject_id: e for e in outcome.exceptions}
    assert RESOLVABLE_LINE not in subjects, "an accepted subject is no longer an exception"

    rejected = subjects[AMBIGUOUS_LINE]
    assert rejected.verifier_verdict == "rejected"
    assert rejected.failed_check == "uniqueness"
    assert "setl_TWIN_A" in rejected.verifier_reason
    assert "setl_TWIN_B" in rejected.verifier_reason
    assert rejected.llm_hypothesis is not None
    assert "pay_200" in rejected.llm_hypothesis


def test_the_model_confidence_does_not_reach_the_match():
    """`self_confidence` is recorded in evidence and read by nothing.

    A hypothesis asserting 1.0 produces exactly the same `confidence` as one
    asserting 0.1, because what licenses the match is the verifier's six checks.
    """
    lines = [_line(RESOLVABLE_LINE, LATE_NET)]
    confident, timid = (
        run_llm_pass(
            _result(lines),
            ORDERS,
            PSP_TXNS,
            lines,
            client=StubAnalystClient(
                [_hypothesis(RESOLVABLE_LINE, RESOLVABLE_LINE, LATE_LEGS, confidence=c)]
            ),
        )
        for c in (1.0, 0.1)
    )
    assert confident.matches[0].confidence == timid.matches[0].confidence
    assert confident.matches[0].confidence == LLM_CONFIDENCE


def test_an_untouched_subject_keeps_the_not_attempted_verdict():
    """The analyst declining a subject is a correct answer, and it must not be
    recorded as a rejection: `not_attempted` and `rejected` are different claims
    and `llm_rejection_rate` counts only the second."""
    lines = [_line(RESOLVABLE_LINE, LATE_NET), _line(AMBIGUOUS_LINE, TWIN_NET)]
    outcome = run_llm_pass(
        _result(lines), ORDERS, PSP_TXNS, lines, client=StubAnalystClient([])
    )
    assert outcome.proposed == 0 and outcome.rejected == 0
    assert [e.verifier_verdict for e in outcome.exceptions] == ["not_attempted"] * 2
    assert all(e.failed_check is None for e in outcome.exceptions)


# --- rule 1: the claimed set is updated between verifications ------------------


def test_the_same_settlement_cannot_be_accepted_for_two_bank_lines():
    """The dual of the uniqueness check, closed by the loop rather than by the
    verifier.

    `_uniqueness` asks how many settlements close one bank line. Nothing asks how
    many bank lines one settlement closes. Verified against a single frozen
    context, BOTH hypotheses here pass every check -- `setl_LATE` closes
    `BL-0001` and `BL-0003` alike, and neither leg is claimed at the start of the
    pass. Only updating `claimed_txn_ids` after each acceptance turns the second
    into an `exclusivity` rejection.
    """
    lines = [_line(RESOLVABLE_LINE, LATE_NET), _line(RIVAL_LINE, LATE_NET)]
    result = _result(lines)
    client = StubAnalystClient(
        [
            _hypothesis(RESOLVABLE_LINE, RESOLVABLE_LINE, LATE_LEGS),
            _hypothesis(RIVAL_LINE, RIVAL_LINE, LATE_LEGS),
        ]
    )

    outcome = run_llm_pass(result, ORDERS, PSP_TXNS, lines, client=client)

    assert outcome.accepted == 1 and outcome.rejected == 1
    assert [m.bank_line_id for m in outcome.matches] == [RESOLVABLE_LINE]
    loser = next(e for e in outcome.exceptions if e.subject_id == RIVAL_LINE)
    assert loser.failed_check == "exclusivity"
    assert "already claimed" in loser.verifier_reason


def test_each_verification_sees_the_ids_accepted_before_it():
    """The same rule, watched from inside: the context handed to `verify` grows.

    Pinned on the context rather than only on the outcome because the outcome
    above would also be produced by a loop that happened to reject the second
    hypothesis for some other reason.
    """
    lines = [_line(RESOLVABLE_LINE, LATE_NET), _line(RIVAL_LINE, LATE_NET)]
    seen: list[set[str]] = []
    real_verify = pipeline.verify

    def spy(hypothesis, ctx: VerifyContext):
        seen.append(set(ctx.claimed_txn_ids))
        return real_verify(hypothesis, ctx)

    client = StubAnalystClient(
        [
            _hypothesis(RESOLVABLE_LINE, RESOLVABLE_LINE, LATE_LEGS),
            _hypothesis(RIVAL_LINE, RIVAL_LINE, LATE_LEGS),
        ]
    )
    original = pipeline.verify
    pipeline.verify = spy
    try:
        run_llm_pass(_result(lines), ORDERS, PSP_TXNS, lines, client=client)
    finally:
        pipeline.verify = original

    assert seen[0] == set(), "nothing was matched deterministically"
    assert seen[1] == set(LATE_LEGS), "the first acceptance is visible to the second check"


# --- rule 2: subject_id is tied to proposed_bank_line_id ----------------------


def test_a_hypothesis_that_closes_a_different_line_than_its_subject_is_rejected():
    """The `trap_capture_rate` exploit.

    Every one of the verifier's six checks passes on this hypothesis: `BL-0001`
    exists, its legs are unclaimed, they settled before it, the arithmetic
    closes, the proposal is exactly one settlement and only one settlement closes
    that line. What it does is credit the resolution to `BL-0002` -- an
    unresolvable subject -- which would move the project's headline honesty
    metric while every check reported green.
    """
    lines = [_line(RESOLVABLE_LINE, LATE_NET), _line(AMBIGUOUS_LINE, TWIN_NET)]
    result = _result(lines)
    client = StubAnalystClient(
        [_hypothesis(AMBIGUOUS_LINE, RESOLVABLE_LINE, LATE_LEGS, confidence=1.0)]
    )

    outcome = run_llm_pass(result, ORDERS, PSP_TXNS, lines, client=client)

    assert outcome.matches == [], "no subject may be resolved by another's evidence"
    assert (outcome.accepted, outcome.rejected) == (0, 1)
    trap = next(e for e in outcome.exceptions if e.subject_id == AMBIGUOUS_LINE)
    assert trap.verifier_verdict == "rejected"
    assert trap.failed_check == "existence"
    assert "may only close the bank line it was proposed for" in trap.verifier_reason
    # And the line it tried to borrow is still an open exception, not a match.
    assert any(e.subject_id == RESOLVABLE_LINE for e in outcome.exceptions)


def test_a_second_hypothesis_for_an_already_resolved_subject_is_rejected():
    """One bank line, one match. A repeated subject cannot produce a second.

    Two guards catch this and the verifier's is the first: once the first
    hypothesis is accepted its legs are claimed, so the repeat fails
    `exclusivity` before `_subject_tie` is reached. The tie check still holds the
    case `exclusivity` cannot see -- a subject that names no open exception at
    all, pinned in the test below.
    """
    lines = [_line(RESOLVABLE_LINE, LATE_NET)]
    client = StubAnalystClient(
        [
            _hypothesis(RESOLVABLE_LINE, RESOLVABLE_LINE, LATE_LEGS),
            _hypothesis(RESOLVABLE_LINE, RESOLVABLE_LINE, LATE_LEGS),
        ]
    )
    outcome = run_llm_pass(_result(lines), ORDERS, PSP_TXNS, lines, client=client)
    assert len(outcome.matches) == 1
    assert outcome.rejected == 1
    # The subject is gone from the exception list, so the rejection has nowhere
    # to be recorded -- it still counts, and the audit trail is where it lands.
    assert outcome.exceptions == []
    assert any(
        e.rule == "verifier:rejected:exclusivity" for e in outcome.audit
    ), "the repeat was rejected, and the trail says on which check"


def test_a_hypothesis_whose_subject_is_not_an_open_exception_is_rejected():
    """An invented subject resolves nothing, however well its proposal verifies.

    Every verifier check passes here -- the proposal is a real, unclaimed,
    arithmetically-closing settlement for a real bank line. What it lacks is a
    subject: `BL-9999` is not in this run's residue, so there is nothing for the
    resolution to be credited to, and accepting it would mint a match for a
    subject the engine never reported as open.
    """
    lines = [_line(RESOLVABLE_LINE, LATE_NET)]
    client = StubAnalystClient(
        [_hypothesis("BL-9999", RESOLVABLE_LINE, LATE_LEGS, confidence=1.0)]
    )
    outcome = run_llm_pass(_result(lines), ORDERS, PSP_TXNS, lines, client=client)

    assert outcome.matches == []
    assert (outcome.accepted, outcome.rejected) == (0, 1)
    assert [e.subject_id for e in outcome.exceptions] == [RESOLVABLE_LINE]
    assert outcome.exceptions[0].verifier_verdict == "not_attempted"
    assert any("is not an open subject" in e.evidence for e in outcome.audit)


def test_a_hypothesis_for_a_psp_subject_cannot_produce_a_bank_line_match():
    """A duplicate PSP row is a real exception and a real subject in the prompt,
    but no settlement-to-bank-line resolution can close one."""
    lines = [_line(RESOLVABLE_LINE, LATE_NET)]
    result = _result(lines)
    # Borrow a psp_txn subject from a dataset that has them, so the shape under
    # test is the engine's own and not one invented here.
    from core.ingest.reader import read_bank, read_orders, read_psp

    root = Path(__file__).resolve().parents[2] / "fixtures" / "seed42-50"
    real = run_match(
        read_orders(root / "orders.csv"),
        read_psp(root / "psp.csv"),
        read_bank(root / "bank.csv"),
        run_id="run-psp",
    )
    psp_subject = next(e for e in real.exceptions if e.subject_type == "psp_txn")
    result.exceptions.append(psp_subject)

    client = StubAnalystClient(
        [_hypothesis(psp_subject.subject_id, RESOLVABLE_LINE, LATE_LEGS)]
    )
    outcome = run_llm_pass(result, ORDERS, PSP_TXNS, lines, client=client)

    assert outcome.matches == []
    rejected = next(e for e in outcome.exceptions if e.subject_id == psp_subject.subject_id)
    assert rejected.failed_check == "existence"
    assert "is a psp_txn" in rejected.verifier_reason


# --- rule 3: the settlement map is built from every ingested leg ---------------


def test_the_analyst_context_offers_no_settlement_less_legs():
    """`_analyst_context` stops collecting legs that carry no `settlement_id`.

    The companion to `test_the_prompt_does_not_invite_proposals_it_must_reject`:
    the renderer will not show such a leg, and the accept loop no longer puts
    one in front of it. Both halves, because a context carrying rows the prompt
    silently drops is its own trap for the next reader.

    A settlement-less leg cannot be in any accepted proposal -- `_coherence`
    requires a proposal to BE one complete settlement -- so it is not a
    candidate, and the orders reachable only through one are not in scope
    either.
    """
    loose = PSPTransaction(
        txn_id="pay_loose",
        txn_type="payment",
        order_id="ORD-100",
        captured_at="2026-07-08T10:00:00",
        amount=5_000_000,
        settlement_id=None,
        settled_at=SETTLED,
    )
    lines = [_line(RESOLVABLE_LINE, LATE_NET)]
    result = _result(lines)

    client = StubAnalystClient([])
    run_llm_pass(result, ORDERS, [*PSP_TXNS, loose], lines, client=client)

    (prompt,) = client.prompts
    assert "pay_loose" not in prompt
    assert "PSP legs with no settlement id" not in prompt
    # Not vacuous: the settlements that ARE candidates are still rendered.
    assert "setl_LATE" in prompt and "pay_100" in prompt


def test_an_accepted_match_recovers_the_order_behind_an_anonymous_leg():
    """The accept loop applies the SAME order-recovery policy the pool applies.

    `pay_100` names no order -- the `missing_order_ref` defect. The deterministic
    path recovers it: `CandidatePool.order_ids` returns the order recovered by
    unique remainder, because a settlement's true order set is every order whose
    economic event is in the batch, not the set of ids the PSP rows happen to
    spell (`core/matcher/pool.py`).

    `pipeline._order_ids` used to scrape `txn.order_id` off the legs and stop
    there, so an LLM-accepted match on such a settlement reported an order set
    short by one and the scorer graded it a **false match** with all six
    verifier checks passed. Two accept paths, two answers, one of them wrong.

    ORD-100 is recoverable by the same three steps the pool uses: it is the only
    order in the register at gross 5,000,000, no leg names it outright, and no
    other anonymous leg contests it.
    """
    anonymous = [
        (
            t.model_copy(update={"order_id": None})
            if t.txn_id == "pay_100"
            else t
        )
        for t in PSP_TXNS
    ]
    assert all(
        t.order_id is None for t in anonymous if t.txn_id == "pay_100"
    ), "the fixture must actually blank the leg"

    lines = [_line(RESOLVABLE_LINE, LATE_NET)]
    result = run_match(ORDERS, anonymous, lines, run_id="run-recovery")
    assert result.matches == [], "the fixture must leave the batch to the analyst"

    outcome = run_llm_pass(
        result,
        ORDERS,
        anonymous,
        lines,
        client=StubAnalystClient(
            [_hypothesis(RESOLVABLE_LINE, RESOLVABLE_LINE, LATE_LEGS)]
        ),
    )

    (match,) = outcome.matches
    assert match.order_ids == ["ORD-100"], (
        "the recovered order must be in the match's order set, exactly as "
        "CandidatePool.order_ids would have put it there"
    )


def test_the_llm_and_deterministic_order_sets_cannot_disagree():
    """The two accept paths answer `order_ids` from ONE implementation.

    Asserted against `CandidatePool.order_ids` directly rather than against a
    literal, so this fails if the pool's recovery policy changes and the accept
    loop does not follow it -- which is the drift that made the defect, and the
    reason the fix reuses the pool rather than reimplementing its three steps.
    """
    from core.matcher.pool import CandidatePool

    anonymous = [
        (
            t.model_copy(update={"order_id": None})
            if t.txn_id == "pay_100"
            else t
        )
        for t in PSP_TXNS
    ]
    lines = [_line(RESOLVABLE_LINE, LATE_NET)]
    result = run_match(ORDERS, anonymous, lines, run_id="run-parity")

    outcome = run_llm_pass(
        result,
        ORDERS,
        anonymous,
        lines,
        client=StubAnalystClient(
            [_hypothesis(RESOLVABLE_LINE, RESOLVABLE_LINE, LATE_LEGS)]
        ),
    )

    pool = CandidatePool(
        orders=list(ORDERS), psp_txns=list(anonymous), bank_lines=list(lines)
    )
    (match,) = outcome.matches
    assert match.order_ids == pool.order_ids("setl_LATE")


def test_the_settlement_map_carries_every_ingested_leg():
    """Built from the ingested rows, never filtered by match state.

    Pinned directly on the builder because the damage a filtered map does is
    silent: `_coherence` derives its expected leg set from `txns_by_id`, so a
    partial map does not reject anything -- it would simply stop being able to
    say which settlements exist, and a partly-claimed settlement would present as
    a complete one. `fixtures/seed42-500` is the right witness: seven of its
    settlements are partly claimed, because a suppressed duplicate leg is never
    part of the match that claimed its twin.
    """
    from core.ingest.reader import read_bank, read_orders, read_psp

    root = Path(__file__).resolve().parents[2] / "fixtures" / "seed42-500"
    orders = read_orders(root / "orders.csv")
    psp_txns = read_psp(root / "psp.csv")
    bank_lines = read_bank(root / "bank.csv")
    result = run_match(orders, psp_txns, bank_lines, run_id="run-map")
    claimed = {i for m in result.matches for i in m.psp_txn_ids}

    grouped = pipeline._txns_by_settlement(psp_txns)

    expected = {t.settlement_id for t in psp_txns if t.settlement_id is not None}
    assert set(grouped) == expected
    for settlement_id, legs in grouped.items():
        assert {t.txn_id for t in legs} == {
            t.txn_id for t in psp_txns if t.settlement_id == settlement_id
        }

    partly_claimed = [
        settlement_id
        for settlement_id, legs in grouped.items()
        if any(t.txn_id in claimed for t in legs)
        and not all(t.txn_id in claimed for t in legs)
    ]
    assert partly_claimed, "the witness this test needs is gone; find another fixture"
    assert any(
        t.txn_id in claimed for t in grouped[partly_claimed[0]]
    ), "a claimed leg is still in the map -- exclusion is claimed_txn_ids' job"


def test_the_prompt_carries_the_residue_and_not_the_whole_batch():
    """The analyst sees the unmatched subjects and the unclaimed candidates.

    A prompt carrying the settlements the engine already matched is noise that
    invites proposals whose only outcome is an `exclusivity` rejection -- and it
    is the difference between a bounded call and one that grows with the dataset.
    """
    from core.ingest.reader import read_bank, read_orders, read_psp

    root = Path(__file__).resolve().parents[2] / "fixtures" / "seed42-500"
    orders = read_orders(root / "orders.csv")
    psp_txns = read_psp(root / "psp.csv")
    bank_lines = read_bank(root / "bank.csv")
    result = run_match(orders, psp_txns, bank_lines, run_id="run-prompt")
    client = StubAnalystClient([])

    run_llm_pass(result, orders, psp_txns, bank_lines, client=client)

    (prompt,) = client.prompts
    matched = result.matches[0]
    assert matched.settlement_id not in prompt, "a matched settlement is not a candidate"
    assert matched.bank_line_id not in prompt, "a matched bank line is not a subject"
    excepted = next(e for e in result.exceptions if e.subject_type == "bank_line")
    assert excepted.subject_id in prompt


# --- the full stack: execute_run, the scorer and the store --------------------


def _write_dataset(directory: Path, lines: list[BankLine]) -> Path:
    """The fixture records on disk, with the answers `scorer/` grades against.

    `BL-0002` is recorded as unresolvable: two settlements reconstruct to its
    credit and the data does not determine which. That is what makes
    `trap_capture_rate` meaningful on this dataset.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "orders.csv").write_text(
        "order_id,order_date,customer_ref,gross_amount,currency,status\n"
        + "".join(
            f"{o.order_id},{o.order_date},{o.customer_ref},{o.gross_amount},INR,paid\n"
            for o in ORDERS
        ),
        encoding="utf-8",
    )
    (directory / "psp.csv").write_text(
        "txn_id,txn_type,order_id,captured_at,amount,settlement_id,settled_at\n"
        + "".join(
            f"{t.txn_id},{t.txn_type},{t.order_id or ''},"
            f"{t.captured_at.isoformat()},{t.amount},{t.settlement_id},{SETTLED}\n"
            for t in PSP_TXNS
        ),
        encoding="utf-8",
    )
    (directory / "bank.csv").write_text(
        "line_id,txn_date,narration,credit,debit,balance,utr\n"
        + "".join(
            f"{b.line_id},{BANK_DATE},{b.narration},{b.credit},,{b.balance},\n"
            for b in lines
        ),
        encoding="utf-8",
    )
    (directory / "truth.json").write_text(
        json.dumps(
            {
                "seed": 7,
                "record_count": len(ORDERS),
                "linkages": [
                    {
                        "bank_line_id": RESOLVABLE_LINE,
                        "settlement_id": "setl_LATE",
                        "psp_txn_ids": LATE_LEGS,
                        "order_ids": ["ORD-100"],
                    },
                    {
                        "bank_line_id": AMBIGUOUS_LINE,
                        "settlement_id": "setl_TWIN_A",
                        "psp_txn_ids": TWIN_A_LEGS,
                        "order_ids": ["ORD-200"],
                    },
                ],
                "unresolvable_ids": [AMBIGUOUS_LINE],
            }
        ),
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def dataset(tmp_path) -> Path:
    return _write_dataset(
        tmp_path / "ds", [_line(RESOLVABLE_LINE, LATE_NET), _line(AMBIGUOUS_LINE, TWIN_NET)]
    )


def _run(tmp_path, dataset: Path, **kwargs) -> tuple[Repo, str]:
    from api.jobs import execute_run

    repo = Repo(tmp_path / "run.db")
    run_id = repo.create_run(
        seed=7,
        record_count=len(ORDERS),
        created_at=datetime(2026, 8, 28, 9, 30, tzinfo=timezone.utc),
    )
    execute_run(repo, run_id, dataset, **kwargs)
    return repo, run_id


def test_execute_run_persists_the_accepted_match_and_the_rejected_exception(
    tmp_path, dataset
):
    """Residue -> analyst -> verifier -> store, through the API's own job.

    The stub reports its own token and cost figures, which is how the two LLM
    metrics are shown to be plumbed rather than hard-coded to zero. **No live
    model call is made here**: the numbers below are the stub's, and they are
    asserted as arithmetic on a known input, never quoted as a real run's cost.
    """
    client = StubAnalystClient(
        [
            _hypothesis(RESOLVABLE_LINE, RESOLVABLE_LINE, LATE_LEGS),
            _hypothesis(AMBIGUOUS_LINE, AMBIGUOUS_LINE, TWIN_A_LEGS),
        ],
        tokens=9_000,
        cost_usd=0.03,
    )
    repo, run_id = _run(tmp_path, dataset, use_llm=True, analyst_client=client)

    status = repo.status(run_id)
    assert status.state == "completed"
    assert "1 accepted" in status.stage and "1 rejected" in status.stage

    summary = repo.summary(run_id)
    metrics = summary.metrics
    assert metrics.tier_counts == {"T0": 0, "T1": 0, "T2": 0, "T3": 0, "LLM": 1}
    assert metrics.llm_rejection_rate == 0.5
    assert metrics.llm_tokens_per_100 == 9_000 * 100 // len(ORDERS)
    assert metrics.llm_cost_usd_per_100 == pytest.approx(0.03 * 100 / len(ORDERS))
    # The two numbers that make the headline honest.
    assert metrics.false_match_rate == 0.0
    assert metrics.trap_capture_rate == 1.0
    assert metrics.assisted_match_rate == 1.0
    assert metrics.auto_match_rate == 0.0

    page = repo.exceptions_page(run_id, page=1, size=50)
    rejected = [e for e in page.items if e.subject_id == AMBIGUOUS_LINE]
    assert len(rejected) == 1
    assert rejected[0].verifier_verdict == "rejected"
    assert rejected[0].failed_check == "uniqueness"
    assert rejected[0].verifier_reason
    assert not [e for e in page.items if e.subject_id == RESOLVABLE_LINE]


def test_the_pass_reports_the_reasoning_tokens_the_client_counted():
    """`thoughts_tokens` reaches `LLMPass`, where it was captured and dropped.

    `GeminiAnalystClient` counts reasoning tokens as a subset of `output_tokens`
    and breaks them out on `self.thoughts_tokens`, because how much of a
    reconciliation's spend was thinking is worth reporting -- and on
    `gemini-3.5-flash` it is most of it (`OBFUSCATED-REF-REPORT.md` 9.5: 8,900
    to 10,102 of ~11,000 output tokens, billed at the output rate). Nothing read
    it, so the number stopped at the client.

    Read defensively, exactly as `tokens` and `cost_usd` are: `AnalystClient` is
    a one-method Protocol, and a stub that counts nothing must report zero rather
    than be forced to fake a field.
    """
    lines = [_line(RESOLVABLE_LINE, LATE_NET)]
    client = StubAnalystClient(
        [_hypothesis(RESOLVABLE_LINE, RESOLVABLE_LINE, LATE_LEGS)],
        tokens=9_000,
        cost_usd=0.03,
        thoughts_tokens=7_400,
    )
    outcome = run_llm_pass(_result(lines), ORDERS, PSP_TXNS, lines, client=client)

    assert outcome.tokens == 9_000
    assert outcome.thoughts_tokens == 7_400
    assert outcome.thoughts_tokens <= outcome.tokens, (
        "reasoning tokens are a subset of the total, never an addition to it"
    )


def test_a_client_that_counts_no_reasoning_reports_zero_not_an_estimate():
    """The Anthropic client exposes no `thoughts_tokens` at all, and a run
    through it must report zero rather than an invented figure -- the same rule
    `_client_usage` already applies to `tokens` and `cost_usd`."""
    lines = [_line(RESOLVABLE_LINE, LATE_NET)]
    client = StubAnalystClient(
        [_hypothesis(RESOLVABLE_LINE, RESOLVABLE_LINE, LATE_LEGS)], tokens=500
    )
    assert not hasattr(client, "thoughts_tokens")

    outcome = run_llm_pass(_result(lines), ORDERS, PSP_TXNS, lines, client=client)
    assert outcome.thoughts_tokens == 0


def test_a_reasoning_run_says_so_in_the_stage_and_a_non_reasoning_one_does_not(
    tmp_path, dataset
):
    """Reasoning tokens surface in the run's `stage`, and ONLY when there are any.

    The smallest honest surface for this number. It is not on `Metrics`: only one
    of the two shipped providers counts reasoning, so a `Metrics` field would be
    a permanent zero on every Anthropic deployment -- a frozen-contract field
    that is structurally meaningless for half its readers, and one that would
    oblige an `api/openapi.yaml` mirror and a `web/lib/api-types.ts`
    regeneration to say nothing. `stage` already carries the LLM narrative in
    free text, which is where a provider-specific figure belongs.

    The zero case is asserted as strictly as the non-zero one: a run whose client
    counts no reasoning must produce the stage string it produced before this
    field existed, byte for byte, so nothing downstream reading `stage` sees a
    change it did not need to.
    """
    payload = [_hypothesis(RESOLVABLE_LINE, RESOLVABLE_LINE, LATE_LEGS)]

    thinking = StubAnalystClient(
        list(payload), tokens=11_000, cost_usd=0.09, thoughts_tokens=9_500
    )
    repo, run_id = _run(tmp_path / "a", dataset, use_llm=True, analyst_client=thinking)
    stage = repo.status(run_id).stage
    assert "9500" in stage or "9,500" in stage, stage
    assert "reasoning" in stage.lower(), stage

    quiet = StubAnalystClient(list(payload), tokens=11_000, cost_usd=0.09)
    repo, run_id = _run(tmp_path / "b", dataset, use_llm=True, analyst_client=quiet)
    quiet_stage = repo.status(run_id).stage
    assert "reasoning" not in quiet_stage.lower(), quiet_stage
    assert quiet_stage == (
        "complete (LLM: 1 hypotheses proposed, 1 accepted, 0 rejected by the "
        "verifier)"
    ), "the no-reasoning stage string must be unchanged by this field existing"


def test_use_llm_without_a_key_completes_deterministically_and_says_so(
    tmp_path, dataset, monkeypatch
):
    """The path this repository actually runs on when no credential is present.

    It must complete, report valid `Metrics` with the LLM fields at zero, and say
    in `stage` that the analyst did not run -- a bare "complete" on a run the
    caller asked the LLM for reads as "the LLM ran and found nothing", which is a
    different and false claim.
    """
    from api import settings

    monkeypatch.delenv(settings.ANTHROPIC_API_KEY_ENV, raising=False)
    repo, run_id = _run(tmp_path, dataset, use_llm=True)

    status = repo.status(run_id)
    assert status.state == "completed"
    assert settings.ANTHROPIC_API_KEY_ENV in status.stage
    metrics = repo.summary(run_id).metrics
    assert metrics.tier_counts["LLM"] == 0
    assert metrics.llm_rejection_rate == 0.0
    assert metrics.llm_tokens_per_100 == 0
    assert metrics.llm_cost_usd_per_100 == 0.0


def test_a_failing_analyst_call_does_not_lose_the_deterministic_result(
    tmp_path, dataset
):
    """A timeout or a 401 must not throw away work that already succeeded --
    and must not be silent about having happened."""

    class Exploding:
        def call(self, prompt, schema):
            raise RuntimeError("connection reset")

    repo, run_id = _run(tmp_path, dataset, use_llm=True, analyst_client=Exploding())

    status = repo.status(run_id)
    assert status.state == "completed"
    assert "the LLM pass failed" in status.stage
    assert "RuntimeError" in status.stage
    assert repo.summary(run_id).metrics.tier_counts["LLM"] == 0


def test_use_llm_false_is_untouched_by_any_of_this(tmp_path, dataset):
    """The deterministic path does not change shape because an analyst exists."""
    repo, run_id = _run(tmp_path, dataset, use_llm=False)
    assert repo.status(run_id).stage == "complete"
    metrics = repo.summary(run_id).metrics
    assert metrics.tier_counts["LLM"] == 0
    assert metrics.llm_rejection_rate == 0.0
    assert repo.summary(run_id).exception_count == 2


def test_merge_leaves_the_deterministic_result_alone(tmp_path):
    """`merge` returns a new `MatchResult`; the engine's own is not mutated."""
    lines = [_line(RESOLVABLE_LINE, LATE_NET)]
    result = _result(lines)
    before = (len(result.matches), len(result.exceptions), len(result.audit))
    outcome = run_llm_pass(
        result,
        ORDERS,
        PSP_TXNS,
        lines,
        client=StubAnalystClient(
            [_hypothesis(RESOLVABLE_LINE, RESOLVABLE_LINE, LATE_LEGS)]
        ),
    )
    merged = merge(result, outcome)
    assert (len(result.matches), len(result.exceptions), len(result.audit)) == before
    assert merged.tier_breakdown["LLM"] == 1
    assert merged.exceptions == []
    # The trail continues rather than restarting: duplicate `entry_id`s inside
    # one run would collide in the store and break the ordering the UI renders.
    sequences = [e.sequence for e in merged.audit]
    assert sequences == sorted(sequences)
    assert len({e.entry_id for e in merged.audit}) == len(merged.audit)


# --- what the real client bills ------------------------------------------------


def test_the_anthropic_client_counts_what_the_api_said_it_billed():
    """Cost and token accounting, with the SDK stubbed out.

    `Metrics.llm_cost_usd_per_100` has to come from reported usage rather than
    from a token count computed on this side, so this pins that `usage` is read
    off the response and priced from the one table that names a rate.
    """
    from types import SimpleNamespace

    from core.llm.analyst import PRICING_USD_PER_MTOK, AnthropicAnalystClient

    class FakeMessages:
        def create(self, **kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", input={"hypotheses": []})],
                usage=SimpleNamespace(input_tokens=12_000, output_tokens=400),
            )

    client = AnthropicAnalystClient(SimpleNamespace(messages=FakeMessages()))
    assert client.call("prompt", {}) == []
    assert client.call("prompt", {}) == []

    assert client.input_tokens == 24_000
    assert client.output_tokens == 800
    assert client.tokens == 24_800
    per_input, per_output = PRICING_USD_PER_MTOK["claude-sonnet-5"]
    assert client.cost_usd == pytest.approx(
        (24_000 * per_input + 800 * per_output) / 1_000_000
    )


def test_an_unpriced_model_reports_no_cost_rather_than_a_guess():
    from types import SimpleNamespace

    from core.llm.analyst import AnthropicAnalystClient

    class FakeMessages:
        def create(self, **kwargs):
            return SimpleNamespace(
                content=[], usage=SimpleNamespace(input_tokens=5, output_tokens=5)
            )

    client = AnthropicAnalystClient(
        SimpleNamespace(messages=FakeMessages()), model="some-unlisted-model"
    )
    client.call("prompt", {})
    assert client.tokens == 10
    assert client.cost_usd == 0.0
