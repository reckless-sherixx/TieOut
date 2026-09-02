"""`core/drift/compare.py` -- what changed between two runs, and whether it matters.

The rule this file exists to hold the implementation to: **detection is
deterministic and the model writes prose only.** Every threshold below is a
named constant in `core/drift/compare.py`, and
`test_the_narrative_is_never_an_input_to_material` proves the same two runs
produce a byte-identical set of moves with the narrative present and absent.

Deliberately **no `tests/drift/__init__.py`** -- same reason
`tests/api/conftest.py` gives: `tests/` lands on `sys.path`, so a package-shaped
`tests/drift` would shadow nothing today but is exactly how `tests/scorer/
__init__.py` shadowed the real `scorer/` package once already.

The module under test takes plain models and plain dicts and never touches the
store, which is what lets every test here run with no database at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.drift.compare import (
    COMPARED_METRICS,
    EXACT_METRICS,
    MAGNITUDE_MATERIAL_RATIO,
    NEVER_MATERIAL_METRICS,
    RATE_MATERIAL_DELTA,
    compare,
)
from core.models import DriftReport, Metrics, MetricMove, ReasonCodeMove, RunSummary

# The committed seed-42 500-record figures (METRICS.md §1), so a move in a test
# below is a move away from a number this repository actually reports.
BASELINE_METRICS = dict(
    auto_match_rate=0.8758,
    assisted_match_rate=0.0,
    exception_rate=0.1754,
    false_match_rate=0.0,
    precision=1.0,
    recall_on_resolvable=0.8758,
    trap_capture_rate=1.0,
    llm_rejection_rate=0.0,
    throughput_records_per_sec=30185.0,
    llm_cost_usd_per_100=0.0,
    llm_tokens_per_100=0,
    tier_counts={"T0": 126, "T1": 1, "T2": 9, "T3": 5, "LLM": 0},
    itc_substantiated_paise=3_933_031,
    itc_at_risk_paise=1_206_673,
    itc_variance_paise=-119_429,
)

BASELINE_CENSUS = {
    "AMBIGUOUS_MULTI_CANDIDATE": 10,
    "DUPLICATE_PSP_TXN": 10,
    "AMOUNT_MISMATCH": 10,
    "NO_SETTLEMENT_REF": 10,
}


def metrics(**overrides) -> Metrics:
    return Metrics(**{**BASELINE_METRICS, **overrides})


def summary(run_id: str, *, record_count: int = 500) -> RunSummary:
    return RunSummary(
        run_id=run_id,
        seed=42,
        record_count=record_count,
        state="completed",
        created_at=datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
        match_count=141,
        exception_count=40,
        metrics=None,
    )


def drift(
    *,
    before: Metrics | None = None,
    after: Metrics | None = None,
    before_census: dict[str, int] | None = None,
    after_census: dict[str, int] | None = None,
    narrative: str | None = None,
) -> DriftReport:
    return compare(
        summary("run-baseline"),
        summary("run-current"),
        baseline_metrics=before if before is not None else metrics(),
        current_metrics=after if after is not None else metrics(),
        baseline_census=before_census if before_census is not None else BASELINE_CENSUS,
        current_census=after_census if after_census is not None else BASELINE_CENSUS,
        narrative=narrative,
    )


def move(report: DriftReport, metric: str) -> MetricMove:
    return next(m for m in report.moves if m.metric == metric)


def code(report: DriftReport, reason_code: str) -> ReasonCodeMove:
    return next(m for m in report.reason_code_moves if m.reason_code == reason_code)


# --- the shape --------------------------------------------------------------


def test_the_report_names_both_runs_in_baseline_then_current_order():
    report = drift()
    assert report.baseline_run_id == "run-baseline"
    assert report.current_run_id == "run-current"


def test_every_numeric_metric_is_compared_and_none_is_silently_dropped():
    """A metric added to `Metrics` and not to the drift report is a metric that
    can change without anybody being told. `COMPARED_METRICS` is derived from
    `Metrics.model_fields`, so this holds for a field that does not exist yet."""
    numeric = {
        name
        for name, field in Metrics.model_fields.items()
        if field.annotation in (int, float)
    }
    assert set(COMPARED_METRICS) == numeric
    assert {m.metric for m in drift().moves} == numeric
    assert "tier_counts" not in numeric, "a dict is not a MetricMove"


def test_moves_come_back_in_the_declaration_order_of_the_metrics_contract():
    """Deterministic ordering, and the one order that carries meaning: the
    accuracy rates first, then throughput and cost, then the rupee figures."""
    assert [m.metric for m in drift().moves] == list(COMPARED_METRICS)


def test_delta_is_after_minus_before_on_every_move():
    report = drift(after=metrics(auto_match_rate=0.9100, itc_at_risk_paise=2_000_000))
    for m in report.moves:
        assert m.delta == pytest.approx(m.after - m.before)


# --- the rate rule: 0.01 absolute -------------------------------------------


def test_two_runs_of_the_same_data_report_no_material_move_at_all():
    report = drift()
    assert all(m.delta == 0.0 for m in report.moves)
    assert not [m for m in report.moves if m.material]
    assert report.reason_code_moves == []


def test_the_same_dataset_twice_is_quiet_even_though_the_clock_is_not():
    """The control case, in the shape it actually takes. Two runs of one dataset
    produce byte-identical accuracy and rupee figures and a *different*
    throughput, because throughput is measured. The report must still be
    silent -- a detector that reports a finding when nothing changed is
    reporting itself."""
    report = drift(after=metrics(throughput_records_per_sec=30185.0 / 4))
    assert [m for m in report.moves if m.material] == []
    assert report.reason_code_moves == []
    assert [m.metric for m in report.moves if m.delta != 0.0] == [
        "throughput_records_per_sec"
    ]


def test_a_rate_move_under_the_threshold_is_reported_but_not_material():
    report = drift(after=metrics(auto_match_rate=0.8758 + 0.005))
    assert move(report, "auto_match_rate").material is False
    assert move(report, "auto_match_rate").after == pytest.approx(0.8808)


def test_a_rate_move_at_the_threshold_is_material():
    """The boundary is inclusive. §7 says "exceeded"; a float delta essentially
    never lands exactly on the constant, so an exclusive boundary would be a
    distinction no real comparison draws -- and "0.01 is the threshold but a
    0.01 move is not material" is the reading a reviewer would call a bug."""
    report = drift(after=metrics(exception_rate=0.1754 + RATE_MATERIAL_DELTA))
    assert move(report, "exception_rate").material is True


def test_the_98_to_91_headline_move_is_material():
    report = drift(
        before=metrics(auto_match_rate=0.98), after=metrics(auto_match_rate=0.91)
    )
    assert move(report, "auto_match_rate").material is True
    assert move(report, "auto_match_rate").delta == pytest.approx(-0.07)


# --- the exact rule: metrics whose correct value is a known constant ---------


def test_four_wrong_matches_appearing_where_there_were_none_is_material():
    """0.0 -> 0.004 is four false matches in a thousand subjects and sits well
    under the 0.01 rate threshold. It is the single most important thing this
    system can say, so `false_match_rate` does not get the rate rule."""
    assert 0.004 < RATE_MATERIAL_DELTA
    report = drift(after=metrics(false_match_rate=0.004, precision=0.996))
    assert move(report, "false_match_rate").material is True


def test_precision_slipping_off_one_is_material():
    """`scorer/score.py` sets `precision = 1.0 - false_match_rate`, so this is
    the same event from the other side. Reported twice on purpose: a wrong match
    must be impossible to miss."""
    report = drift(after=metrics(false_match_rate=0.004, precision=0.996))
    assert move(report, "precision").material is True


def test_a_single_trap_escaping_is_material():
    report = drift(after=metrics(trap_capture_rate=0.996))
    assert move(report, "trap_capture_rate").material is True


def test_a_correctness_metric_returning_to_its_target_is_material_too():
    """Symmetric by design. "The wrong matches went away" is a finding, and a
    threshold that depended on the direction of the arrow would be the detector
    making a judgement instead of a measurement."""
    report = drift(
        before=metrics(false_match_rate=0.004, precision=0.996),
        after=metrics(false_match_rate=0.0, precision=1.0),
    )
    assert move(report, "false_match_rate").material is True
    assert move(report, "precision").material is True


def test_an_unchanged_correctness_metric_is_still_not_material():
    """The exact rule fires on any *change*, not on any *value*. A run that held
    `false_match_rate` at 0.0 must not report a material move."""
    assert move(drift(), "false_match_rate").material is False
    assert move(drift(), "trap_capture_rate").material is False


def test_every_compared_metric_falls_under_exactly_one_rule():
    """Four rules partition the metrics: no metric is governed by two of them,
    and none falls through to a default nobody chose. The rate set is derived by
    subtraction, so a metric added to `Metrics` tomorrow lands there -- an
    absolute threshold on a number nobody has classified is the conservative
    mistake; a ratio on a bounded rate is not."""
    from core.drift import compare as module

    buckets = [
        module._RATE_METRICS,
        module._MAGNITUDE_METRICS,
        EXACT_METRICS,
        NEVER_MATERIAL_METRICS,
    ]
    assert set().union(*buckets) == set(COMPARED_METRICS)
    assert sum(len(b) for b in buckets) == len(COMPARED_METRICS), "overlapping rules"


def test_the_exact_set_is_exactly_the_three_metrics_with_a_documented_target():
    assert EXACT_METRICS == frozenset(
        {"false_match_rate", "precision", "trap_capture_rate"}
    )


def test_a_rate_with_no_documented_target_keeps_the_rate_rule():
    """`recall_on_resolvable` moves with the data by design -- METRICS.md §1
    reports it at three different values -- so it is not a correctness metric."""
    report = drift(after=metrics(recall_on_resolvable=0.8758 + 0.004))
    assert move(report, "recall_on_resolvable").material is False


# --- the magnitude rule: relative, because 0.01 absolute is meaningless ------


def test_a_one_paise_move_on_a_rupee_figure_is_not_material():
    """0.01 absolute on integer paise fires on one paise out of ₹39,330. A
    threshold that fires on everything is noise."""
    report = drift(after=metrics(itc_substantiated_paise=3_933_032))
    assert move(report, "itc_substantiated_paise").material is False
    assert move(report, "itc_substantiated_paise").delta == pytest.approx(1.0)


def test_a_rupee_figure_moving_by_more_than_the_ratio_is_material():
    after = int(3_933_031 * (1 + MAGNITUDE_MATERIAL_RATIO)) + 1
    report = drift(after=metrics(itc_substantiated_paise=after))
    assert move(report, "itc_substantiated_paise").material is True


def test_a_signed_rupee_figure_crossing_zero_is_material():
    report = drift(after=metrics(itc_variance_paise=346_461))
    assert move(report, "itc_variance_paise").material is True


def test_a_magnitude_appearing_from_zero_is_material():
    """No ratio is defined against a zero baseline, so any appearance counts --
    the same rule the reason-code census uses, for the same reason."""
    report = drift(after=metrics(llm_tokens_per_100=1_200, llm_cost_usd_per_100=0.0004))
    assert move(report, "llm_tokens_per_100").material is True
    assert move(report, "llm_cost_usd_per_100").material is True


def test_a_magnitude_falling_to_zero_is_material():
    report = drift(
        before=metrics(llm_tokens_per_100=1_200), after=metrics(llm_tokens_per_100=0)
    )
    assert move(report, "llm_tokens_per_100").material is True


def test_a_zero_that_stayed_zero_is_not_material():
    assert move(drift(), "llm_tokens_per_100").material is False
    assert move(drift(), "assisted_match_rate").material is False


# --- throughput: the one figure that does not reproduce ---------------------


@pytest.mark.parametrize("after", [30185.0 * 1.02, 30185.0 * 3, 8658.0, 1.0, 0.0])
def test_a_throughput_move_of_any_size_is_reported_and_never_material(after):
    """METRICS.md §8: throughput is the only figure in this repository that will
    not reproduce on another machine, and it measures it as best/median/worst of
    25 runs after 5 warm-ups. One run against one run is not that measurement.

    Not a threshold set high -- no threshold at all, because 50 records is about
    1.4ms of wall clock and a scheduler hiccup there is a multiple rather than a
    percent. The control case proved it: the same dataset run twice through the
    API produced a material throughput move under test-suite load."""
    report = drift(after=metrics(throughput_records_per_sec=after))
    reported = move(report, "throughput_records_per_sec")
    assert reported.material is False
    assert reported.after == pytest.approx(after)
    assert reported.delta == pytest.approx(after - 30185.0)


def test_the_never_material_set_is_exactly_throughput():
    assert NEVER_MATERIAL_METRICS == frozenset({"throughput_records_per_sec"})


def test_a_never_material_metric_does_not_silence_the_run():
    """The one thing that would make the rule a hole: throughput going quiet
    while a real move in the same report goes quiet with it."""
    report = drift(
        after=metrics(
            throughput_records_per_sec=1.0,
            itc_at_risk_paise=int(1_206_673 * 1.5),
        )
    )
    assert move(report, "throughput_records_per_sec").material is False
    assert move(report, "itc_at_risk_paise").material is True


# --- reason codes: any appearance ------------------------------------------


def test_a_previously_absent_reason_code_appears():
    """The whole point of §7: a match rate that fell because a new deduction
    type appeared is the finding; the rate on its own is not."""
    report = drift(after_census={**BASELINE_CENSUS, "MISSING_ORDER_REF": 7})
    assert code(report, "MISSING_ORDER_REF").appeared is True
    assert code(report, "MISSING_ORDER_REF").before == 0
    assert code(report, "MISSING_ORDER_REF").after == 7


def test_a_reason_code_whose_count_did_not_change_is_not_reported():
    report = drift(after_census={**BASELINE_CENSUS, "MISSING_ORDER_REF": 7})
    assert [m.reason_code for m in report.reason_code_moves] == ["MISSING_ORDER_REF"]


def test_a_reason_code_that_grew_is_reported_but_did_not_appear():
    report = drift(after_census={**BASELINE_CENSUS, "AMOUNT_MISMATCH": 31})
    assert code(report, "AMOUNT_MISMATCH").appeared is False
    assert (code(report, "AMOUNT_MISMATCH").before, code(report, "AMOUNT_MISMATCH").after) == (10, 31)


def test_a_reason_code_that_disappeared_is_reported_and_did_not_appear():
    """A code that stopped firing is a change and is reported; `appeared` is the
    narrower fact of "absent before, present now" and stays false."""
    shrunk = {k: v for k, v in BASELINE_CENSUS.items() if k != "AMOUNT_MISMATCH"}
    report = drift(after_census=shrunk)
    assert code(report, "AMOUNT_MISMATCH").appeared is False
    assert (code(report, "AMOUNT_MISMATCH").before, code(report, "AMOUNT_MISMATCH").after) == (10, 0)


def test_reason_code_moves_are_ordered_by_code():
    report = drift(
        after_census={"MISSING_ORDER_REF": 3, "AMOUNT_MISMATCH": 99, "ORPHAN_PSP_TXN": 1}
    )
    codes = [m.reason_code for m in report.reason_code_moves]
    assert codes == sorted(codes)


def test_the_census_is_taken_as_given_and_never_reordered_into_a_verdict():
    """`compare` reads dicts. Two censuses with the same counts in a different
    key order are the same census."""
    shuffled = dict(reversed(list(BASELINE_CENSUS.items())))
    assert drift(after_census=shuffled).reason_code_moves == []


# --- the rule that makes this trustworthy -----------------------------------


def test_the_narrative_is_never_an_input_to_material():
    """The same two runs must produce an identical set of material moves with
    the narrative present and absent. This is the same division of labour the
    verifier enforces: the model may describe a fact it did not compute, and may
    not decide anything."""
    moved = metrics(
        auto_match_rate=0.91,
        false_match_rate=0.004,
        precision=0.996,
        itc_at_risk_paise=9_000_000,
    )
    census = {**BASELINE_CENSUS, "MISSING_ORDER_REF": 7}

    silent = drift(after=moved, after_census=census, narrative=None)
    spoken = drift(
        after=moved,
        after_census=census,
        narrative=(
            "Match rate fell seven points because a deduction type that had "
            "never appeared before turned up in 7 settlements. Nothing here is "
            "computed by the model."
        ),
    )

    assert silent.moves == spoken.moves
    assert silent.reason_code_moves == spoken.reason_code_moves
    assert silent.narrative is None
    assert spoken.narrative is not None
    assert silent.model_dump(exclude={"narrative"}) == spoken.model_dump(
        exclude={"narrative"}
    )


def test_a_narrative_claiming_the_opposite_changes_nothing():
    """The adversarial form of the rule: prose that contradicts the arithmetic
    is still only prose."""
    moved = metrics(auto_match_rate=0.91)
    lying = drift(after=moved, narrative="Nothing changed; every metric is flat.")
    assert move(lying, "auto_match_rate").material is True


def test_narrative_is_none_when_no_model_ran():
    assert drift().narrative is None
