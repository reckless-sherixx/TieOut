"""The settlement indexes the candidate search reads (spec §8).

`_ReconstructionTier._candidates` used to scan every unclaimed settlement for
every bank line, three times over for T1, T2 and T3. The index replaces that
scan, and an index is exactly the kind of change that can be fast and wrong at
the same time -- so the tests here are about **equivalence to the scan it
replaces**, not about the index being clever.

Two properties carry all the weight, and both were live bugs once:

* **Cardinality-blindness.** A net-keyed index must key on the net and nothing
  else. The moment it partitions by payment-leg count, the T1/T2 split becomes
  a tie-breaker: two settlements that close the same credit at different
  cardinalities each look unique inside their own partition, both subjects get
  matched, and every individual rule still reads as correct.
* **Every equal-net settlement comes back together.** An index that returns
  candidates in a deterministic order invites a first-match-wins shortcut. The
  ambiguity rule needs the whole set, because more than one candidate means
  match nothing -- and a lookup that returned one of two identical nets would
  make the second one invisible rather than ambiguous.

The last test in each group is a brute-force differential: the index and the
linear scan must answer identically over a pool built to have collisions,
near-misses at both edges of the tolerance, and duplicate nets.
"""

from __future__ import annotations

from datetime import date

import pytest

from core.canonicalize.txn_types import MDR_BPS
from core.matcher.pool import CandidatePool
from core.money import pct_of

from .conftest import build_settlement, net_of


def settlement_at(settlement_id: str, payments: list[int], offset: int = 0, **kw):
    """Legs whose reconstructed net is `offset` paise BELOW the natural net.

    The fee leg is bent, never the payment legs, so a settlement's cardinality
    is free to vary independently of its net -- which is the whole point of the
    collisions these tests build.
    """
    gross = sum(payments)
    fee = pct_of(gross, MDR_BPS)
    return build_settlement(settlement_id, payments, fee_override=fee + offset, **kw)


def pool_of(*groups, bank_lines=()) -> CandidatePool:
    legs = [leg for group in groups for leg in group]
    return CandidatePool(orders=[], psp_txns=list(legs), bank_lines=list(bank_lines))


def scan(pool: CandidatePool, credit: int, tolerance: int) -> list[str]:
    """The O(n) scan the index replaces, kept here as the oracle."""
    return sorted(
        sid
        for sid in pool.unclaimed_settlements()
        if abs(pool.totals(sid).net - credit) <= tolerance
    )


# --- the net index ------------------------------------------------------------


def test_every_settlement_at_the_same_net_comes_back_together():
    """Two identical nets are an ambiguity, not a lookup that happens to return
    one of them. If the index ever returns a single settlement here the second
    becomes invisible and the tier matches the first -- a false match produced
    by an index, with every tier rule still intact."""
    a = settlement_at("setl_A", [2_000_000])
    b = settlement_at("setl_B", [1_200_000, 800_000])
    pool = pool_of(a, b)
    assert net_of(a) == net_of(b), "the fixture must actually collide"

    assert pool.unclaimed_settlements_near_net(net_of(a), 0) == ["setl_A", "setl_B"]


def test_the_index_does_not_partition_by_payment_leg_count():
    """`setl_A` has one payment leg and `setl_B` has two. A net-keyed index
    that also keyed on cardinality would return one of them for a T1 lookup and
    the other for a T2 lookup, and both subjects would match."""
    a = settlement_at("setl_A", [2_000_000])
    b = settlement_at("setl_B", [1_200_000, 800_000])
    pool = pool_of(a, b)

    found = pool.unclaimed_settlements_near_net(net_of(a), 0)
    assert {pool.payment_legs(sid) for sid in found} == {1, 2}


def test_the_tolerance_window_is_inclusive_at_both_edges():
    """T3's tolerance is ±100 paise: 100 is a match and 101 is not, the same
    boundary `abs(delta) > tolerance` drew."""
    exact = settlement_at("setl_0", [3_000_000])
    credit = net_of(exact)
    pool = pool_of(
        exact,
        settlement_at("setl_low", [3_000_000], offset=100),
        settlement_at("setl_high", [3_000_000], offset=-100),
        settlement_at("setl_out_low", [3_000_000], offset=101),
        settlement_at("setl_out_high", [3_000_000], offset=-101),
    )

    assert pool.unclaimed_settlements_near_net(credit, 100) == [
        "setl_0",
        "setl_high",
        "setl_low",
    ]


def test_a_zero_tolerance_lookup_returns_only_the_exact_net():
    exact = settlement_at("setl_0", [3_000_000])
    pool = pool_of(exact, settlement_at("setl_1", [3_000_000], offset=1))

    assert pool.unclaimed_settlements_near_net(net_of(exact), 0) == ["setl_0"]


def test_a_claimed_settlement_leaves_the_index():
    """The index is built once per run; the claimed set is not. A settlement
    another bank line already closed must stop being a candidate."""
    a = settlement_at("setl_A", [2_000_000])
    b = settlement_at("setl_B", [1_200_000, 800_000])
    pool = pool_of(a, b)
    credit = net_of(a)

    pool.claim("BL-1", "setl_A")

    assert pool.unclaimed_settlements_near_net(credit, 0) == ["setl_B"]


def test_a_net_no_settlement_carries_returns_nothing():
    pool = pool_of(settlement_at("setl_A", [2_000_000]))
    assert pool.unclaimed_settlements_near_net(1, 100) == []


@pytest.mark.parametrize("tolerance", [0, 100])
def test_the_index_answers_exactly_what_the_scan_answered(tolerance):
    """The differential. A pool with collisions, both tolerance edges and a
    near-miss on each side; every credit in a wide sweep must produce the same
    settlement set from the index as from the linear scan, before and after a
    claim."""
    groups = [
        settlement_at("setl_A", [2_000_000]),
        settlement_at("setl_B", [1_200_000, 800_000]),
        settlement_at("setl_C", [2_000_000], offset=100),
        settlement_at("setl_D", [2_000_000], offset=-100),
        settlement_at("setl_E", [2_000_000], offset=101),
        settlement_at("setl_F", [2_000_000], offset=-101),
        settlement_at("setl_G", [999_999, 3, 5]),
    ]
    pool = pool_of(*groups)
    base = net_of(groups[0])

    for credit in range(base - 250, base + 250):
        assert pool.unclaimed_settlements_near_net(credit, tolerance) == scan(
            pool, credit, tolerance
        ), credit

    pool.claim("BL-1", "setl_A")
    pool.claim("BL-2", "setl_D")
    for credit in range(base - 250, base + 250):
        assert pool.unclaimed_settlements_near_net(credit, tolerance) == scan(
            pool, credit, tolerance
        ), credit


# --- the settled-date index ---------------------------------------------------


def test_the_date_index_answers_exactly_what_within_window_answered():
    pool = pool_of(
        settlement_at("setl_A", [2_000_000], settled_at="2026-07-24"),
        settlement_at("setl_B", [2_100_000], settled_at="2026-07-22"),
        settlement_at("setl_C", [2_200_000], settled_at="2026-07-26"),
        settlement_at("setl_D", [2_300_000], settled_at="2026-07-27"),
        settlement_at("setl_E", [2_400_000], settled_at="2026-07-21"),
    )

    for day in range(18, 31):
        when = date(2026, 7, day)
        expected = sorted(
            sid
            for sid in pool.unclaimed_settlements()
            if pool.within_window(sid, when, 2)
        )
        assert pool.unclaimed_settlements_in_window(when, 2) == expected, when


def test_the_date_index_drops_a_claimed_settlement():
    pool = pool_of(
        settlement_at("setl_A", [2_000_000], settled_at="2026-07-24"),
        settlement_at("setl_B", [2_100_000], settled_at="2026-07-24"),
    )
    pool.claim("BL-1", "setl_A")
    assert pool.unclaimed_settlements_in_window(date(2026, 7, 24), 2) == ["setl_B"]


# --- the unclaimed cache ------------------------------------------------------


def test_the_unclaimed_cache_is_invalidated_by_a_claim():
    pool = pool_of(
        settlement_at("setl_A", [2_000_000]), settlement_at("setl_B", [2_100_000])
    )
    assert pool.unclaimed_settlements() == ["setl_A", "setl_B"]
    pool.claim("BL-1", "setl_A")
    assert pool.unclaimed_settlements() == ["setl_B"]


def test_a_caller_cannot_corrupt_the_cache_through_the_list_it_is_given():
    """The method has always handed back a list a caller may do what it likes
    with. Caching the sort must not quietly turn that into a shared mutable."""
    pool = pool_of(
        settlement_at("setl_A", [2_000_000]), settlement_at("setl_B", [2_100_000])
    )
    handed_out = pool.unclaimed_settlements()
    handed_out.append("setl_NOT_REAL")
    handed_out.remove("setl_A")

    assert pool.unclaimed_settlements() == ["setl_A", "setl_B"]
