"""The committed fixtures must be what the generator emits *today*.

This file exists because documenting an invariant is not the same as testing
one. `METRICS.md` §2.1 has always said that regenerating `seed42-500` over the
committed copy leaves every file identical, and that **if it ever differs, every
number in that document is void**. Nothing enforced it. The generator grew from
ten defect classes to thirteen and started emitting `psp_gst_invoice.csv`, the
committed fixtures were never refreshed, and the load-bearing claim of the whole
metrics document quietly became false. A reviewer following the README's
quickstart would have found out before we did.

**The gap this closes is not determinism.** `tests/generator/test_emit.py`
already proves that two runs of the *same* generator produce byte-identical
output, and it still holds -- that is a property of the generator. The property
nobody was checking is that the generator and the committed artefact are still
the same *version* of the dataset. Those are different claims, and only the
second one goes stale on its own, silently, while every test stays green.

So the assertion here is deliberately the strongest available one: byte
equality, over the full set of emitted files, for every committed dataset. Not
a row count, not a checksum of the interesting columns, not "the defect classes
are all present" -- because the claim in `METRICS.md` is byte equality, and a
weaker test would let a fixture drift in exactly the way that voids the numbers
while reporting that all is well.

The regeneration goes through `build_dataset` + `emit_dataset` rather than a
subprocess, which is precisely what `recon generate` does with no `--defect-mix`
(`core/generator/cli.py`). Same code path, no process spawn, and no dependence
on `uv` being resolvable from inside the test runner.

**`fixtures/tiny/` is not in `COMMITTED_DATASETS` and must never be added.** It
is hand-written, its `truth.json` records `seed: 0` to say so, and
`tests/test_fixture_integrity.py` asserts its arithmetic by hand. Regenerating
it would destroy the one dataset in the repository whose defects were placed by
a person and reasoned about line by line. The last test in this file pins that
exclusion, so the guard cannot be undone by someone tidying up the list.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.generator.emit import emit_dataset
from core.generator.pipeline import build_dataset

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "fixtures"

#: Every dataset directory that is committed to the repository, and the exact
#: `recon generate` arguments that must reproduce it. `fixtures/seed42-5000` is
#: deliberately absent: it is not committed (it is ~1.7 MB), so there is no
#: stored artefact for it to drift away from -- METRICS.md §2.1 regenerates it
#: on demand instead.
COMMITTED_DATASETS: dict[str, tuple[int, int]] = {
    "seed42-50": (42, 50),
    "seed42-500": (42, 500),
}

#: The hand-written fixture, frozen. Named here only so the exclusion is
#: asserted rather than assumed.
HAND_WRITTEN = "tiny"


def _regenerate(seed: int, count: int, out_dir: Path) -> Path:
    """`recon generate --seed <seed> --count <count> --out <out_dir>`, in process."""
    batches, injections = build_dataset(seed, count, None)
    emit_dataset(batches, injections, out_dir=out_dir, seed=seed)
    return out_dir


def _fix_command(name: str, seed: int, count: int) -> str:
    return (
        f"    python -m uv run recon generate "
        f"--seed {seed} --count {count} --out fixtures/{name}"
    )


def _stale_fixture_message(name: str, seed: int, count: int, detail: str) -> str:
    """The whole point of this file: a failure that explains itself.

    Whoever trips this will not have the context that produced it, so the
    message carries the diagnosis, the fix, and the consequence of ignoring it.
    """
    return (
        f"\n\nfixtures/{name} is STALE -- it is not what the generator emits today."
        f"\n{detail}"
        "\n\nThis is almost certainly NOT a generator bug. The generator is "
        "byte-for-byte deterministic for a given version of itself "
        "(tests/generator/test_emit.py proves that separately). What has "
        "happened is that the generator changed and the committed fixture was "
        "not regenerated alongside it."
        "\n\nFix, in order:"
        f"\n{_fix_command(name, seed, count)}"
        f"\n    git add fixtures/{name}"
        "\n\nThen RE-DERIVE the documented numbers, because they are measured "
        "over this data and are now wrong: METRICS.md §1 (headline table) "
        "and §2, README.md's results table, and any test that asserts a "
        "match count or tier breakdown for this dataset. METRICS.md §2.1 "
        "states that if a regeneration ever differs from the committed "
        "fixture, every number in that document is void -- this test is what "
        "makes that promise true instead of merely written down."
    )


@pytest.fixture(scope="module")
def regenerated(tmp_path_factory) -> dict[str, Path]:
    """One fresh generation per committed dataset, shared by the tests below."""
    return {
        name: _regenerate(seed, count, tmp_path_factory.mktemp(name))
        for name, (seed, count) in COMMITTED_DATASETS.items()
    }


@pytest.mark.parametrize("name", sorted(COMMITTED_DATASETS))
def test_committed_fixture_holds_exactly_the_files_the_generator_emits(name, regenerated):
    """A file appearing or disappearing is the failure mode that actually bit us.

    `psp_gst_invoice.csv` was added to the generator's output and never landed in
    the committed directories. A byte comparison that only walked the files it
    found on disk would have compared four files, found them equal once they
    were refreshed, and still missed a fifth that was simply absent.
    """
    seed, count = COMMITTED_DATASETS[name]
    committed = FIXTURES / name

    expected = {path.name for path in regenerated[name].iterdir() if path.is_file()}
    actual = {path.name for path in committed.iterdir() if path.is_file()}

    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    detail = []
    if missing:
        detail.append(f"  missing (the generator emits these, the fixture has not): {missing}")
    if unexpected:
        detail.append(f"  unexpected (not emitted by the generator any more): {unexpected}")

    assert not missing and not unexpected, _stale_fixture_message(
        name, seed, count, "\n".join(detail)
    )


@pytest.mark.parametrize("name", sorted(COMMITTED_DATASETS))
def test_committed_fixture_is_byte_identical_to_a_fresh_generation(name, regenerated):
    """The claim `METRICS.md` §2.1 makes, asserted rather than described.

    Byte equality, not a semantic comparison: `.gitattributes` pins these files
    to `eol=lf`, so a CRLF checkout is itself a finding. Reading bytes rather
    than text is what lets this test say so.
    """
    seed, count = COMMITTED_DATASETS[name]
    committed = FIXTURES / name
    fresh = regenerated[name]

    differing = []
    for path in sorted(fresh.iterdir()):
        if not path.is_file():
            continue
        target = committed / path.name
        if not target.exists():
            continue  # the file-set test above owns this failure
        if target.read_bytes() != path.read_bytes():
            differing.append(path.name)

    assert not differing, _stale_fixture_message(
        name,
        seed,
        count,
        f"  differs on: {differing}",
    )


def test_the_hand_written_tiny_fixture_is_never_regenerated():
    """`fixtures/tiny/` is frozen, and this is the guard on that.

    Its defects were placed by hand and are asserted one by one in
    `tests/test_fixture_integrity.py`; no seed produces it. Adding it to
    `COMMITTED_DATASETS` would make this suite overwrite the only dataset in the
    repository whose every row was reasoned about, and the reviewer's worked
    example in `README.md` along with it.
    """
    assert HAND_WRITTEN not in COMMITTED_DATASETS, (
        "fixtures/tiny/ is hand-written and must not be regenerated -- "
        "remove it from COMMITTED_DATASETS"
    )

    truth = json.loads((FIXTURES / HAND_WRITTEN / "truth.json").read_text(encoding="utf-8"))
    assert truth["seed"] == 0, (
        "fixtures/tiny/truth.json records seed 0 to mark itself hand-written; "
        "a non-zero seed here means it was generated over, and the worked "
        "example in README.md no longer describes the committed rows"
    )
