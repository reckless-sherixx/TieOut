"""The scalability work's acceptance test, as a test rather than as a promise.

Spec §8 accepts the O(n log n) matcher on one condition: **every metric at 50,
500 and 5,000 records is byte-identical before and after**. A performance change
that moves an accuracy number is a bug, not a trade-off.

That claim was captured from `main` before the hot path was touched
(`bench/baseline/`, committed in its own commit so the artefact predates the
change it judges) and this file is what keeps it true afterwards. Without it the
acceptance test is a thing that happened once, in a session nobody can re-run,
and the next person to index, cache or bucket the candidate search has nothing
to check themselves against.

What is compared is wider than the headline claim, because a rate is a quotient
and two compensating errors keep a quotient still:

* every field of `Metrics` -- the criterion itself;
* the subject-level walk of what matched at which tier and what excepted with
  which reason code;
* `scorer.explain()`, one line per subject the run got wrong;
* a SHA-256 over matches, exceptions and the **whole audit trail**, which pins
  every evidence string the tiers write. An optimisation that quietly reworded
  an ambiguity line would still be a change to the audit argument, and the
  audit argument is the product.

`throughput_records_per_sec` is the one field excluded from meaning anything
here, and it excludes itself: the capture runs untimed, so the scorer's own
`elapsed_seconds=None` rule reports 0.0 both before and after.

`fixtures/seed42-5000/` is not committed (~1.7 MB, see
`tests/test_committed_fixtures_are_current.py`), so the 5,000-record leg runs
only where it has been generated and skips where it has not. The 50 and 500
legs always run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.capture import capture

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = REPO_ROOT / "bench" / "baseline"
FIXTURES = REPO_ROOT / "fixtures"

SCALES = (50, 500, 5000)

#: Fields present in `Metrics` today but absent from the captured baselines,
#: each one a deliberate addition rather than a metric that moved.
#:
#: * ``total_traps`` (2026-09-02) -- not a new metric but the denominator
#:   ``trap_capture_rate`` already divided by, promoted onto the wire so the
#:   console can render "100.0% (10 of 10 traps)". At 50 records that
#:   denominator is 2, and `100.0%` of 2 renders identically to `100.0%` of 100.
FIELDS_ADDED_SINCE_BASELINE = frozenset({"total_traps"})


@pytest.mark.parametrize("records", SCALES)
def test_every_metric_is_byte_identical_to_the_pre_optimisation_capture(records):
    dataset = FIXTURES / f"seed42-{records}"
    if not dataset.is_dir():
        pytest.skip(f"{dataset} is not generated here; it is not a committed fixture")

    expected = json.loads((BASELINE / f"seed42-{records}.json").read_text("utf-8"))
    actual = capture(dataset)

    # Metrics first and on its own: it is the acceptance criterion, and a
    # failure here should say so rather than being buried in a dict diff of the
    # whole capture.
    #
    # Compared KEY BY KEY against the baseline's own keys, not as a whole-dict
    # equality, so that a field ADDED to `Metrics` after the capture does not
    # read as a metric that moved. Regenerating the baseline would have been the
    # easy fix and the wrong one: it would relabel every future change as
    # intentional too, and this guard's entire value is that it cannot be
    # satisfied by re-recording the thing it judges.
    for key, was in expected["metrics"].items():
        assert actual["metrics"][key] == was, (
            f"{key} moved: baseline {was!r}, now {actual['metrics'][key]!r}. "
            "The optimisation was accepted on the condition that no metric "
            "changes at any scale."
        )

    # And any key the baseline does not have must be one somebody deliberately
    # added. An unlisted new key fails, because "a field appeared" and "the
    # engine started reporting something new without anyone noticing" look
    # identical from here.
    appeared = set(actual["metrics"]) - set(expected["metrics"])
    assert appeared <= FIELDS_ADDED_SINCE_BASELINE, (
        f"{sorted(appeared - FIELDS_ADDED_SINCE_BASELINE)} appeared in Metrics "
        "since the baseline was captured and is not recorded as a deliberate "
        "addition. Add it to FIELDS_ADDED_SINCE_BASELINE with the reason, or "
        "find out why the engine grew a field nobody asked for."
    )
    assert actual["tier_walk"] == expected["tier_walk"]
    assert actual["exception_walk"] == expected["exception_walk"]
    assert actual["explain"] == expected["explain"]
    assert actual["run_sha256"] == expected["run_sha256"], (
        "matches, exceptions or the audit trail changed even though every "
        "metric held -- the numbers survived but the argument behind them did not"
    )
    # The remaining sections are still compared whole -- only `metrics` has a
    # schema that grows.
    assert {k: v for k, v in actual.items() if k != "metrics"} == {
        k: v for k, v in expected.items() if k != "metrics"
    }


def test_the_baseline_is_not_empty_at_any_scale():
    """A capture of nothing compares equal to a capture of nothing. Every
    baseline must contain matches, exceptions and audit entries, or the test
    above passes vacuously at that scale."""
    for records in SCALES:
        path = BASELINE / f"seed42-{records}.json"
        assert path.is_file(), f"{path} is missing"
        baseline = json.loads(path.read_text("utf-8"))
        assert baseline["match_count"] > 0
        assert baseline["exception_count"] > 0
        assert baseline["audit_entry_count"] > 0
        assert baseline["metrics"]["auto_match_rate"] > 0.0
