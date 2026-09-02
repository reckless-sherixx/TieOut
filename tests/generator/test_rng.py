"""Reproducibility is a headline claim, so the RNG is tested, not assumed.

Task A.1. The three tests from the plan, plus two that pin the properties the
rest of the generator leans on: `chance` must be drawn from the same stream (so
it cannot be swapped for a module-level `random` call and go unnoticed), and
`choice` must refuse an unordered container -- iterating a `set` of strings is
not stable across processes, which would silently break byte-identity.
"""

import pytest

from core.generator.rng import SeededRng


def test_same_seed_gives_identical_sequence():
    a = [SeededRng(42).randint(0, 1000) for _ in range(1)]
    b = [SeededRng(42).randint(0, 1000) for _ in range(1)]
    assert a == b


def test_different_seed_diverges():
    assert SeededRng(1).randint(0, 10**9) != SeededRng(2).randint(0, 10**9)


def test_sequence_is_stable_across_calls():
    r = SeededRng(42)
    first = [r.randint(0, 100) for _ in range(10)]
    r2 = SeededRng(42)
    second = [r2.randint(0, 100) for _ in range(10)]
    assert first == second


def test_every_helper_draws_from_the_one_seeded_stream():
    """A helper that reached for module-level `random` would diverge here."""
    a = SeededRng(42)
    b = SeededRng(42)
    left = [a.randint(0, 9), a.choice("abcdef"), a.chance(50), a.sample(range(20), 3)]
    scratch = [b.randint(0, 9), b.choice("abcdef"), b.chance(50), b.sample(range(20), 3)]
    assert left == scratch

    deck_a, deck_b = list(range(12)), list(range(12))
    a.shuffle(deck_a)
    b.shuffle(deck_b)
    assert deck_a == deck_b


def test_choice_refuses_an_unordered_container():
    """`set` iteration order is not stable across processes for str keys."""
    with pytest.raises(TypeError, match="unordered"):
        SeededRng(42).choice({"a", "b", "c"})
