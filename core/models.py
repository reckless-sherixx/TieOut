"""Frozen data contracts (spec §6). Every downstream lane imports from here
and nowhere else. Do not change field names, types, or literal values
without halting all parallel lanes.
"""

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.money import Money


class ReasonCode(str, Enum):
    NO_SETTLEMENT_REF = "NO_SETTLEMENT_REF"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    ORPHAN_BANK_LINE = "ORPHAN_BANK_LINE"
    ORPHAN_PSP_TXN = "ORPHAN_PSP_TXN"
    DUPLICATE_PSP_TXN = "DUPLICATE_PSP_TXN"
    AMBIGUOUS_MULTI_CANDIDATE = "AMBIGUOUS_MULTI_CANDIDATE"
    UNPARSEABLE_NARRATION = "UNPARSEABLE_NARRATION"
    MISSING_ORDER_REF = "MISSING_ORDER_REF"


# --- 6.2 Input records ------------------------------------------------------


class Order(BaseModel):
    model_config = ConfigDict(strict=True)

    order_id: str  # "ORD-004471"
    order_date: date = Field(strict=False)
    customer_ref: str
    gross_amount: Money
    currency: Literal["INR"]
    status: Literal["paid", "refunded", "partially_refunded", "cancelled"]


class PSPTransaction(BaseModel):
    model_config = ConfigDict(strict=True)

    txn_id: str  # "pay_", "rfnd_", "adj_", "cb_"
    txn_type: Literal["payment", "refund", "fee", "tax", "chargeback", "adjustment", "reserve"]
    order_id: str | None  # may be absent or garbled
    captured_at: datetime = Field(strict=False)
    amount: Money  # signed: credits positive, deductions negative
    settlement_id: str | None  # "setl_"
    settled_at: date | None = Field(strict=False)


class BankLine(BaseModel):
    model_config = ConfigDict(strict=True)

    line_id: str
    txn_date: date = Field(strict=False)
    narration: str  # deliberately messy
    credit: Money | None
    debit: Money | None
    balance: Money
    utr: str | None  # often absent


# --- 6.3 Output records -----------------------------------------------------


class MatchGroup(BaseModel):
    model_config = ConfigDict(strict=True)

    match_id: str
    bank_line_id: str
    settlement_id: str | None
    psp_txn_ids: list[str]
    order_ids: list[str]
    gross: Money
    fees: Money
    tax: Money
    refunds: Money
    holds: Money
    net: Money  # must equal the bank line credit
    tier: Literal["T0", "T1", "T2", "T3", "LLM"]
    confidence: float
    evidence: list[str]  # human-readable, one line per rule fired


class Settlement(BaseModel):
    """One settlement batch of one run, as a listable row.

    Added for `GET /api/runs/{id}/settlements`. Until it existed a settlement
    reached the wire only one id at a time, through
    `GET /api/runs/{id}/batches/{settlement_id}` -- so the netting diagram was
    browsable only for a batch whose id someone already knew, and a batch that
    never closed was not reachable at all.

    Where each number comes from is the whole point of the shape, and the rule
    is one line: **the engine's number, or the engine's function.** A settlement
    the run matched carries its `MatchGroup`'s own `gross`/`fees`/`tax`/
    `refunds`/`holds`/`net`, passed through untouched. A settlement it did not
    match has no `MatchGroup`, so its breakdown is `core.matcher.batch.
    reconstruct` over that settlement's active legs -- the same function the
    matcher itself calls. Nothing here sums money a second way.

    One consequence to render rather than hide: on a **T3** row
    `gross - fees - tax - refunds - holds` can sit up to the matcher's tolerance
    away from `net`. That is the engine's definition, not a rounding slip --
    `MatchGroup.net` *is* the bank credit, and a T3 match is precisely one where
    the reconstruction and the credit disagree within tolerance. The residual is
    spelled out in that match's `evidence`. `BatchNetting` behaves the same way
    for the same reason.
    """

    model_config = ConfigDict(strict=True)

    settlement_id: str
    gross: Money
    fees: Money
    tax: Money
    refunds: Money
    holds: Money
    net: Money
    #: `payment` legs only, the spec 7.1 cardinality. Fee, tax, refund,
    #: chargeback, reserve and adjustment legs are the batch's arithmetic, not
    #: its size: a settlement with one payment, one fee and one tax leg settles
    #: one order.
    payment_leg_count: int
    #: Whether the run closed this batch against a bank line. `False` is a
    #: result, not a gap -- an unclosed batch is exactly what a reviewer opens
    #: this listing to find.
    matched: bool
    #: The bank line it closed against, and the match that closed it. Both null
    #: when `matched` is false; both non-null when it is true.
    bank_line_id: str | None
    match_id: str | None
    #: The tier that matched it, straight from `MatchGroup.tier` -- never
    #: re-derived, so this listing and `Metrics.tier_counts` cannot disagree
    #: about which tier closed which batch. Null when unmatched.
    tier: Literal["T0", "T1", "T2", "T3", "LLM"] | None


class ReconException(BaseModel):
    model_config = ConfigDict(strict=True)

    exception_id: str
    subject_type: Literal["order", "psp_txn", "bank_line"]
    subject_id: str
    reason_code: ReasonCode
    amount: Money
    llm_hypothesis: str | None
    verifier_verdict: Literal["accepted", "rejected", "not_attempted"]
    verifier_reason: str | None
    # Machine-readable counterpart to the free-text verifier_reason: which of the
    # five checks in spec 8.2 rejected the hypothesis. None when no hypothesis was
    # attempted or when it was accepted. The UI's audit slide-over names the
    # failing check (LANE-E-web.md 7.2) and must not have to parse prose to do it.
    failed_check: Literal[
        "existence", "exclusivity", "causality", "arithmetic", "uniqueness"
    ] | None


class AuditEntry(BaseModel):
    model_config = ConfigDict(strict=True)

    entry_id: str
    run_id: str
    subject_id: str
    stage: Literal["ingest", "canonicalize", "match", "llm", "verify"]
    actor: Literal["deterministic", "llm", "verifier"]
    rule: str
    evidence: str
    confidence: float
    sequence: int  # monotonic within a run; no wall-clock in core


# --- §8.1 Hypothesis ---------------------------------------------------------


class Hypothesis(BaseModel):
    model_config = ConfigDict(strict=True)

    subject_id: str
    proposed_bank_line_id: str | None
    proposed_psp_txn_ids: list[str]
    proposed_order_ids: list[str]
    reasoning: str
    self_confidence: float


# --- §9 Metrics / run summary -----------------------------------------------


#: The tier labels `Metrics.tier_counts` is keyed on, identical to the values
#: `MatchGroup.tier` may take. Both a match group and a tier count use the same
#: five labels because they are the same fact counted twice; a sixth tier would
#: have to appear in both.
TIER_KEYS = ("T0", "T1", "T2", "T3", "LLM")


class Metrics(BaseModel):
    """Every metric defined in spec section 9. Rates are 0.0-1.0."""

    model_config = ConfigDict(strict=True)

    auto_match_rate: float
    assisted_match_rate: float
    exception_rate: float
    false_match_rate: float
    precision: float
    recall_on_resolvable: float
    trap_capture_rate: float
    #: The denominator `trap_capture_rate` divided by -- the count of subjects
    #: this dataset deliberately does not determine. It is 2 at 50 records and
    #: 10 at 500, and without it `100.0%` of 2 renders exactly like `100.0%` of
    #: 100. Defaulted so a stored run from before this field existed still
    #: loads; None there means "not recorded", never "zero traps".
    total_traps: int | None = None
    llm_rejection_rate: float
    throughput_records_per_sec: float
    llm_cost_usd_per_100: float
    llm_tokens_per_100: int
    #: Matches produced per tier, straight from the engine's own `MatchGroup.tier`
    #: assignment -- never re-derived, because a second derivation is a second
    #: thing that can disagree with the tier the engine actually recorded.
    #:
    #: All five keys are ALWAYS present. A tier that scored nothing is `0`, not
    #: absent: "T1 matched nothing" and "we do not know what T1 did" are
    #: different claims, and the UI must be able to render the first one. On
    #: `fixtures/tiny/` T1 legitimately scores zero, and that zero is a result.
    tier_counts: dict[str, int]

    # --- spec §6, input tax credit ------------------------------------------
    #
    # The three fields that turn the match rate into a rupee figure. They are
    # money, so they are **int paise and never float** -- `llm_cost_usd_per_100`
    # above is the one non-paise number in this model and it is a dollar cost,
    # not a counter-example.
    #
    # `scorer.score()` does not compute them: `core/itc/reconcile.py` produces
    # the `ITCReport` and `api/jobs.py` hands its three totals in as keyword
    # arguments, exactly as `llm_cost_usd` and `llm_tokens` arrive. They default
    # to zero, so a dataset with no `psp_gst_invoice.csv` scores normally.

    #: GST both computed from a MATCHED settlement and covered by an invoice --
    #: claimable and evidenced.
    itc_substantiated_paise: int
    #: GST that is one but not the other: computed and not invoiced, or invoiced
    #: and not substantiated by a settlement the engine could close. The second
    #: half is the coupling that makes this a rupee figure -- an unmatched
    #: settlement moves money from the field above into this one.
    itc_at_risk_paise: int
    #: Signed total disagreement between computed and invoiced. Unlike
    #: `itc_at_risk_paise` it does not take an absolute value, so it is a net
    #: position and may legitimately be negative.
    itc_variance_paise: int

    @field_validator("tier_counts")
    @classmethod
    def _tier_counts_has_exactly_the_five_tiers(
        cls, value: dict[str, int]
    ) -> dict[str, int]:
        if set(value) != set(TIER_KEYS):
            missing = sorted(set(TIER_KEYS) - set(value))
            unknown = sorted(set(value) - set(TIER_KEYS))
            raise ValueError(
                "tier_counts must carry exactly the keys "
                f"{list(TIER_KEYS)}; missing {missing}, unknown {unknown}. A "
                "tier that scored nothing is 0, never an absent key."
            )
        return value


# --- §7 drift detection -----------------------------------------------------


class MetricMove(BaseModel):
    """One metric of `Metrics`, before and after, and whether it matters.

    `before`, `after` and `delta` are `float` for every metric, integer-paise
    ones included. That is not money loosening its type: a `MetricMove` is a
    *comparison record*, never a money field, and it carries rates, a
    throughput, a dollar cost and a paise count in the same three slots. Every
    paise figure this system produces is far below 2**53, so a float holds it
    exactly and no rupee is lost on the way through. Money stays `int` in
    `Metrics`, in `MatchGroup` and everywhere it is money.

    `material` is computed **only** from `before` and `after`, against a named
    threshold in `core/drift/compare.py`. `DriftReport.narrative` is never an
    input to it.
    """

    model_config = ConfigDict(strict=True)

    metric: str
    before: float
    after: float
    #: `after - before`, signed.
    delta: float
    #: Whether the move cleared this metric's threshold. See
    #: `core/drift/compare.py` for the constant that decided it and why.
    material: bool


class ReasonCodeMove(BaseModel):
    """How often one `ReasonCode` fired, before and after.

    Reported only when the count changed: a code that fired ten times in both
    runs is not drift. A code that stopped firing entirely IS reported, with
    `appeared` false -- `appeared` is the narrower fact §7 names as its own
    threshold, "absent before, present now", and that is the shape of a new
    deduction type turning up overnight.
    """

    model_config = ConfigDict(strict=True)

    reason_code: str
    before: int
    after: int
    #: Absent before, present now. The reason-code threshold: any appearance.
    appeared: bool


class DriftReport(BaseModel):
    """What changed between two runs of the same dataset shape, and why.

    A finance controller's question is rarely "what is the match rate" -- it is
    "what changed since last time, and why". A match rate that falls from 98%
    to 91% because a new deduction type appeared is the finding; the 91% on its
    own is not.
    """

    model_config = ConfigDict(strict=True)

    baseline_run_id: str
    current_run_id: str
    moves: list[MetricMove]
    reason_code_moves: list[ReasonCodeMove]
    #: LLM-written prose over facts it did not compute; `None` when no model
    #: ran. **Never an input to `MetricMove.material`** -- the same division of
    #: labour the verifier enforces, where the model may describe a fact and may
    #: not decide anything.
    narrative: str | None


class RunSummary(BaseModel):
    model_config = ConfigDict(strict=True)

    run_id: str
    seed: int
    record_count: int
    state: Literal["pending", "running", "completed", "failed"]
    #: When the run was created. DATA, stamped at the API boundary and handed in
    #: -- NOT measured here. The global constraint forbids wall-clock inside
    #: `core/`, so nothing in this package may call `datetime.now()` to populate
    #: this, and it deliberately has no default and no `default_factory`: a
    #: later lane that "helpfully" adds one moves the clock back into `core/`.
    #: `api/` stamps it on creation (LANE-D-api.md 5.5); `core/store/` may
    #: persist the value it is handed and nothing more.
    created_at: datetime = Field(strict=False)
    match_count: int
    exception_count: int
    metrics: Metrics | None
