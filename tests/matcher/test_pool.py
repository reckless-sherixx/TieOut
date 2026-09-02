"""Pool-level rules that the tier tests cannot reach.

Two of the pool's three derived views make a *choice* -- which order sits
behind an anonymous leg, and which row of a duplicate pair survives. Both
choices used to be settled by iteration order, which is the one thing
LANE-B-matcher.md forbids ("not id order, not statement order, not the first
one seen"). The tests here are written as **permutation** tests wherever a
choice is involved: the same rows in a different order must produce the same
answer, and where the data does not determine an answer the pool must decline
rather than produce a reproducible guess.

A reproducible guess is still a guess. `txn_id` order makes a wrong answer
stable, not correct.
"""

from __future__ import annotations

from core.matcher.pool import CandidatePool

from .conftest import build_settlement, make_bank_line, make_order, make_txn, net_of


def recoveries_by_txn(pool: CandidatePool) -> dict[str, str | None]:
    return {r.txn_id: r.order_id for r in pool.order_recoveries}


def build_pool(orders, txns, bank_lines=()) -> CandidatePool:
    return CandidatePool(
        orders=list(orders), psp_txns=list(txns), bank_lines=list(bank_lines)
    )


# --- order recovery: the tie must not be broken by iteration order -----------


def two_anonymous_legs_and_two_orders() -> tuple[list, list]:
    """Two `payment` legs identical in every exposed field, and two unclaimed
    orders carrying that same gross. Nothing distinguishes either pairing."""
    orders = [
        make_order("ORD-AAA", 720_000, order_date="2026-07-21"),
        make_order("ORD-BBB", 720_000, order_date="2026-07-21"),
    ]
    legs = [
        make_txn(
            "pay_L1",
            "payment",
            720_000,
            order_id=None,
            settlement_id="setl_R1",
            captured_at="2026-07-22T11:05:47",
        ),
        make_txn(
            "pay_L2",
            "payment",
            720_000,
            order_id=None,
            settlement_id="setl_R2",
            captured_at="2026-07-22T11:05:47",
        ),
    ]
    return orders, legs


def test_two_legs_contesting_the_same_orders_recover_nothing():
    """The ambiguity rule, applied on the order side.

    Two legs, two candidate orders, no signal separating them. Assigning
    `pay_L1 -> ORD-AAA` and `pay_L2 -> ORD-BBB` is only defensible if you accept
    statement order as evidence, and the brief says it is not. Neither leg may
    recover.
    """
    orders, legs = two_anonymous_legs_and_two_orders()
    pool = build_pool(orders, legs)

    assert recoveries_by_txn(pool) == {"pay_L1": None, "pay_L2": None}
    assert pool.recovered_order("pay_L1") is None
    assert pool.recovered_order("pay_L2") is None


def test_recovery_is_invariant_under_row_order():
    """Swapping the two PSP rows in the file must not swap the assignment.

    The near/far shape is what makes this bite. With both orders inside the
    window neither leg narrows to one and the naive loop declines both anyway;
    with one order inside the window the first leg read takes it, the second is
    handed the leftover, and reversing the rows reverses the answer.
    """
    orders = [
        make_order("ORD-NEAR", 720_000, order_date="2026-07-20"),
        make_order("ORD-FAR", 720_000, order_date="2026-05-01"),
    ]
    _, legs = two_anonymous_legs_and_two_orders()

    forward = recoveries_by_txn(build_pool(orders, legs))
    reversed_rows = recoveries_by_txn(build_pool(orders, list(reversed(legs))))
    reversed_orders = recoveries_by_txn(build_pool(list(reversed(orders)), legs))

    assert forward == reversed_rows == reversed_orders


def test_recovery_is_invariant_under_row_order_on_a_flat_tie():
    orders, legs = two_anonymous_legs_and_two_orders()

    forward = recoveries_by_txn(build_pool(orders, legs))
    reversed_rows = recoveries_by_txn(build_pool(orders, list(reversed(legs))))
    reversed_orders = recoveries_by_txn(build_pool(list(reversed(orders)), legs))

    assert forward == reversed_rows == reversed_orders


def test_the_seven_day_window_narrows_but_never_licenses_a_leftover():
    """The shape the reviewer demonstrated: one near order, one far order.

    The near order is the only one inside the 7-day window, so it is the only
    candidate either leg can defend -- and both legs want it equally. The far
    order must NOT become `pay_L2`'s answer just because `pay_L1` took the near
    one first: an order the window already rejected does not become plausible
    because it is the last one left.
    """
    orders = [
        make_order("ORD-NEAR", 720_000, order_date="2026-07-20"),
        make_order("ORD-FAR", 720_000, order_date="2026-05-01"),
    ]
    _, legs = two_anonymous_legs_and_two_orders()
    pool = build_pool(orders, legs)

    assert recoveries_by_txn(pool) == {"pay_L1": None, "pay_L2": None}
    assert "ORD-FAR" not in set(recoveries_by_txn(pool).values())


def test_a_genuinely_unique_remainder_is_still_recovered():
    """The rule must decline ties, not decline everything.

    One anonymous leg, one unclaimed order at that gross, and a second order
    already spoken for by a leg that names it. The answer is determined, so it
    is taken.
    """
    orders = [
        make_order("ORD-TAKEN", 720_000, order_date="2026-07-21"),
        make_order("ORD-FREE", 720_000, order_date="2026-07-21"),
    ]
    legs = [
        make_txn(
            "pay_named", "payment", 720_000, order_id="ORD-TAKEN", settlement_id="setl_R1"
        ),
        make_txn("pay_anon", "payment", 720_000, order_id=None, settlement_id="setl_R1"),
    ]
    pool = build_pool(orders, legs)

    assert pool.recovered_order("pay_anon") == "ORD-FREE"


def test_the_declined_evidence_states_the_real_population():
    """An audit line that states a false count is a defect, not a typo.

    The declined-recovery evidence used to report the count *after* the 7-day
    narrowing while describing the population *before* it, printing "0 unclaimed
    orders carry gross 720000" in a case where there were two.
    """
    orders = [
        make_order("ORD-NEAR", 720_000, order_date="2026-07-20"),
        make_order("ORD-FAR", 720_000, order_date="2026-05-01"),
    ]
    _, legs = two_anonymous_legs_and_two_orders()
    pool = build_pool(orders, legs)

    evidence = " ".join(r.evidence for r in pool.order_recoveries if not r.recovered)
    assert "0 unclaimed orders" not in evidence
    assert "2 unclaimed orders" in evidence


def test_every_leg_gets_exactly_one_recovery_record():
    """Declined or not, the attempt is the audit argument."""
    orders, legs = two_anonymous_legs_and_two_orders()
    pool = build_pool(orders, legs)

    assert [r.txn_id for r in pool.order_recoveries] == ["pay_L1", "pay_L2"]


# --- duplicate suppression: a survivor must never delete a settlement --------


def cross_settlement_twins() -> tuple[list, list]:
    """The same economic tuple, banked into two different settlements.

    `pay_AAA` sits in `setl_1` and `pay_ZZZ` in `setl_2`. Under an alphabetical
    survivor rule `pay_ZZZ` loses -- and `setl_2` has no other leg, so it ceases
    to exist.
    """
    orders = [make_order("ORD-DUP", 500_000, order_date="2026-07-21")]
    first = build_settlement("setl_1", [500_000], order_ids=["ORD-DUP"])
    second = build_settlement("setl_2", [500_000], order_ids=["ORD-DUP"])
    # Force the two payment legs onto the same economic tuple with names that
    # sort in opposite directions, so an id-ordered rule is visible.
    first[0] = make_txn(
        "pay_AAA", "payment", 500_000, order_id="ORD-DUP", settlement_id="setl_1"
    )
    second[0] = make_txn(
        "pay_ZZZ", "payment", 500_000, order_id="ORD-DUP", settlement_id="setl_2"
    )
    return orders, [*first, *second]


def test_cross_settlement_twins_do_not_annihilate_a_settlement():
    """Suppressing one twin used to delete a whole settlement.

    `setl_2`'s only payment leg is the suppressed row, so the settlement drops
    out of `settlement_ids` entirely and any bank line that would have closed
    against it can no longer be matched. Suppression is a within-settlement
    remedy; it must not reach across settlement boundaries.
    """
    orders, txns = cross_settlement_twins()
    pool = build_pool(orders, txns)

    assert "setl_1" in pool.settlement_ids
    assert "setl_2" in pool.settlement_ids
    assert pool.suppressed_txn_ids == set()


def test_cross_settlement_twins_survive_in_either_row_order():
    orders, txns = cross_settlement_twins()
    forward = build_pool(orders, txns)
    backward = build_pool(orders, list(reversed(txns)))

    assert forward.settlement_ids == backward.settlement_ids
    assert forward.suppressed_txn_ids == backward.suppressed_txn_ids == set()


def test_cross_settlement_twins_are_recorded_rather_than_passed_over():
    """Leaving both rows alone must be a stated decision, not a silence.

    The old behaviour was silent in the worst way: a settlement vanished and
    nothing in the exceptions, the matches or the audit trail said so.
    """
    orders, txns = cross_settlement_twins()
    pool = build_pool(orders, txns)

    (twin,) = pool.cross_settlement_twins
    assert twin.settlement_ids == ("setl_1", "setl_2")
    assert twin.txn_ids == ("pay_AAA", "pay_ZZZ")
    assert "neither row is suppressed" in twin.evidence


def test_both_cross_settlement_batches_still_reconstruct():
    """The arithmetic each settlement's bank line needs is intact."""
    orders, txns = cross_settlement_twins()
    pool = build_pool(orders, txns)

    assert pool.totals("setl_1").net == net_of([t for t in txns if t.settlement_id == "setl_1"])
    assert pool.totals("setl_2").net == net_of([t for t in txns if t.settlement_id == "setl_2"])


def test_an_in_settlement_twin_is_still_suppressed():
    """The real duplicate shape is untouched by the cross-settlement carve-out.

    Two rows, one settlement, same economic tuple: the second inflates the batch
    so nothing can close until it is discounted.
    """
    orders = [make_order("ORD-DUP", 500_000, order_date="2026-07-21")]
    legs = build_settlement("setl_D", [500_000], order_ids=["ORD-DUP"])
    twin = make_txn(
        "pay_twin", "payment", 500_000, order_id="ORD-DUP", settlement_id="setl_D"
    )
    pool = build_pool(orders, [*legs, twin])

    assert len(pool.suppressed_txn_ids) == 1
    assert len(pool.duplicates) == 1


def test_an_unsettled_twin_is_still_suppressed_in_favour_of_the_settled_row():
    """The `fixtures/tiny/` shape: the spurious copy carries no settlement.

    A row with no `settlement_id` belongs to no batch, so keeping the settled
    twin cannot delete anything -- this is the case where a survivor rule is
    safe, and it stays.
    """
    orders = [make_order("ORD-DUP", 500_000, order_date="2026-07-21")]
    legs = build_settlement("setl_U", [500_000], order_ids=["ORD-DUP"])
    orphan = make_txn(
        "pay_orphan", "payment", 500_000, order_id="ORD-DUP", settlement_id=None
    )
    pool = build_pool(orders, [*legs, orphan])

    assert pool.suppressed_txn_ids == {"pay_orphan"}
    assert "setl_U" in pool.settlement_ids


def test_the_unsettled_twin_loses_regardless_of_row_order():
    orders = [make_order("ORD-DUP", 500_000, order_date="2026-07-21")]
    legs = build_settlement("setl_U", [500_000], order_ids=["ORD-DUP"])
    orphan = make_txn(
        "pay_orphan", "payment", 500_000, order_id="ORD-DUP", settlement_id=None
    )
    forward = build_pool(orders, [*legs, orphan])
    backward = build_pool(orders, [orphan, *legs])

    assert forward.suppressed_txn_ids == backward.suppressed_txn_ids == {"pay_orphan"}


def test_no_false_duplicate_on_fee_and_tax_legs():
    """Settlement-level legs carry an empty `order_id` by design, so two
    settlements charging the same fee on the same day collide on the tuple
    without being duplicates at all."""
    first = build_settlement("setl_F1", [500_000])
    second = build_settlement("setl_F2", [500_000])
    pool = build_pool([], [*first, *second])

    assert pool.suppressed_txn_ids == set()


def test_a_bank_line_can_still_close_against_the_second_settlement():
    """The consequence the metrics feel: `setl_2` is matchable again."""
    orders, txns = cross_settlement_twins()
    second_net = net_of([t for t in txns if t.settlement_id == "setl_2"])
    pool = build_pool(
        orders,
        txns,
        [
            make_bank_line(
                "BL-9101",
                second_net,
                narration="NEFT CR RAZORPAY SOFTWARE PVT LTD SETL setl_2",
            )
        ],
    )

    assert pool.referenced_settlement(pool.bank_lines[0]) == "setl_2"
