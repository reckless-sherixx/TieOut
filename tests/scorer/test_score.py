"""Scorer tests.

`scorer/` is the only module in the repository that opens the ground-truth
file. `tests/test_boundaries.py` proves the matcher cannot, which is what makes
these numbers a measurement rather than a claim.

`false_match_rate` and `trap_capture_rate` are the honesty metrics. A wrong
match is worse than no match in accounting, and `trap_capture_rate < 1.0` means
the system resolved something built to be unresolvable.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from core.ingest.reader import read_bank, read_orders, read_psp
from core.matcher.engine import run_match
from scorer.score import score

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "tiny"
TRUTH = FIX / "truth.json"


def _run():
    return run_match(
        read_orders(FIX / "orders.csv"),
        read_psp(FIX / "psp.csv"),
        read_bank(FIX / "bank.csv"),
    )


@pytest.fixture(scope="module")
def baseline():
    return score(_run(), TRUTH)


# --- the plan's four tests ----------------------------------------------------


def test_reports_every_metric(baseline):
    for field in (
        "auto_match_rate",
        "assisted_match_rate",
        "exception_rate",
        "false_match_rate",
        "precision",
        "recall_on_resolvable",
        "trap_capture_rate",
        "llm_rejection_rate",
        "throughput_records_per_sec",
        "llm_cost_usd_per_100",
        "llm_tokens_per_100",
        "tier_counts",
    ):
        assert hasattr(baseline, field)


def test_tier_counts_come_from_the_engines_own_tier_assignment(baseline):
    """`tier_counts` is the engine's claim about what each tier did, so it is
    read off `MatchGroup.tier` and nothing else.

    The scorer already partitions subjects into deterministic and assisted, but
    those are *subjects intersected with truth* -- a different quantity. Deriving
    the breakdown from them would produce a number that can disagree with the
    tier the engine actually recorded, and the disagreement would surface as a
    UI bar that contradicts the match rows underneath it."""
    result = _run()
    assert score(result, TRUTH).tier_counts == result.tier_breakdown


def test_tier_counts_reports_a_tier_that_scored_nothing_as_zero(baseline):
    """`fixtures/tiny/` contains no T1 match: its two single-payment-leg
    settlements are a rounding break (T3) and the ambiguity trap (an exception).
    Zero is the correct answer there, and it has to be *reported* as zero --
    an omitted key would let the UI silently drop the row."""
    assert set(baseline.tier_counts) == {"T0", "T1", "T2", "T3", "LLM"}
    assert baseline.tier_counts["T1"] == 0
    assert baseline.tier_counts["LLM"] == 0, "a deterministic run assists nothing"
    assert sum(baseline.tier_counts.values()) == 4


def test_trap_capture_rate_is_one_on_the_fixture(baseline):
    """The two ambiguous lines are unresolvable by construction. Resolving them
    means the system hallucinated."""
    assert baseline.trap_capture_rate == 1.0


def test_false_match_rate_is_zero_for_deterministic_tiers(baseline):
    assert baseline.false_match_rate == 0.0


def test_a_deliberately_wrong_match_is_counted():
    result = _run()
    result.matches[0].order_ids = ["ORD-NONEXISTENT"]
    assert score(result, TRUTH).false_match_rate > 0.0


# --- the rest of spec section 9 ----------------------------------------------


def test_the_four_resolvable_subjects_are_all_matched(baseline):
    assert baseline.auto_match_rate == 1.0
    assert baseline.recall_on_resolvable == 1.0
    assert baseline.precision == 1.0


def test_exception_rate_counts_unmatched_subjects_over_all_subjects(baseline):
    """Two of six bank lines are exceptions -- the trap, by design."""
    assert baseline.exception_rate == pytest.approx(2 / 6)


def test_assisted_match_rate_is_zero_without_the_llm_layer(baseline):
    assert baseline.assisted_match_rate == 0.0


def test_llm_fields_are_present_and_typed_for_a_no_llm_run(baseline):
    assert baseline.llm_rejection_rate == 0.0
    assert baseline.llm_cost_usd_per_100 == 0.0
    assert baseline.llm_tokens_per_100 == 0
    assert isinstance(baseline.llm_tokens_per_100, int)


def test_throughput_is_reported_when_the_caller_times_the_run():
    """No wall-clock lives inside `core/`, so the elapsed time is measured at
    the boundary and handed in."""
    metrics = score(_run(), TRUTH, elapsed_seconds=0.5)
    assert metrics.throughput_records_per_sec == pytest.approx(24.0)


def test_throughput_is_zero_when_the_run_was_not_timed(baseline):
    assert baseline.throughput_records_per_sec == 0.0


# --- what the honesty metrics actually catch ----------------------------------


def test_matching_a_trap_line_destroys_trap_capture_rate():
    """Any match on a trap line fails this identically, whether or not it
    happens to agree: the point is that the data does not determine it."""
    result = _run()
    guess = copy.deepcopy(result.matches[0])
    guess.match_id = "match-BL-0005"
    guess.bank_line_id = "BL-0005"
    result.matches.append(guess)
    result.exceptions = [e for e in result.exceptions if e.subject_id != "BL-0005"]

    assert score(result, TRUTH).trap_capture_rate == 0.5


def test_a_match_on_the_wrong_settlement_is_a_false_match():
    result = _run()
    result.matches[0].settlement_id = "setl_M2"
    metrics = score(result, TRUTH)
    assert metrics.false_match_rate == pytest.approx(1 / 4)
    assert metrics.precision == pytest.approx(3 / 4)


def test_dropping_the_recovered_order_is_scored_as_a_disagreement():
    """The trap that costs the most: a matcher that reads the linkage as
    "orders named by a leg" omits the order recovered from the leg with an
    empty `order_id`, and is marked wrong for otherwise being right."""
    result = _run()
    (match,) = [m for m in result.matches if m.settlement_id == "setl_C3"]
    match.order_ids = [o for o in match.order_ids if o != "ORD-004603"]
    assert score(result, TRUTH).false_match_rate > 0.0


def test_including_the_dangling_chargeback_reference_is_a_disagreement():
    result = _run()
    (match,) = [m for m in result.matches if m.settlement_id == "setl_B2"]
    match.order_ids = [*match.order_ids, "ORD-004018"]
    assert score(result, TRUTH).false_match_rate > 0.0


def test_a_match_missing_a_psp_leg_is_a_disagreement():
    result = _run()
    result.matches[0].psp_txn_ids = result.matches[0].psp_txn_ids[:-1]
    assert score(result, TRUTH).false_match_rate > 0.0


def test_recall_falls_when_a_resolvable_subject_is_left_unmatched():
    result = _run()
    dropped = result.matches.pop()
    assert dropped.bank_line_id
    metrics = score(result, TRUTH)
    assert metrics.recall_on_resolvable == pytest.approx(3 / 4)
    assert metrics.auto_match_rate == pytest.approx(3 / 4)


def test_an_empty_run_does_not_divide_by_zero():
    """An empty run against a non-empty truth reconciles NOTHING.

    This used to assert `exception_rate == 0.0`, which was the defect the
    self-referential denominator produced rather than a property worth keeping:
    with `total_subjects = len(matched | excepted)` an engine that emitted
    nothing had an empty denominator and scored a perfect 0.0. Truth records
    six subjects and none of them was reconciled, so the rate is 1.0.
    """
    empty = run_match([], [], [])
    metrics = score(empty, TRUTH)
    assert metrics.false_match_rate == 0.0
    assert metrics.exception_rate == 1.0, "six subjects, none reconciled"
    assert metrics.trap_capture_rate == 1.0, "declining everything captures the trap"


def test_an_empty_truth_does_not_divide_by_zero(tmp_path):
    """The actual empty-denominator case: nothing to grade against."""
    empty_truth = tmp_path / "truth.json"
    empty_truth.write_text(
        '{"seed": 0, "record_count": 0, "linkages": [], "injected_defects": [], '
        '"unresolvable_ids": []}',
        encoding="utf-8",
    )
    metrics = score(run_match([], [], []), empty_truth)

    assert metrics.exception_rate == 0.0
    assert metrics.auto_match_rate == 0.0
    assert metrics.recall_on_resolvable == 0.0
    assert metrics.trap_capture_rate == 1.0, "no trap is not a blown trap"


# --- the denominators themselves ----------------------------------------------
#
# Two metrics used to be gradeable by the thing they were grading. A rate whose
# denominator comes out of the result under test is not a measurement, and both
# of these flattered a broken engine rather than exposing it.


def test_a_match_on_a_trap_line_cannot_push_auto_match_rate_above_one():
    """`Metrics` documents "Rates are 0.0-1.0" and strict mode does not check it.

    The numerator counted every T0-T3 match; the denominator counted resolvable
    subjects only. Two matches on the two trap lines therefore reported
    `auto_match_rate = 1.5` -- a number Lane D serialises and Lane E renders.
    """
    result = _run()
    for trap in ("BL-0005", "BL-0006"):
        guess = copy.deepcopy(result.matches[0])
        guess.match_id = f"match-{trap}"
        guess.bank_line_id = trap
        result.matches.append(guess)
    result.exceptions = [
        e for e in result.exceptions if e.subject_id not in {"BL-0005", "BL-0006"}
    ]

    metrics = score(result, TRUTH)
    assert metrics.auto_match_rate <= 1.0
    assert metrics.auto_match_rate == 1.0, "the four resolvable subjects still match"
    assert metrics.trap_capture_rate == 0.0, "and the traps are still reported blown"


def test_every_rate_stays_within_zero_and_one_on_a_hostile_result():
    """The range in the `Metrics` docstring, asserted rather than assumed."""
    result = _run()
    for trap in ("BL-0005", "BL-0006"):
        guess = copy.deepcopy(result.matches[0])
        guess.match_id = f"match-{trap}"
        guess.bank_line_id = trap
        result.matches.append(guess)
    result.exceptions = []

    metrics = score(result, TRUTH)
    for field in (
        "auto_match_rate",
        "assisted_match_rate",
        "exception_rate",
        "false_match_rate",
        "precision",
        "recall_on_resolvable",
        "trap_capture_rate",
        "llm_rejection_rate",
    ):
        value = getattr(metrics, field)
        assert 0.0 <= value <= 1.0, f"{field} = {value}"


def test_dropping_a_matched_subject_makes_the_exception_rate_worse():
    """A subject the engine loses must cost it.

    `total_subjects` used to be `len(matched | bank_exceptions)` -- derived from
    the result being graded -- so a subject dropped from BOTH sets vanished from
    the denominator and the rate improved. An engine could raise its score by
    losing work.
    """
    result = _run()
    dropped = result.matches.pop()
    assert dropped.bank_line_id

    assert score(result, TRUTH).exception_rate > score(_run(), TRUTH).exception_rate


def test_dropping_an_excepted_subject_does_not_improve_the_exception_rate():
    """The reviewer's demonstration: silently skipping `BL-0006` moved the rate
    from 0.333 to 0.2 while `trap_capture_rate` stayed 1.0 and `explain()`
    returned `[]`. The grader flattered a broken engine."""
    baseline_rate = score(_run(), TRUTH).exception_rate

    result = _run()
    result.exceptions = [e for e in result.exceptions if e.subject_id != "BL-0006"]

    assert score(result, TRUTH).exception_rate >= baseline_rate


def test_the_exception_rate_denominator_is_truth_not_the_result():
    """Both halves at once: drop a subject from matches AND exceptions."""
    result = _run()
    result.matches = [m for m in result.matches if m.bank_line_id != "BL-0001"]
    result.exceptions = [e for e in result.exceptions if e.subject_id != "BL-0005"]

    # 6 linkages; BL-0001 and both traps are now unaccounted for, plus BL-0006.
    assert score(result, TRUTH).exception_rate == pytest.approx(3 / 6)


# --- split_settlement: two linkages, one settlement ---------------------------
#
# NOTE ON THIS DIRECTORY: `tests/scorer/` must NOT have an `__init__.py`.
# `tests/matcher/` and `tests/generator/` need one -- it gives them a package
# name and stops `test_cli.py` colliding on basename with the frozen
# `tests/test_cli.py`. Adding one here does the opposite: the directory becomes
# importable as the top-level package `scorer`, which shadows the real `scorer/`
# package and every `from scorer.score import score` in this file fails with
# `No module named 'scorer.score'`, aborting collection for the whole suite.
# The protection here is keeping test basenames unique instead, which they are.


def test_two_linkages_sharing_one_settlement_both_survive_load(tmp_path):
    """`split_settlement` pays one settlement across two bank lines.

    CSV_SCHEMAS 5 is "one entry per bank line", so truth carries TWO linkage
    entries with the same `settlement_id`. A scorer keying linkages by
    settlement collapses them and silently loses half its subjects -- and the
    500-record dataset contains five of these, so it would drop ten.
    """
    from scorer.metrics import load

    truth_file = tmp_path / "truth.json"
    truth_file.write_text(
        """{"seed": 1, "record_count": 2, "unresolvable_ids": [],
            "injected_defects": [],
            "linkages": [
              {"bank_line_id": "BL-0001", "settlement_id": "setl_S",
               "psp_txn_ids": ["pay_1"], "order_ids": ["ORD-1"]},
              {"bank_line_id": "BL-0002", "settlement_id": "setl_S",
               "psp_txn_ids": ["pay_1"], "order_ids": ["ORD-1"]}
            ]}""",
        encoding="utf-8",
    )
    truth = load(truth_file)

    assert len(truth.linkages) == 2
    assert set(truth.linkages) == {"BL-0001", "BL-0002"}
    assert truth.resolvable_ids == frozenset({"BL-0001", "BL-0002"})
    assert {link.settlement_id for link in truth.linkages.values()} == {"setl_S"}


def test_a_split_settlement_pair_counts_as_two_subjects_in_the_rates(tmp_path):
    """Both halves are subjects. Matching one and losing the other is 50%, not
    100% -- which is what a settlement-keyed scorer would have reported."""
    from scorer.metrics import load

    truth_file = tmp_path / "truth.json"
    truth_file.write_text(
        """{"seed": 1, "record_count": 2, "unresolvable_ids": [],
            "injected_defects": [],
            "linkages": [
              {"bank_line_id": "BL-0001", "settlement_id": "setl_S",
               "psp_txn_ids": ["pay_1"], "order_ids": ["ORD-1"]},
              {"bank_line_id": "BL-0002", "settlement_id": "setl_S",
               "psp_txn_ids": ["pay_1"], "order_ids": ["ORD-1"]}
            ]}""",
        encoding="utf-8",
    )
    empty = run_match([], [], [])
    metrics = score(empty, truth_file)

    assert len(load(truth_file).linkages) == 2
    assert metrics.exception_rate == 1.0, "two subjects, neither reconciled"
    assert metrics.auto_match_rate == 0.0
