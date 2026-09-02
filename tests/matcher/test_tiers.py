"""Tier tests (spec 7).

The three rules under test that are non-negotiable, in the order they cost most
if they are wrong:

1. **Candidacy is cardinality-blind.** More than one candidate for a subject
   means the subject matches nothing -- counted over the WHOLE candidate set,
   never within a cardinality-filtered subset.
2. **T0 needs the arithmetic as well as the reference.** A settlement id in a
   narration is evidence of identity, never a substitute for the sum.
3. **T1 and T2 differ by payment-leg cardinality, not by method.**
"""

from __future__ import annotations

import pytest

from core.audit import AuditLog
from core.matcher.tiers import TIERS, T0, T1, T2, T3

from .conftest import build_settlement, make_bank_line, make_pool, net_of


# --- T0 -----------------------------------------------------------------------


def test_t0_matches_on_settlement_id_in_narration(pool_with_setl_in_narration):
    matches = T0.match(pool_with_setl_in_narration, AuditLog("r"))
    assert len(matches) == 1
    assert matches[0].tier == "T0" and matches[0].confidence == 1.0
    assert matches[0].settlement_id == "setl_X1"


def test_t0_falls_through_when_the_arithmetic_does_not_close(
    pool_with_setl_ref_and_50p_break,
):
    """A settlement id in a narration is evidence of identity, never a
    substitute for the arithmetic. `setl_D4` in the fixture is exactly this:
    the narration names it and it reconstructs 50 paise high. T0 must decline
    so T3 can claim it."""
    assert T0.match(pool_with_setl_ref_and_50p_break, AuditLog("r")) == []


def test_t0_records_why_it_declined(pool_with_setl_ref_and_50p_break):
    log = AuditLog("r")
    T0.match(pool_with_setl_ref_and_50p_break, log)
    trail = " ".join(e.evidence for e in log.entries_for("BL-9002"))
    assert "setl_X2" in trail, "the reference hit must be recorded even when declined"
    assert "50" in trail, "the residual must be recorded"


def test_t0_matches_on_a_settlement_id_carried_in_the_utr_field():
    legs = build_settlement("setl_U1", [2_500_000])
    pool = make_pool(
        legs,
        [make_bank_line("BL-9101", net_of(legs), narration="NEFT CR", utr="setl_U1")],
    )
    matches = T0.match(pool, AuditLog("r"))
    assert [m.settlement_id for m in matches] == ["setl_U1"]


def test_t0_ignores_a_reference_to_a_settlement_that_is_not_in_the_psp_report():
    legs = build_settlement("setl_R1", [2_500_000])
    pool = make_pool(
        legs,
        [
            make_bank_line(
                "BL-9102",
                net_of(legs),
                narration="NEFT CR RAZORPAY SETL setl_NOT_HERE",
            )
        ],
    )
    assert T0.match(pool, AuditLog("r")) == []


# --- T1 / T2 cardinality ------------------------------------------------------


def test_t1_matches_a_single_payment_leg_settlement(pool_with_one_payment_leg):
    """T1 and T2 differ by CARDINALITY, not method: one payment leg is T1."""
    matches = T1.match(pool_with_one_payment_leg, AuditLog("r"))
    assert len(matches) == 1
    assert matches[0].tier == "T1" and matches[0].confidence == 0.95


def test_t2_declines_the_single_payment_leg_settlement(pool_with_one_payment_leg):
    assert T2.match(pool_with_one_payment_leg, AuditLog("r")) == []


def test_t2_matches_on_reconstructed_net(pool_with_netted_batch):
    matches = T2.match(pool_with_netted_batch, AuditLog("r"))
    assert len(matches) == 1
    assert matches[0].tier == "T2" and matches[0].confidence == 0.99
    assert (
        matches[0].net
        == matches[0].gross
        - matches[0].fees
        - matches[0].tax
        - matches[0].refunds
        - matches[0].holds
    )


def test_t1_declines_a_multi_payment_leg_settlement(pool_with_netted_batch):
    """T1 runs first. If it claimed a batch, T2 -- the core of the project --
    would never fire, which is the exact bug the cardinality ruling removes."""
    assert T1.match(pool_with_netted_batch, AuditLog("r")) == []


def test_deduction_legs_do_not_count_toward_cardinality(pool_with_one_payment_leg):
    """One payment, one fee and one tax leg is T1: the deduction legs are the
    arithmetic, not the batch."""
    pool = pool_with_one_payment_leg
    assert len(pool.legs("setl_X3")) == 3
    assert pool.payment_legs("setl_X3") == 1


def test_a_refund_leg_does_not_make_a_settlement_a_batch():
    """The cardinality set is `payment` alone -- narrower than the
    order-bearing set, which also contains `refund` and `chargeback`."""
    from .conftest import make_txn

    refund = make_txn(
        "rfnd_S1", "refund", -100_000, order_id="ORD-OLD", settlement_id="setl_S1"
    )
    legs = build_settlement("setl_S1", [2_500_000], extra_legs=[refund])
    pool = make_pool(legs, [make_bank_line("BL-9103", net_of(legs))])
    assert pool.payment_legs("setl_S1") == 1
    assert [m.tier for m in T1.match(pool, AuditLog("r"))] == ["T1"]


# --- the ambiguity rule -------------------------------------------------------


def test_cardinality_never_partitions_the_candidate_pool(
    pool_with_two_candidates_of_different_cardinality,
):
    """The ambiguity trap's two candidates differ in payment-leg count. A tier
    that filters candidates BY cardinality sees one candidate and matches it --
    turning the cardinality split into a tie-breaker and `trap_capture_rate`
    into 0.0. Candidacy is cardinality-blind; cardinality only labels the
    winner.

    ONE subject, deliberately. With two subjects the contest rule declines both
    proposals for an unrelated reason and the test passes on the broken
    implementation -- see `test_the_full_trap_shape_...` below, which is the
    weaker realistic case, not the discriminating one.
    """
    pool = pool_with_two_candidates_of_different_cardinality
    assert T1.match(pool, AuditLog("r")) == []
    assert T2.match(pool, AuditLog("r")) == []
    assert T3.match(pool, AuditLog("r")) == []
    assert set(pool.ambiguous_candidates("BL-9005")) == {"setl_P1", "setl_P2"}


def test_the_whole_tier_stack_leaves_the_different_cardinality_trap_alone(
    pool_with_two_candidates_of_different_cardinality,
):
    """Run every tier in order on one pool, the way the engine does. A pool
    that survives T1 only to be claimed by T3 is the same failure one tier
    later."""
    pool = pool_with_two_candidates_of_different_cardinality
    log = AuditLog("r")
    produced = [m for tier in TIERS for m in tier.match(pool, log)]
    assert produced == []
    assert pool.ambiguous_candidates("BL-9005"), "the ambiguity must be recorded"


def test_the_full_trap_shape_is_left_entirely_unmatched(pool_with_the_full_trap_shape):
    """The fixture's trap as it stands: two indistinguishable bank lines, two
    settlements of different cardinality. Both lines must survive every tier."""
    pool = pool_with_the_full_trap_shape
    log = AuditLog("r")
    assert [m for tier in TIERS for m in tier.match(pool, log)] == []
    assert pool.was_undecidable("BL-9005") and pool.was_undecidable("BL-9006")


def test_ambiguity_matches_nothing(pool_with_two_identical_candidates):
    """Guessing under ambiguity is how false matches are created."""
    assert T2.match(pool_with_two_identical_candidates, AuditLog("r")) == []


def test_two_subjects_competing_for_one_candidate_match_nothing():
    """The mirror image of the ambiguity rule. Two bank lines, one settlement
    that closes both: whichever is processed first would claim it, which makes
    iteration order the tie-breaker. Nothing may be matched."""
    legs = build_settlement("setl_C1", [1_600_000, 900_000])
    credit = net_of(legs)
    pool = make_pool(
        legs, [make_bank_line("BL-9104", credit), make_bank_line("BL-9105", credit)]
    )
    assert T2.match(pool, AuditLog("r")) == []


# --- T3 -----------------------------------------------------------------------


def test_t3_accepts_half_rupee_break_and_records_delta(pool_with_50p_break):
    matches = T3.match(pool_with_50p_break, AuditLog("r"))
    assert len(matches) == 1
    assert matches[0].tier == "T3" and matches[0].confidence == 0.80
    assert any("delta=50" in e for e in matches[0].evidence)


def test_t3_reports_net_as_the_bank_credit(pool_with_50p_break):
    """`MatchGroup.net` must equal the bank line credit (core/models.py:80,
    spec 6.3, api/openapi.yaml). On a break the reconstruction and the credit
    disagree, and it is the credit that the field carries -- the residual goes
    into evidence, never into the net."""
    (match,) = T3.match(pool_with_50p_break, AuditLog("r"))
    (line,) = pool_with_50p_break.bank_lines
    assert match.net == line.credit


def test_t3_rejects_break_above_one_rupee(pool_with_150p_break):
    assert T3.match(pool_with_150p_break, AuditLog("r")) == []


def test_t3_is_cardinality_agnostic(pool_with_50p_break):
    """`setl_D4` is single-payment-leg and carries the rounding break.
    Restricting T3 to T2's cardinality would strand it."""
    assert pool_with_50p_break.payment_legs("setl_X5") == 1
    assert len(T3.match(pool_with_50p_break, AuditLog("r"))) == 1


# --- the date window ----------------------------------------------------------


@pytest.mark.parametrize(
    ("txn_date", "expected"),
    [
        ("2026-07-22", 1),
        ("2026-07-26", 1),
        ("2026-07-21", 0),
        ("2026-07-27", 0),
    ],
)
def test_settled_at_must_be_within_two_days_of_the_bank_date(txn_date, expected):
    legs = build_settlement("setl_W1", [2_500_000], settled_at="2026-07-24")
    pool = make_pool(legs, [make_bank_line("BL-9106", net_of(legs), txn_date=txn_date)])
    assert len(T1.match(pool, AuditLog("r"))) == expected


def test_t0_does_not_require_the_date_window():
    """T0's rule is a reference plus the arithmetic (spec 7). A late-posting
    bank line with an explicit settlement reference is not ambiguous."""
    legs = build_settlement("setl_W2", [2_500_000], settled_at="2026-07-24")
    pool = make_pool(
        legs,
        [
            make_bank_line(
                "BL-9107",
                net_of(legs),
                txn_date="2026-08-15",
                narration="NEFT CR SETL setl_W2",
            )
        ],
    )
    assert len(T0.match(pool, AuditLog("r"))) == 1


# --- claiming and the audit trail --------------------------------------------


def test_a_matched_settlement_is_removed_from_the_pool(pool_with_setl_in_narration):
    pool = pool_with_setl_in_narration
    T0.match(pool, AuditLog("r"))
    assert pool.unclaimed_settlements() == []
    assert pool.open_bank_lines() == []


def test_every_tier_records_an_entry_whether_or_not_it_matched(
    pool_with_150p_break,
):
    log = AuditLog("r")
    for tier in TIERS:
        tier.match(pool_with_150p_break, log)
    entries = log.entries_for("BL-9008")
    assert {e.rule.split(":")[0] for e in entries} == {"T0", "T1", "T2", "T3"}
    assert all(e.stage == "match" and e.actor == "deterministic" for e in entries)


def test_a_debit_only_line_is_never_a_candidate_subject():
    legs = build_settlement("setl_Z1", [2_500_000])
    pool = make_pool(
        legs, [make_bank_line("BL-9108", None, debit=net_of(legs), balance=0)]
    )
    for tier in TIERS:
        assert tier.match(pool, AuditLog("r")) == []


def test_match_groups_carry_the_settlement_legs_and_orders(pool_with_netted_batch):
    (match,) = T2.match(pool_with_netted_batch, AuditLog("r"))
    assert set(match.psp_txn_ids) == {t.txn_id for t in pool_with_netted_batch.legs("setl_X4")}
    assert match.bank_line_id == "BL-9004"
    assert match.evidence, "a match must explain itself"


# --- the audit line a maintainer actually reads -------------------------------


def test_the_ambiguity_line_does_not_misreport_the_candidates_cardinality(
    pool_with_two_candidates_of_different_cardinality,
):
    """This is the one place a maintainer looks to confirm cardinality-blindness.

    It used to render `2 equally valid candidates ['setl_P1', 'setl_P2'] at
    exactly one payment leg` -- because the trailing clause was T1's own LABEL
    rule spliced onto a sentence about the candidate set. `setl_P1` has TWO
    payment legs and was deliberately not filtered out, so the line asserted
    the exact opposite of the property it exists to evidence.

    An audit line that states a false fact about the data is a defect, not a
    typo.
    """
    pool = pool_with_two_candidates_of_different_cardinality
    log = AuditLog("r")
    T1.match(pool, log)

    (entry,) = [e for e in log.entries() if e.rule == "T1:ambiguous"]

    assert "at exactly one payment leg" not in entry.evidence
    # Both candidates are named with their real, differing leg counts.
    assert "setl_P1 (2 payment legs)" in entry.evidence
    assert "setl_P2 (1 payment leg)" in entry.evidence
    assert "any payment-leg count" in entry.evidence.lower()


def test_the_ambiguity_line_still_names_the_tiers_label_rule(
    pool_with_two_candidates_of_different_cardinality,
):
    """Naming the label rule is useful; presenting it as the search filter was
    the bug. It must be stated as what runs AFTER a single candidate survives."""
    pool = pool_with_two_candidates_of_different_cardinality
    log = AuditLog("r")
    T1.match(pool, log)

    (entry,) = [e for e in log.entries() if e.rule == "T1:ambiguous"]
    assert "exactly one payment leg" in entry.evidence, "the label rule is named"
    assert "after" in entry.evidence.lower()
