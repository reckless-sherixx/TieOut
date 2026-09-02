"""The tier breakdown reports the confidence the ENGINE stamped, per run.

A constant in the console that happens to agree with the engine today is not
the same claim as a field derived from this run's matches, and the difference
is exactly how `web/lib/tiers.ts` came to render "verified" on a rung the
engine stamps 0.70. Measured on 2026-09-02:

    tier   engine stamps   console showed
    T0     1.0             "1.00"
    T2     0.99            "0.99"
    T3     0.8             "0.80"
    LLM    0.7             "verified"    <- a reassurance, not a measurement

Nothing tested the two against each other, so nothing caught it. This file is
that test.
"""

from core.models import MatchGroup


def _match(tier: str, confidence: float, n: int) -> MatchGroup:
    """A MatchGroup whose only interesting fields are `tier` and `confidence`.

    Every money field closes (gross - fees - tax - refunds - holds == net) so
    the model's own validators cannot reject the fixture for a reason that has
    nothing to do with what is being asserted.
    """
    return MatchGroup(
        match_id=f"m-{tier}-{n}",
        bank_line_id=f"BL-{n:04d}",
        settlement_id=f"setl_{n:05d}",
        psp_txn_ids=[f"pay_{n}"],
        order_ids=[f"ORD-{n:04d}"],
        gross=10_000,
        fees=236,
        tax=42,
        refunds=0,
        holds=0,
        net=9_722,
        tier=tier,
        confidence=confidence,
        evidence=["fixture"],
    )


def test_a_tier_reports_the_confidence_the_engine_stamped():
    from api.routes import tier_confidence_map

    matches = [_match("T0", 1.0, 1), _match("T0", 1.0, 2), _match("LLM", 0.7, 3)]
    got = tier_confidence_map(matches)

    assert got["T0"] == {"confidence_observed": 1.0, "confidence_conflict": False}
    assert got["LLM"] == {"confidence_observed": 0.7, "confidence_conflict": False}


def test_a_tier_with_no_matches_reports_null_not_a_default():
    """"T1 did not fire" and "T1 is 0.95" are different sentences.

    The old console rendered the second when the truth was the first, because
    its number came from a table rather than from the run.
    """
    from api.routes import tier_confidence_map

    got = tier_confidence_map([_match("T0", 1.0, 1)])
    assert got["T1"] == {"confidence_observed": None, "confidence_conflict": False}


def test_two_confidences_on_one_tier_refuse_to_collapse():
    """A set of two values has no single representative.

    Reporting either one would be a true statement about some matches and a
    false statement about the rest, with nothing on the wire saying which.
    """
    from api.routes import tier_confidence_map

    got = tier_confidence_map([_match("T3", 0.8, 1), _match("T3", 0.75, 2)])
    assert got["T3"] == {"confidence_observed": None, "confidence_conflict": True}


def test_a_conflict_is_distinguishable_from_an_absence():
    """Both render as "no figure", and they are not the same event.

    One means the rung did not fire; the other means it disagrees with itself
    and someone should look. A single null for both would hide the second.
    """
    from api.routes import tier_confidence_map

    got = tier_confidence_map([_match("T3", 0.8, 1), _match("T3", 0.75, 2)])
    assert got["T3"]["confidence_conflict"] is True
    assert got["T1"]["confidence_conflict"] is False


def test_every_tier_key_is_always_present():
    """Same rule as `tier_counts`: an absent key is a silence, not a zero."""
    from api.routes import tier_confidence_map

    got = tier_confidence_map([])
    assert set(got) == {"T0", "T1", "T2", "T3", "LLM"}
    assert all(v["confidence_observed"] is None for v in got.values())
