"""The LLM analyst (spec §8.1).

Every test here runs offline through an injected stub client -- no
`ANTHROPIC_API_KEY`, no network. The analyst's job is to propose; the verifier's
job is to decide. Nothing in this file asserts that a proposal is *correct*,
only that it is well-formed, that the prompt withholds the answers, and that a
malformed response produces nothing rather than a guess.
"""

import pytest

from core.llm.analyst import analyse
from core.llm.prompts import AnalystContext
from core.models import BankLine, PSPTransaction, ReasonCode, ReconException

VALID_PAYLOAD = [
    {
        "subject_id": "BL-1",
        "proposed_bank_line_id": "BL-1",
        "proposed_psp_txn_ids": ["pay_1"],
        "proposed_order_ids": [],
        "reasoning": "amount matches",
        "self_confidence": 0.8,
    }
]


class StubClient:
    """The injected client. `analyse` calls exactly this and nothing else."""

    def __init__(self, payload):
        self.payload = payload
        self.seen = None
        self.calls = 0

    def call(self, prompt, schema):
        self.seen = prompt
        self.calls += 1
        return self.payload


@pytest.fixture
def one_exception() -> ReconException:
    return ReconException(
        exception_id="EXC-1",
        subject_type="bank_line",
        subject_id="BL-1",
        reason_code=ReasonCode.NO_SETTLEMENT_REF,
        amount=4_794_654,
        llm_hypothesis=None,
        verifier_verdict="not_attempted",
        verifier_reason=None,
        failed_check=None,
    )


@pytest.fixture
def minimal_context() -> AnalystContext:
    return AnalystContext(
        bank_lines=[
            BankLine(
                line_id="BL-1",
                txn_date="2026-07-24",
                narration="NEFT RAZORPAY CREDIT",
                credit=4_794_654,
                debit=None,
                balance=0,
                utr=None,
            )
        ],
        psp_txns=[
            PSPTransaction(
                txn_id="pay_1",
                txn_type="payment",
                order_id="ORD-1",
                captured_at="2026-07-20T10:00:00",
                amount=4_932_000,
                settlement_id="setl_A",
                settled_at="2026-07-24",
            ),
            PSPTransaction(
                txn_id="fee_1",
                txn_type="fee",
                order_id=None,
                captured_at="2026-07-20T10:00:00",
                amount=-116_395,
                settlement_id="setl_A",
                settled_at="2026-07-24",
            ),
            PSPTransaction(
                txn_id="tax_1",
                txn_type="tax",
                order_id=None,
                captured_at="2026-07-20T10:00:00",
                amount=-20_951,
                settlement_id="setl_A",
                settled_at="2026-07-24",
            ),
        ],
    )


@pytest.fixture
def two_exceptions(one_exception) -> list[ReconException]:
    second = ReconException(
        exception_id="EXC-2",
        subject_type="bank_line",
        subject_id="BL-2",
        reason_code=ReasonCode.ORPHAN_BANK_LINE,
        amount=2_430_380,
        llm_hypothesis=None,
        verifier_verdict="not_attempted",
        verifier_reason=None,
        failed_check=None,
    )
    return [one_exception, second]


def test_returns_typed_hypotheses(one_exception, minimal_context):
    client = StubClient(VALID_PAYLOAD)
    out = analyse([one_exception], minimal_context, client)
    assert len(out) == 1
    assert out[0].subject_id == "BL-1"
    assert out[0].proposed_psp_txn_ids == ["pay_1"]


def test_the_whole_residue_is_batched_into_one_call(two_exceptions, minimal_context):
    """Asserting `calls == 1` against a ONE-element residue is vacuous -- a
    per-exception loop satisfies it too. Two exceptions is the smallest residue
    that can tell the two apart, and the prompt must carry both subjects: a loop
    would leave `client.seen` holding only the last one."""
    client = StubClient([])
    analyse(two_exceptions, minimal_context, client)
    assert client.calls == 1
    assert "BL-1" in client.seen and "BL-2" in client.seen


def test_prompt_never_contains_ground_truth(one_exception, minimal_context):
    """The analyst sees the unmatched residue and canonicalised context, and
    nothing else. Never the whole batch, never truth.json, and never a hint
    that a subject is one the dataset marked as having no answer."""
    client = StubClient([])
    analyse([one_exception], minimal_context, client)
    assert "truth" not in client.seen.lower()
    assert "unresolvable" not in client.seen.lower()


def test_prompt_carries_the_residue_and_its_candidates(one_exception, minimal_context):
    """A prompt that withholds ground truth must still carry the evidence, or
    the previous test would pass on an empty string."""
    client = StubClient([])
    analyse([one_exception], minimal_context, client)
    assert "BL-1" in client.seen
    assert "pay_1" in client.seen
    assert "setl_A" in client.seen


def test_prompt_states_the_net_computed_by_reconstruct(one_exception, minimal_context):
    """"The model is never asked to do arithmetic" is a headline claim of this
    lane, and it rests entirely on `render_prompt` calling `reconstruct` and
    handing the model the result. Replacing that call with placeholder text left
    every other test green. The expected figures are hard-coded here rather than
    recomputed, so a placeholder cannot coincidentally satisfy them:

        setl_A = 4_932_000 - 116_395 - 20_951 = 4_794_654
    """
    client = StubClient([])
    analyse([one_exception], minimal_context, client)
    assert "net=4794654" in client.seen
    assert "gross=4932000" in client.seen
    assert "fees=116395" in client.seen
    assert "tax=20951" in client.seen


def test_the_prompt_does_not_invite_proposals_it_must_reject(one_exception):
    """A leg carrying no `settlement_id` is never rendered as a candidate.

    The prompt used to append every such leg under `## PSP legs with no
    settlement id`. `_coherence` rejects any proposal whose legs carry no
    `settlement_id` -- by construction, on every dataset -- so that section
    could only ever produce rejections: tokens spent inviting hypotheses that
    cannot be accepted, inflating `llm_rejection_rate` for no information gain
    (ARCHITECTURE.md 7.2). It is the same self-inflicted rejection the
    per-settlement candidate filter in `_analyst_context` was written to avoid,
    two sections further down the same prompt.

    `render_prompt` is asserted here rather than the accept loop because it must
    hold for ANY context it is handed, including one assembled by a future
    caller that has not read this note.
    """
    context = AnalystContext(
        bank_lines=[
            BankLine(
                line_id="BL-1",
                txn_date="2026-07-24",
                narration="NEFT RAZORPAY CREDIT",
                credit=900_000,
                debit=None,
                balance=0,
                utr=None,
            )
        ],
        psp_txns=[
            PSPTransaction(
                txn_id="pay_loose",
                txn_type="payment",
                order_id="ORD-9",
                captured_at="2026-07-20T10:00:00",
                amount=900_000,
                settlement_id=None,
                settled_at="2026-07-24",
            )
        ],
    )
    client = StubClient([])
    analyse([one_exception], context, client)

    assert "PSP legs with no settlement id" not in client.seen
    assert "pay_loose" not in client.seen, (
        "a leg no accepted proposal could ever contain must not be offered as one"
    )


def test_malformed_response_yields_no_hypotheses(one_exception, minimal_context):
    out = analyse([one_exception], minimal_context, StubClient([{"garbage": 1}]))
    assert out == []


@pytest.mark.parametrize("payload", [None, {"hypotheses": []}, "not a list", 7])
def test_a_non_list_response_yields_no_hypotheses(
    one_exception, minimal_context, payload
):
    """A model that returns the wrong SHAPE is as malformed as one that returns
    the wrong fields. Neither may raise, and neither may be repaired."""
    assert analyse([one_exception], minimal_context, StubClient(payload)) == []


def test_a_malformed_entry_is_dropped_without_discarding_the_valid_ones(
    one_exception, minimal_context
):
    """Dropping means dropping -- never repairing a partial object into a
    plausible one, and never letting one bad entry take the good ones with it."""
    client = StubClient([{"garbage": 1}, *VALID_PAYLOAD, {"subject_id": "BL-9"}])
    out = analyse([one_exception], minimal_context, client)
    assert [h.subject_id for h in out] == ["BL-1"]


def test_analyse_reads_no_api_key(monkeypatch, one_exception, minimal_context):
    """The client is injected. No module-level client, no implicit environment
    read -- this is what makes the whole suite runnable with no key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert analyse([one_exception], minimal_context, StubClient(VALID_PAYLOAD))
