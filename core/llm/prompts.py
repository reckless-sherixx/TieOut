"""What the analyst is allowed to see, and how it is rendered.

Three rules govern this module, and all three are tested.

**The prompt never contains ground truth.** The analyst receives the unmatched
residue plus canonicalised context and nothing else -- never the whole batch,
never a labels file, and never a hint that some subjects are ones the dataset
considers to have no answer. A model told which subjects it is not expected to
solve is being handed the answer sheet, and the headline honesty metric it
would then score is worth nothing.

**The prompt is dataset-agnostic.** No fixture-specific hints, no worked
examples with real ids, no "the answer is usually X" heuristics. It runs
against generated datasets it has never seen.

**The model is never asked to do arithmetic.** Every money figure in the prompt
-- each candidate settlement's reconstructed net and its delta against the bank
credit -- is computed here by `reconstruct`, the matcher's own function. The
model's job is identity and narration reasoning over deterministic facts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from core.matcher.batch import reconstruct  # arithmetic only; no matcher state
from core.models import BankLine, Order, PSPTransaction, ReconException

#: The tool the model must call. Structured output through tool use rather than
#: free text, so a response either parses into `Hypothesis` objects or is
#: dropped -- there is no prose to guess at in between.
TOOL_NAME = "propose_hypotheses"

HYPOTHESIS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "hypotheses": {
            "type": "array",
            "description": (
                "One entry per subject you can resolve. Omit any subject the "
                "evidence does not determine; an empty array is a valid answer."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "subject_id": {
                        "type": "string",
                        "description": "The id of the unmatched subject, copied verbatim.",
                    },
                    "proposed_bank_line_id": {
                        "type": ["string", "null"],
                        "description": "The bank line this resolves, or null.",
                    },
                    "proposed_psp_txn_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Every PSP leg of the proposed settlement, including "
                            "its fee and tax legs."
                        ),
                    },
                    "proposed_order_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "One or two sentences naming the evidence used.",
                    },
                    "self_confidence": {
                        "type": "number",
                        "description": "0.0-1.0. Recorded for the audit trail only.",
                    },
                },
                "required": [
                    "subject_id",
                    "proposed_bank_line_id",
                    "proposed_psp_txn_ids",
                    "proposed_order_ids",
                    "reasoning",
                    "self_confidence",
                ],
            },
        }
    },
    "required": ["hypotheses"],
}


@dataclass(frozen=True)
class AnalystContext:
    """The canonicalised context handed to the analyst by its caller.

    Plain frozen records from `core/models.py` and nothing else -- no engine, no
    tiers, no pool, no match state. The caller decides what to put in it, which
    is what keeps the analyst testable with a handful of hand-built rows.
    """

    bank_lines: list[BankLine] = field(default_factory=list)
    psp_txns: list[PSPTransaction] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)


SYSTEM_RULES = """\
You are a reconciliation analyst for an Indian payments processor.

A bank credit is the NET of a settlement: the sum of its payment legs, less its
fee, tax, refund, chargeback and reserve legs. Amounts below are integer paise;
PSP leg amounts are signed (payments positive, deductions negative) and bank
credits are unsigned.

Your task: for each unmatched subject below, decide whether the candidate
evidence identifies exactly one settlement that produced it.

Rules you must follow:
1. Propose only ids that appear verbatim in the data below. Never invent one.
2. A proposal must list EVERY leg of the settlement, fee and tax legs included.
3. A settlement cannot have settled after the date the bank credited the money.
4. If two or more candidates fit a subject equally well, propose NOTHING for
   that subject. Declining is a correct and expected answer; a coin-flip
   between equal candidates is not.
5. Every figure below was computed for you. Do not recompute any total, and do
   not propose a set whose delta is not already shown as zero or near zero.

An empty list is a valid response. Every proposal you make is re-checked by
deterministic code before it is accepted, so a guess costs you and buys nothing.
"""


def _fmt_bank_line(b: BankLine) -> str:
    amount = f"credit={b.credit}" if b.credit is not None else f"debit={b.debit}"
    utr = b.utr if b.utr else "none"
    return (
        f"  {b.line_id}: date={b.txn_date} {amount} utr={utr} "
        f'narration="{b.narration}"'
    )


def _fmt_txn(t: PSPTransaction) -> str:
    return (
        f"    {t.txn_id}: type={t.txn_type} amount={t.amount} "
        f"order_id={t.order_id or 'none'} settled_at={t.settled_at or 'none'}"
    )


def _group_by_settlement(
    txns: Sequence[PSPTransaction],
) -> dict[str, list[PSPTransaction]]:
    grouped: dict[str, list[PSPTransaction]] = {}
    for t in txns:
        if t.settlement_id is not None:
            grouped.setdefault(t.settlement_id, []).append(t)
    return grouped


def render_prompt(
    exceptions: Sequence[ReconException], context: AnalystContext
) -> str:
    """Render the whole residue into one prompt.

    Batched over exceptions rather than one call per subject: the candidate
    settlements are shared, and a model that can see the other unmatched
    subjects can tell that two of them are competing for the same candidate.
    """
    lines: list[str] = [SYSTEM_RULES, "", "## Unmatched subjects", ""]
    for e in exceptions:
        lines.append(
            f"  {e.subject_id} ({e.subject_type}) amount={e.amount} "
            f"reason={e.reason_code.value}"
        )

    lines += ["", "## Bank lines in scope", ""]
    lines += [_fmt_bank_line(b) for b in context.bank_lines] or ["  (none)"]

    lines += ["", "## Candidate settlements", ""]
    grouped = _group_by_settlement(context.psp_txns)
    if not grouped:
        lines.append("  (none)")
    for setl_id, legs in grouped.items():
        # Deterministic arithmetic, done here so the model never has to.
        totals = reconstruct(legs)
        lines.append(
            f"  {setl_id}: net={totals.net} "
            f"(gross={totals.gross} fees={totals.fees} tax={totals.tax} "
            f"refunds={totals.refunds} holds={totals.holds}) legs={len(legs)}"
        )
        lines += [_fmt_txn(t) for t in legs]

    # A `## PSP legs with no settlement id` section used to be rendered here,
    # listing every leg whose `settlement_id` is None. It is deliberately gone.
    #
    # `_coherence` rejects any proposal whose legs carry no `settlement_id`, by
    # construction and on every dataset, so no proposal containing one of those
    # legs could ever be accepted. The section could only ever produce
    # rejections: tokens spent inviting hypotheses the verifier must refuse,
    # inflating `llm_rejection_rate` for no information gain. It was the same
    # self-inflicted rejection the caller's per-settlement candidate filter was
    # written to avoid, two sections further down the same prompt
    # (ARCHITECTURE.md 7.2).
    #
    # The legs are not rendered as context-only material either. Every row in
    # this prompt is one the model may name in a proposal, and a section headed
    # "here is some data you are forbidden to use" spends the same tokens to
    # buy a rule the model can misread -- where dropping it spends none and
    # cannot be misread at all. `_analyst_context` no longer collects them, so
    # in practice there are none to drop; this renderer stays defensive because
    # it must hold for any context a future caller assembles.
    # Pinned by `test_the_prompt_does_not_invite_proposals_it_must_reject`.

    if context.orders:
        lines += ["", "## Orders in scope", ""]
        lines += [
            f"  {o.order_id}: date={o.order_date} gross={o.gross_amount} "
            f"status={o.status} customer_ref={o.customer_ref}"
            for o in context.orders
        ]

    lines += [
        "",
        f"Call `{TOOL_NAME}` exactly once with your proposals.",
    ]
    return "\n".join(lines)
