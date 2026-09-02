"""Deterministic seeded RNG (Task A.1).

Reproducibility is a headline claim of this project -- `--seed 42` must emit
byte-identical CSVs on every machine, every run -- so randomness enters the
generator through exactly one door and that door is seeded.

Two rules the rest of `core/generator/` depends on:

1. **There is no module-level RNG.** Every draw comes from a `SeededRng`
   instance that was constructed from an explicit seed and threaded through the
   call. Nothing here reads the clock, `os.urandom`, or `random`'s global state.
2. **No draw is ever made from an unordered container.** `set` iteration order
   for `str` keys depends on `PYTHONHASHSEED`, so `random.choice(list(a_set))`
   is reproducible within a process and not across processes. `choice` and
   `sample` therefore reject `set`/`frozenset` outright rather than accepting a
   sequence whose order is an accident.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, MutableSequence, Sequence
from typing import TypeVar

T = TypeVar("T")


def _ordered(seq: Iterable[T], *, method: str) -> Sequence[T]:
    if isinstance(seq, (set, frozenset)):
        raise TypeError(
            f"SeededRng.{method}() refuses an unordered container: iteration order "
            "of a set is not stable across processes, which would silently break "
            "the byte-identical-output guarantee. Pass a list or tuple."
        )
    if isinstance(seq, Sequence):
        return seq
    return list(seq)


class SeededRng:
    """A thin, deliberately small wrapper over `random.Random(seed)`.

    Small on purpose: every method added here is another way for two runs to
    diverge, so the surface is exactly what the generator needs.
    """

    __slots__ = ("_rng", "_seed")

    def __init__(self, seed: int) -> None:
        self._seed = int(seed)
        self._rng = random.Random(self._seed)

    @property
    def seed(self) -> int:
        return self._seed

    def randint(self, a: int, b: int) -> int:
        """Inclusive on both ends, as `random.Random.randint` is."""
        return self._rng.randint(a, b)

    def choice(self, seq: Iterable[T]) -> T:
        return self._rng.choice(_ordered(seq, method="choice"))

    def chance(self, pct: int) -> bool:
        """True with probability `pct`/100, drawn from the seeded stream."""
        return self._rng.randint(1, 100) <= pct

    def shuffle(self, seq: MutableSequence[T]) -> None:
        """Shuffle in place. Takes a MutableSequence so a set cannot be passed."""
        self._rng.shuffle(seq)

    def sample(self, seq: Iterable[T], k: int) -> list[T]:
        return self._rng.sample(_ordered(seq, method="sample"), k)
