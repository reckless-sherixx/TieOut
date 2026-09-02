"""The deterministic verifier (spec §8.2).

Every test here runs with **no API key and no network**. The verifier is pure
Python over frozen models plus `reconstruct`, which is exactly why it is the
component that makes the architecture defensible: the LLM never touches money
arithmetic, and this file proves the re-check is real rather than decorative.
"""

import pytest

from core.llm.verifier import VerifyContext, verify
from core.models import BankLine, Hypothesis, PSPTransaction

# --- builders ---------------------------------------------------------------
#
# Hand-built rows rather than fixture CSVs: the verifier's contract is
# `VerifyContext`, not a file on disk, and a test that has to ingest a dataset
# to exercise a pure function is testing the wrong thing.


def _txn(
    txn_id: str,
    txn_type: str,
    amount: int,
    settlement_id: str | None = "setl_A",
    settled_at: str | None = "2026-07-24",
) -> PSPTransaction:
    return PSPTransaction(
        txn_id=txn_id,
        txn_type=txn_type,
        order_id=None,
        captured_at="2026-07-20T10:00:00",
        amount=amount,
        settlement_id=settlement_id,
        settled_at=settled_at,
    )


def _line(line_id: str, credit: int, txn_date: str = "2026-07-24") -> BankLine:
    return BankLine(
        line_id=line_id,
        txn_date=txn_date,
        narration="MISC CREDIT 00000",
        credit=credit,
        debit=None,
        balance=0,
        utr=None,
    )


def _ctx(
    txns: list[PSPTransaction],
    lines: list[BankLine],
    *,
    claimed: set[str] | None = None,
    settlements: dict[str, list[PSPTransaction]] | None = None,
) -> VerifyContext:
    """Assemble the context the way Lane D does: group by `settlement_id`."""
    if settlements is None:
        settlements = {}
        for t in txns:
            if t.settlement_id is not None:
                settlements.setdefault(t.settlement_id, []).append(t)
    return VerifyContext(
        txns_by_id={t.txn_id: t for t in txns},
        bank_lines_by_id={b.line_id: b for b in lines},
        claimed_txn_ids=set() if claimed is None else claimed,
        txns_by_settlement=settlements,
    )


# --- fixtures ---------------------------------------------------------------

# setl_A: 4_932_000 - 116_395 - 20_951 == 4_794_654, which BL-1 credits exactly.
_SETL_A = [
    _txn("pay_1", "payment", 4_932_000),
    _txn("fee_1", "fee", -116_395),
    _txn("tax_1", "tax", -20_951),
]
_SETL_A_NET = 4_794_654

# The ambiguity trap, in the shape `fixtures/tiny/` carries it: two disjoint
# settlements netting to the identical amount on the identical date.
#   setl_K9 = 1_600_000 + 900_000 - 59_000 - 10_620 == 2_430_380
#   setl_M2 = 2_500_000           - 59_000 - 10_620 == 2_430_380
_TRAP_NET = 2_430_380
_SETL_K9 = [
    _txn("pay_K1", "payment", 1_600_000, settlement_id="setl_K9"),
    _txn("pay_K2", "payment", 900_000, settlement_id="setl_K9"),
    _txn("fee_K9", "fee", -59_000, settlement_id="setl_K9"),
    _txn("tax_K9", "tax", -10_620, settlement_id="setl_K9"),
]
_SETL_M2 = [
    _txn("pay_M1", "payment", 2_500_000, settlement_id="setl_M2"),
    _txn("fee_M2", "fee", -59_000, settlement_id="setl_M2"),
    _txn("tax_M2", "tax", -10_620, settlement_id="setl_M2"),
]
_K9_IDS = [t.txn_id for t in _SETL_K9]


@pytest.fixture
def ctx_with_batch() -> VerifyContext:
    """Exactly ONE candidate settlement, so `uniqueness` has nothing to fire on
    and a correct hypothesis is genuinely accepted."""
    return _ctx(list(_SETL_A), [_line("BL-1", _SETL_A_NET)])


@pytest.fixture
def valid_hypothesis() -> Hypothesis:
    return Hypothesis(
        subject_id="BL-1",
        proposed_bank_line_id="BL-1",
        proposed_psp_txn_ids=["pay_1", "fee_1", "tax_1"],
        proposed_order_ids=["ORD-1"],
        reasoning="net of the settlement's legs equals the credit",
        self_confidence=0.8,
    )


@pytest.fixture
def ctx_with_claimed_txn() -> VerifyContext:
    return _ctx(list(_SETL_A), [_line("BL-2", _SETL_A_NET)], claimed={"pay_1"})


@pytest.fixture
def ctx_with_future_settlement() -> VerifyContext:
    """`pay_future` settles the day AFTER the bank line was credited."""
    future = _txn("pay_future", "payment", 100_000, settled_at="2026-07-25")
    return _ctx([future], [_line("BL-3", 100_000, txn_date="2026-07-24")])


@pytest.fixture
def ctx_with_two_equal_candidates() -> VerifyContext:
    return _ctx(_SETL_K9 + _SETL_M2, [_line("BL-A", _TRAP_NET)])


@pytest.fixture
def ctx_with_one_equal_candidate() -> VerifyContext:
    """The companion to the trap: identical in every respect except that only
    one settlement is a candidate."""
    return _ctx(list(_SETL_K9), [_line("BL-A", _TRAP_NET)])


# --- the five checks --------------------------------------------------------


def test_rejects_hallucinated_transaction_id(ctx_with_batch):
    h = Hypothesis(
        subject_id="BL-1",
        proposed_bank_line_id="BL-1",
        proposed_psp_txn_ids=["pay_DOES_NOT_EXIST"],
        proposed_order_ids=[],
        reasoning="",
        self_confidence=0.99,
    )
    assert verify(h, ctx_with_batch).failed_check == "existence"


def test_rejects_when_txn_already_claimed(ctx_with_claimed_txn):
    h = Hypothesis(
        subject_id="BL-2",
        proposed_bank_line_id="BL-2",
        proposed_psp_txn_ids=["pay_1"],
        proposed_order_ids=[],
        reasoning="",
        self_confidence=0.9,
    )
    assert verify(h, ctx_with_claimed_txn).failed_check == "exclusivity"


def test_rejects_when_settlement_postdates_bank_line(ctx_with_future_settlement):
    h = Hypothesis(
        subject_id="BL-3",
        proposed_bank_line_id="BL-3",
        proposed_psp_txn_ids=["pay_future"],
        proposed_order_ids=[],
        reasoning="",
        self_confidence=0.9,
    )
    assert verify(h, ctx_with_future_settlement).failed_check == "causality"


def test_rejects_when_arithmetic_does_not_close(ctx_with_batch):
    """`pay_1` alone is the gross, not the net -- it ignores fee and tax."""
    h = Hypothesis(
        subject_id="BL-1",
        proposed_bank_line_id="BL-1",
        proposed_psp_txn_ids=["pay_1"],
        proposed_order_ids=["ORD-1"],
        reasoning="looks right",
        self_confidence=0.9,
    )
    r = verify(h, ctx_with_batch)
    assert r.accepted is False and r.failed_check == "arithmetic"


def test_rejects_an_ambiguous_hypothesis(ctx_with_two_equal_candidates):
    """Two disjoint, unclaimed settlements close the same bank line, so the
    first four checks all pass. Only `uniqueness` catches it. An LLM must not
    resolve what the deterministic tiers correctly refused."""
    h = Hypothesis(
        subject_id="BL-A",
        proposed_bank_line_id="BL-A",
        proposed_psp_txn_ids=list(_K9_IDS),
        proposed_order_ids=[],
        reasoning="",
        self_confidence=0.9,
    )
    r = verify(h, ctx_with_two_equal_candidates)
    assert r.accepted is False and r.failed_check == "uniqueness"


def test_uniqueness_accepts_the_same_hypothesis_when_only_one_candidate_closes(
    ctx_with_one_equal_candidate,
):
    """The companion to the trap case: byte-for-byte the same hypothesis, one
    fewer candidate settlement. Without this, `uniqueness` rejecting everything
    would pass the test above."""
    h = Hypothesis(
        subject_id="BL-A",
        proposed_bank_line_id="BL-A",
        proposed_psp_txn_ids=list(_K9_IDS),
        proposed_order_ids=[],
        reasoning="",
        self_confidence=0.9,
    )
    r = verify(h, ctx_with_one_equal_candidate)
    assert r.accepted is True and r.failed_check is None


# --- settlement coherence: the proposal must BE a settlement ----------------
#
# The first four checks each test the proposed set in isolation, and `uniqueness`
# enumerates whole entries of `txns_by_settlement`. Neither ever asks whether the
# PROPOSED set is one of those entries -- so a cherry-picked leg set that closes
# the arithmetic is invisible to all five.

# setl_P closes a 1,000,000 credit. setl_R (700,000) and setl_S (1,500,000) do
# not -- but one leg from each sums to exactly 1,000,000.
_SETL_P = [_txn("pay_P", "payment", 1_000_000, settlement_id="setl_P")]
_SETL_R = [
    _txn("pay_R", "payment", 400_000, settlement_id="setl_R"),
    _txn("pay_R2", "payment", 300_000, settlement_id="setl_R"),
]
_SETL_S = [
    _txn("pay_S", "payment", 600_000, settlement_id="setl_S"),
    _txn("pay_S2", "payment", 900_000, settlement_id="setl_S"),
]

# setl_Q nets 972,152 after its fee and tax legs. A model that drops those two
# legs proposes a set that closes a credit equal to the GROSS.
_SETL_Q = [
    _txn("pay_Q", "payment", 1_000_000, settlement_id="setl_Q"),
    _txn("fee_Q", "fee", -23_600, settlement_id="setl_Q"),
    _txn("tax_Q", "tax", -4_248, settlement_id="setl_Q"),
]


@pytest.fixture
def ctx_with_three_settlements() -> VerifyContext:
    return _ctx(_SETL_P + _SETL_R + _SETL_S, [_line("BL-X", 1_000_000)])


def test_rejects_a_cherry_picked_cross_settlement_leg_set(ctx_with_three_settlements):
    """One leg from setl_R plus one from setl_S sums to the credit, so all four
    earlier checks pass, and `uniqueness` counts only setl_P -- one closer, so it
    passes too. Two mutually exclusive hypotheses accepted for one bank line is
    trap_capture_rate = 0.0 by a route the check structurally cannot see."""
    h = Hypothesis(
        subject_id="BL-X",
        proposed_bank_line_id="BL-X",
        proposed_psp_txn_ids=["pay_R", "pay_S"],
        proposed_order_ids=[],
        reasoning="these two add up",
        self_confidence=0.9,
    )
    r = verify(h, ctx_with_three_settlements)
    assert r.accepted is False and r.failed_check == "existence"
    # Pins the span branch specifically. Without it the proposal still gets
    # rejected -- by the leg-set comparison, against whichever settlement
    # `next(iter(...))` happened to pick out of a set -- so the label alone
    # cannot tell the two apart, and that fallback is non-deterministic.
    assert "span 2 settlements" in r.reason


def test_accepts_the_whole_settlement_that_closes_the_same_line(
    ctx_with_three_settlements,
):
    """The discrimination companion: coherence must reject the cherry-pick
    WITHOUT rejecting the real answer sitting in the same context."""
    h = Hypothesis(
        subject_id="BL-X",
        proposed_bank_line_id="BL-X",
        proposed_psp_txn_ids=["pay_P"],
        proposed_order_ids=[],
        reasoning="setl_P reconstructs to the credit",
        self_confidence=0.9,
    )
    r = verify(h, ctx_with_three_settlements)
    assert r.accepted is True and r.failed_check is None


def test_rejects_an_incomplete_settlement_leg_set():
    """`pay_Q` alone closes a credit equal to setl_Q's gross, because the fee and
    tax legs were dropped. No settlement closes this line, so `uniqueness` counts
    zero and waves it through."""
    ctx = _ctx(list(_SETL_Q), [_line("BL-Q", 1_000_000)])
    h = Hypothesis(
        subject_id="BL-Q",
        proposed_bank_line_id="BL-Q",
        proposed_psp_txn_ids=["pay_Q"],
        proposed_order_ids=[],
        reasoning="the payment matches the credit",
        self_confidence=0.9,
    )
    r = verify(h, ctx)
    assert r.accepted is False and r.failed_check == "existence"


def test_rejects_legs_that_carry_no_settlement_id():
    """Legs with `settlement_id is None` are never in `txns_by_settlement`, so
    any combination of them that closes the arithmetic is accepted with zero
    closers. `fixtures/tiny/psp.csv` and `fixtures/seed42-500/psp.csv` both carry
    such rows, and the prompt gives the model a section listing them."""
    orphans = [
        _txn("orph_1", "payment", 500_000, settlement_id=None),
        _txn("orph_2", "payment", 500_000, settlement_id=None),
    ]
    ctx = _ctx(orphans, [_line("BL-O", 1_000_000)])
    assert ctx.txns_by_settlement == {}, "orphan legs are not a settlement"
    h = Hypothesis(
        subject_id="BL-O",
        proposed_bank_line_id="BL-O",
        proposed_psp_txn_ids=["orph_1", "orph_2"],
        proposed_order_ids=[],
        reasoning="two loose legs add up",
        self_confidence=0.9,
    )
    r = verify(h, ctx)
    assert r.accepted is False and r.failed_check == "existence"
    # Pins the settlement_id-is-None branch specifically. Without it the
    # proposal still gets rejected, but via `None not in txns_by_settlement`,
    # which reports a missing settlement rather than loose legs.
    assert "carry no settlement_id" in r.reason


def test_rejects_a_leg_that_never_settled():
    """`settled_at is None` means the leg never settled, and money that never
    settled cannot have funded a bank credit. The original causality predicate
    (`s is not None and s > line.txn_date`) waved it straight through."""
    unsettled = [
        _txn("pay_U", "payment", 1_000_000, settlement_id="setl_U", settled_at=None)
    ]
    ctx = _ctx(unsettled, [_line("BL-U", 1_000_000)])
    h = Hypothesis(
        subject_id="BL-U",
        proposed_bank_line_id="BL-U",
        proposed_psp_txn_ids=["pay_U"],
        proposed_order_ids=[],
        reasoning="the amount matches",
        self_confidence=0.9,
    )
    r = verify(h, ctx)
    assert r.accepted is False and r.failed_check == "causality"


def test_a_stale_settlement_passes_causality_by_design():
    """`_causality` bounds LATENESS only, and has no lower bound at all.

    A settlement that settled thirty days before the credit is accepted here and
    would have been refused by T1, T2 and T3 on `CandidatePool.within_window`'s
    symmetric ±2 days. **This test pins that asymmetry as intended**, so it
    cannot be closed by accident, and so a reader who finds it does not have to
    guess whether anyone noticed.

    Closing it was implemented and measured before being reverted
    (`DEFECT-CLOSEOUT-REPORT.md` D1). Applying the tiers' window here takes 13
    tests red across `tests/llm/test_pipeline.py` and
    `tests/llm/test_obfuscated_ref.py`, and the reason is structural rather than
    fixture-deep: with the window applied, the verifier's admissible band is a
    strict subset of what T3 matches deterministically, so the analyst layer has
    no input left that the engine had not already resolved. The date window is
    the entire space in which an analyst layer can operate here.

    What the verifier substitutes for the window is two constraints no tier has:
    `_coherence` (the proposal must BE one complete settlement) and
    `_uniqueness` (the only unclaimed settlement closing the line **across the
    whole file**, not merely within a two-day window -- so it counts strictly
    more competitors than T3's ambiguity rule). Neither path is uniformly more
    permissive than the other, which is why `TOLERANCE_PAISE`'s comment now
    scopes its claim to the amount tolerance instead of asserting one.

    If this test ever fails, the asymmetry has been closed: read D1 first, and
    check `llm` recall before assuming it was an improvement.
    """
    stale = [
        _txn("pay_S", "payment", 1_000_000, settlement_id="setl_S",
             settled_at="2026-06-24")
    ]
    ctx = _ctx(stale, [_line("BL-S", 1_000_000, txn_date="2026-07-24")])
    h = Hypothesis(
        subject_id="BL-S",
        proposed_bank_line_id="BL-S",
        proposed_psp_txn_ids=["pay_S"],
        proposed_order_ids=[],
        reasoning="the amount matches exactly",
        self_confidence=0.9,
    )
    r = verify(h, ctx)
    assert r.accepted is True, "the 30-day-stale settlement is accepted, by design"

    # And the deterministic tiers would NOT have taken it -- which is the half
    # that makes this an asymmetry rather than a shared rule. Asserted against
    # the tiers' own constant so the gap is stated in the terms it exists in.
    from core.matcher.tiers import WINDOW_DAYS as TIER_WINDOW_DAYS

    gap = (ctx.bank_lines_by_id["BL-S"].txn_date - stale[0].settled_at).days
    assert gap > TIER_WINDOW_DAYS


def test_rejects_a_proposal_completed_only_by_a_partly_claimed_map():
    """`txns_by_settlement` must not be the authority on what a settlement
    CONTAINS -- only on whether it was ingested.

    If Lane D assembles the map from unclaimed legs (the exact mistake the
    VerifyContext docstring warns about), a partly-claimed settlement appears in
    it as a complete one, and the remainder is laundered into a whole settlement
    that closes the gross. The C1 class returns through a map-shape bug.

    An EMPTY map fails closed and loudly; a PARTIALLY populated one fails open
    silently, which is why the expected leg set is derived from `txns_by_id` --
    a source `_existence` already forces to be complete.
    """
    legs = [
        _txn("pay_W", "payment", 1_000_000, settlement_id="setl_W"),
        _txn("fee_W", "fee", -23_600, settlement_id="setl_W"),
    ]
    ctx = _ctx(
        legs,
        [_line("BL-W", 1_000_000)],  # the settlement's GROSS, not its net
        claimed={"fee_W"},
        settlements={"setl_W": [legs[0]]},  # the map omits the claimed leg
    )
    h = Hypothesis(
        subject_id="BL-W",
        proposed_bank_line_id="BL-W",
        proposed_psp_txn_ids=["pay_W"],
        proposed_order_ids=[],
        reasoning="the remainder of setl_W",
        self_confidence=0.9,
    )
    r = verify(h, ctx)
    assert r.accepted is False and r.failed_check == "existence"
    assert "fee_W" in r.reason, "the reason must name the leg that was omitted"


def test_rejects_duplicate_transaction_ids():
    """`_existence` and `_exclusivity` are membership tests, which a repeated id
    passes trivially, and the reconstruct list is built by comprehension with no
    de-dup -- so naming one leg three times triples the net. `claimed_txn_ids`
    cannot help: it holds ids claimed by PREVIOUSLY accepted matches, not ids
    repeated within the hypothesis under test. Coherence cannot help either,
    because `set(ids)` collapses the duplicates before the comparison."""
    ctx = _ctx(
        [_txn("pay_D", "payment", 500_000, settlement_id="setl_D")],
        [_line("BL-D", 1_500_000)],
    )
    h = Hypothesis(
        subject_id="BL-D",
        proposed_bank_line_id="BL-D",
        proposed_psp_txn_ids=["pay_D", "pay_D", "pay_D"],
        proposed_order_ids=[],
        reasoning="three times the leg",
        self_confidence=0.9,
    )
    r = verify(h, ctx)
    assert r.accepted is False and r.failed_check == "existence"


@pytest.mark.parametrize(
    ("delta", "expected_accepted"), [(99, True), (101, False)]
)
def test_the_tolerance_boundary_is_one_rupee(delta, expected_accepted):
    """Pins TOLERANCE_PAISE at 100 from both sides. Without a boundary pair the
    constant is unguarded across four orders of magnitude: the suite stayed green
    with the tolerance set anywhere from 0 to 100,000 paise, because the only
    arithmetic-failing fixture was off by 830,380."""
    legs = [
        _txn("pay_T", "payment", 1_000_000, settlement_id="setl_T"),
        _txn("fee_T", "fee", -23_600, settlement_id="setl_T"),
        _txn("tax_T", "tax", -4_248, settlement_id="setl_T"),
    ]
    net = 972_152  # 1_000_000 - 23_600 - 4_248
    ctx = _ctx(legs, [_line("BL-T", net + delta)])
    h = Hypothesis(
        subject_id="BL-T",
        proposed_bank_line_id="BL-T",
        proposed_psp_txn_ids=["pay_T", "fee_T", "tax_T"],
        proposed_order_ids=[],
        reasoning="a rounding break",
        self_confidence=0.9,
    )
    r = verify(h, ctx)
    assert r.accepted is expected_accepted
    if not expected_accepted:
        assert r.failed_check == "arithmetic"


def test_a_claimed_candidate_settlement_does_not_count_toward_ambiguity():
    """`_uniqueness` skips settlements whose legs are already claimed. Deleting
    that skip left every test green, yet it is the whole reason Lane D populates
    `txns_by_settlement` with matched settlements included and lets
    `claimed_txn_ids` do the excluding -- doing the exclusion in both places is
    how the two lanes drift apart.

    Claiming ONE leg of setl_M2 must retire the whole settlement as a candidate,
    so the otherwise-ambiguous setl_K9 proposal becomes acceptable."""
    ctx = _ctx(
        _SETL_K9 + _SETL_M2, [_line("BL-A", _TRAP_NET)], claimed={"pay_M1"}
    )
    h = Hypothesis(
        subject_id="BL-A",
        proposed_bank_line_id="BL-A",
        proposed_psp_txn_ids=list(_K9_IDS),
        proposed_order_ids=[],
        reasoning="",
        self_confidence=0.9,
    )
    r = verify(h, ctx)
    assert r.accepted is True and r.failed_check is None


def test_rejects_a_bank_line_with_no_credit():
    """`BankLine.credit` is `Money | None`. On a debit-only line `credit or 0`
    made the target zero, so any proposal netting to zero closed it exactly --
    here a complete, coherent settlement whose payment and refund legs cancel.
    `core/llm/prompts.py` renders debit lines to the model, so the analyst is
    actively invited to propose against them."""
    cancelling = [
        _txn("pay_Z", "payment", 100_000, settlement_id="setl_Z"),
        _txn("rfnd_Z", "refund", -100_000, settlement_id="setl_Z"),
    ]
    debit_line = BankLine(
        line_id="BL-DEBIT",
        txn_date="2026-07-24",
        narration="NEFT DEBIT",
        credit=None,
        debit=500_000,
        balance=0,
        utr=None,
    )
    ctx = _ctx(cancelling, [debit_line])
    h = Hypothesis(
        subject_id="BL-DEBIT",
        proposed_bank_line_id="BL-DEBIT",
        proposed_psp_txn_ids=["pay_Z", "rfnd_Z"],
        proposed_order_ids=[],
        reasoning="nets to zero",
        self_confidence=0.9,
    )
    r = verify(h, ctx)
    assert r.accepted is False and r.failed_check == "existence"


@pytest.mark.parametrize(
    ("credit", "legs"),
    [
        (0, [("pay_C0", "payment", 100_000), ("rfnd_C0", "refund", -100_000)]),
        (-50_000, [("rfnd_CN", "refund", -50_000)]),
    ],
    ids=["zero-credit", "negative-credit"],
)
def test_rejects_a_bank_line_whose_credit_is_not_positive(credit, legs):
    """Rejecting `credit is None` still left `credit == 0` as a zero target, so a
    coherent settlement whose legs cancel closes it exactly -- and a negative
    credit is closed by a negative net the same way. `_paise` accepts "0" and
    `BankLine` carries no non-negative validator, so nothing upstream forbids
    either. Latent rather than live: no shipped generator emits one."""
    txns = [
        _txn(txn_id, txn_type, amount, settlement_id="setl_C")
        for txn_id, txn_type, amount in legs
    ]
    ctx = _ctx(txns, [_line("BL-C", credit)])
    h = Hypothesis(
        subject_id="BL-C",
        proposed_bank_line_id="BL-C",
        proposed_psp_txn_ids=[t.txn_id for t in txns],
        proposed_order_ids=[],
        reasoning="the arithmetic closes on nothing",
        self_confidence=0.9,
    )
    r = verify(h, ctx)
    assert r.accepted is False and r.failed_check == "existence"


def test_rejects_an_empty_proposal(ctx_with_batch):
    """An empty proposal resolves nothing and must never be a verdict of
    'accepted'. It also has to be caught in `_existence` rather than falling
    through to a later check: on a zero target every later check passes.

    The reason is asserted as well as the label because `_existence` and
    `_coherence` share the `existence` spelling, and only the reason text
    distinguishes which branch actually fired. That is a unit test pinning an
    internal branch -- the UI must still never parse `verifier_reason`."""
    h = Hypothesis(
        subject_id="BL-1",
        proposed_bank_line_id="BL-1",
        proposed_psp_txn_ids=[],
        proposed_order_ids=[],
        reasoning="nothing at all",
        self_confidence=0.9,
    )
    r = verify(h, ctx_with_batch)
    assert r.accepted is False and r.failed_check == "existence"
    assert "empty" in r.reason.lower()


# --- acceptance, confidence, and the crash that must not happen -------------


def test_accepts_a_correct_hypothesis(ctx_with_batch, valid_hypothesis):
    r = verify(valid_hypothesis, ctx_with_batch)
    assert r.accepted is True and r.failed_check is None


def _case_existence():
    ctx = _ctx(list(_SETL_A), [_line("BL-1", _SETL_A_NET)])
    return ctx, "BL-1", ["pay_NOPE"], "existence"


def _case_exclusivity():
    ctx = _ctx(list(_SETL_A), [_line("BL-2", _SETL_A_NET)], claimed={"pay_1"})
    return ctx, "BL-2", ["pay_1", "fee_1", "tax_1"], "exclusivity"


def _case_causality():
    legs = [
        _txn("pay_late", "payment", 100_000, settlement_id="setl_L",
             settled_at="2026-07-25")
    ]
    ctx = _ctx(legs, [_line("BL-L", 100_000, txn_date="2026-07-24")])
    return ctx, "BL-L", ["pay_late"], "causality"


def _case_arithmetic():
    ctx = _ctx(list(_SETL_A), [_line("BL-1", _SETL_A_NET)])
    return ctx, "BL-1", ["pay_1"], "arithmetic"


def _case_uniqueness():
    ctx = _ctx(_SETL_K9 + _SETL_M2, [_line("BL-A", _TRAP_NET)])
    return ctx, "BL-A", list(_K9_IDS), "uniqueness"


@pytest.mark.parametrize(
    "case",
    [_case_existence, _case_exclusivity, _case_causality, _case_arithmetic,
     _case_uniqueness],
    ids=["existence", "exclusivity", "causality", "arithmetic", "uniqueness"],
)
def test_high_self_confidence_does_not_bypass_any_check(case):
    """The model's own confidence is never an input to acceptance -- and "any
    check" means every one of the five labels, not just whichever one a single
    fixture happens to trip. A hypothesis asserting total certainty is rejected
    exactly as hard as a diffident one."""
    ctx, line_id, txn_ids, expected = case()
    h = Hypothesis(
        subject_id=line_id,
        proposed_bank_line_id=line_id,
        proposed_psp_txn_ids=txn_ids,
        proposed_order_ids=[],
        reasoning="I am certain",
        self_confidence=1.0,
    )
    r = verify(h, ctx)
    assert r.accepted is False and r.failed_check == expected


@pytest.mark.parametrize("bad_line_id", ["BL-DOES-NOT-EXIST", None])
def test_rejects_a_bad_bank_line_id_without_raising(ctx_with_batch, bad_line_id):
    """proposed_bank_line_id is `str | None`, and causality/arithmetic/uniqueness
    all index ctx.bank_lines_by_id with it. An invented or missing id must come
    back as a verdict, never as a KeyError -- a crash here takes the whole run
    down over one bad hypothesis. Deliberately NOT wrapped in pytest.raises."""
    h = Hypothesis(
        subject_id="BL-1",
        proposed_bank_line_id=bad_line_id,
        proposed_psp_txn_ids=[],
        proposed_order_ids=[],
        reasoning="",
        self_confidence=0.9,
    )
    r = verify(h, ctx_with_batch)  # must not raise
    assert r.accepted is False and r.failed_check == "existence"
