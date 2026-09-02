"""Two runs, compared. Detection is deterministic; the model writes prose only.

A finance controller's question is rarely "what is the match rate" -- it is
"what changed since last time, and why". A match rate that falls from 98% to
91% overnight because a new deduction type appeared is the finding; the 91% on
its own is not.

**The division of labour, and the reason this module is worth trusting.**
`material` is computed here, from two numbers and a named constant, and from
nothing else. `narrative` is an argument: prose written over facts this module
already computed, `None` when no model ran, and never read. It is the same
division the verifier enforces on the analyst layer -- the model may describe a
fact it did not compute, and may not decide anything. `tests/drift/
test_compare.py::test_the_narrative_is_never_an_input_to_material` holds the
implementation to it by running the same two runs with the narrative present and
absent and comparing the reports field by field.

**No store, no clock, no model.** Everything arrives as a plain model or a plain
dict, so every test in `tests/drift/` runs with no database at all, and
`tests/drift/test_determinism_boundary.py` proves structurally that this module
cannot reach one. The reason-code counts come from
`Repo.reason_code_census(run_id)`, grouped in SQL by the caller.

---

## What "material" means

A threshold that fires on everything is noise; one that fires on nothing is
decoration. §7's starting point is "0.01 absolute for rates", and `Metrics`
holds three different kinds of number, only one of which that constant suits.

**1. Rates -- `RATE_MATERIAL_DELTA`, 0.01 absolute.** Bounded 0.0-1.0 and
comparable across datasets, so an absolute threshold means the same thing
everywhere. At 500 records 0.01 is about 1.6 subjects; at 5,000 it is about 16.

**2. Metrics whose correct value is a known constant -- `EXACT_METRICS`, any
change at all.** `false_match_rate` is 0.0, `precision` is 1.0 and
`trap_capture_rate` is 1.0 at every scale in METRICS.md §1, and the first two of
those are bolded there as the numbers that make the headline honest. A move from
0.0 to 0.004 in `false_match_rate` is **four wrong matches appearing where there
were none** -- arguably the single most important thing this system could tell
anyone -- and it sits under 0.01. So these three do not get the rate rule.

The line is drawn at "has a documented target", not at "feels important":
`recall_on_resolvable` and `auto_match_rate` move with the data by design and
METRICS.md reports them at three different values, so a rule that fired on any
change to them would fire on every comparison of two different datasets, which
is the noise case. `precision` is included even though `scorer/score.py` defines
it as `1.0 - false_match_rate` and it therefore reports the same event twice:
that is deliberate, because a wrong match should be impossible to miss.

The rule is symmetric. A move back to the target is material too -- "the wrong
matches went away" is a finding, and a threshold that depended on the direction
of the arrow would be the detector making a judgement rather than a measurement,
which is precisely the thing the model is not allowed to do either.

**3. Unbounded magnitudes -- `MAGNITUDE_MATERIAL_RATIO`, 0.05 relative.**
`llm_cost_usd_per_100`, `llm_tokens_per_100` and the three `itc_*_paise` figures
are not rates, and 0.01 absolute is meaningless on all five: it fires on one
paise out of ₹39,330 and on a single token. A ratio of the baseline is
scale-free, so the same rule works at 50 records and at 5,000. These five are
exact sums over the run's own data and carry no measurement noise, so the
threshold exists only to keep a rounding-scale move from being called a finding.
5% of the committed 500-record `itc_substantiated_paise` is ₹1,966.

A zero baseline has no ratio, so **any appearance is material** -- the same rule
§7 gives the reason codes, for the same reason. `llm_tokens_per_100` going 0 ->
1,200 is the analyst layer running for the first time and is a finding.

**4. `throughput_records_per_sec` is reported and NEVER material.** This is the
one rule here that was not chosen up front: it was forced by the control case.
Running the same 50-record dataset twice through the API and comparing the two
produced a material throughput move under test-suite load, and a detector that
reports a finding when nothing changed is reporting itself.

There is no ratio that fixes that. 50 records at ~36,000 rec/s is about 1.4
milliseconds of wall clock, so a scheduler hiccup is a multiple, not a percent;
any constant loose enough to survive it is loose enough to miss anything real.
METRICS.md §8 already says this figure is the only one in this repository that
will not reproduce on another machine, and it measures it as best/median/worst
of 25 runs after 5 warm-ups. **A single run against a single run is not that
measurement**, and flagging it as a finding would be asserting a benchmark
result the method cannot support.

Two things make the omission cheap rather than a hole. The move is still
reported, with its before, after and delta, so a human reading the report sees a
collapse and can go and benchmark it properly. And the comparison this module is
allowed to make is always at equal `record_count` -- the endpoint refuses the
rest with a 409 -- so the O(n^2) cliff §8 of the spec exists to remove, 30,185
rec/s at 500 records against 8,658 at 5,000, can never appear in a drift report
in the first place.

The alternative was accepting a control case that fails intermittently in CI,
which is the "fires on everything" failure applied to the one comparison that
must be quiet.

**What is deliberately not compared: `tier_counts`.** It is a dict, not a
metric, and `MetricMove` carries a float. More to the point, no threshold
expressed as a rate or a ratio is honest about it: on the committed 500-record
run `T1` is 1, and 1 -> 2 is one extra match and a 100% move. The categorical
"why" §7 asks for is carried by the reason-code census, which is counted over
hundreds of exceptions rather than over five buckets, one of which is routinely
a single digit. Adding tier counts would mean inventing a fourth rule with no
defensible constant behind it.
"""

from __future__ import annotations

from collections.abc import Mapping

from core.models import DriftReport, Metrics, MetricMove, ReasonCodeMove, RunSummary

__all__ = [
    "COMPARED_METRICS",
    "EXACT_METRICS",
    "MAGNITUDE_MATERIAL_RATIO",
    "NEVER_MATERIAL_METRICS",
    "RATE_MATERIAL_DELTA",
    "compare",
    "is_material",
]


#: Rates are bounded 0.0-1.0 and comparable across datasets, so §7's absolute
#: constant means the same thing at every scale. The boundary is **inclusive**:
#: §7 says "exceeded", but a float delta essentially never lands exactly on the
#: constant, so an exclusive boundary would be a distinction no real comparison
#: draws -- while "0.01 is the threshold but a 0.01 move is not material" is a
#: reading a reviewer would call a bug.
RATE_MATERIAL_DELTA = 0.01

#: The three metrics whose correct value is a known constant -- 0.0, 1.0 and 1.0
#: respectively at every scale in METRICS.md §1. For these, ANY change is
#: material: `false_match_rate` 0.0 -> 0.004 is four wrong matches in a thousand
#: subjects and would hide under `RATE_MATERIAL_DELTA`. See the module docstring
#: for why the set stops at three.
EXACT_METRICS = frozenset({"false_match_rate", "precision", "trap_capture_rate"})

#: Relative threshold for the unbounded magnitudes that carry no measurement
#: noise: the three `itc_*_paise` figures, `llm_cost_usd_per_100` and
#: `llm_tokens_per_100`. 5% of the committed 500-record `itc_substantiated_paise`
#: is ₹1,966. An absolute 0.01 would fire on one paise.
MAGNITUDE_MATERIAL_RATIO = 0.05

#: Reported with its before, after and delta, and never flagged material.
#:
#: `throughput_records_per_sec` is wall-clock on shared hardware. METRICS.md §8
#: names it the only figure in this repository that will not reproduce on
#: another machine, and measures it as best/median/worst of 25 runs after 5
#: warm-ups; one run against one run is not that measurement, and calling it a
#: finding would assert a benchmark result the method cannot support.
#:
#: This rule was forced by the control case rather than chosen up front: the
#: same 50-record dataset run twice through the API produced a material
#: throughput move under test-suite load. No constant fixes that -- 50 records
#: is about 1.4ms of wall clock, so a scheduler hiccup is a multiple rather than
#: a percent, and any threshold loose enough to survive it is loose enough to
#: miss anything real. The module docstring has the full argument, including why
#: the omission costs nothing: the endpoint refuses to compare runs of different
#: `record_count`, so the O(n^2) cliff cannot reach a drift report anyway.
NEVER_MATERIAL_METRICS = frozenset({"throughput_records_per_sec"})

#: Every numeric field of `Metrics`, in that contract's declaration order --
#: accuracy rates first, then throughput and cost, then the rupee figures.
#:
#: Derived from `Metrics.model_fields` rather than listed by hand, so a metric
#: added to the contract is compared without anyone remembering to add it here.
#: A metric that could change without appearing in a drift report is a metric
#: that can change without anybody being told. `tier_counts` is a `dict` and is
#: excluded by the type test, deliberately -- see the module docstring.
COMPARED_METRICS: tuple[str, ...] = tuple(
    name
    for name, field in Metrics.model_fields.items()
    if field.annotation in (int, float)
)


#: Every compared metric that is not bounded 0.0-1.0, and is therefore held to a
#: relative ratio rather than to an absolute delta.
_MAGNITUDE_METRICS = frozenset(
    {
        "llm_cost_usd_per_100",
        "llm_tokens_per_100",
        "itc_substantiated_paise",
        "itc_at_risk_paise",
        "itc_variance_paise",
    }
)

#: Everything else, held to `RATE_MATERIAL_DELTA`. Derived by subtraction so a
#: rate added to `Metrics` is treated as a rate by default -- the conservative
#: direction, since the other default would silently apply a ratio to a bounded
#: number.
_RATE_METRICS = (
    frozenset(COMPARED_METRICS)
    - _MAGNITUDE_METRICS
    - EXACT_METRICS
    - NEVER_MATERIAL_METRICS
)


def is_material(metric: str, before: float, after: float) -> bool:
    """Whether this metric moving from `before` to `after` is a finding.

    The whole of the detection rule, in one pure function of two numbers and a
    name. Nothing else is consulted -- not the narrative, not the run, not a
    clock, not a store.
    """
    if metric in NEVER_MATERIAL_METRICS:
        # Reported, never a finding. Checked FIRST so a throughput move can
        # never reach a threshold by any other route.
        return False

    delta = after - before
    if delta == 0.0:
        # The exact rule below fires on any *change*, never on any *value*: a
        # run that held `false_match_rate` at 0.0 has not drifted.
        return False
    if metric in EXACT_METRICS:
        return True
    if metric in _RATE_METRICS:
        return abs(delta) >= RATE_MATERIAL_DELTA
    if before == 0:
        # No ratio is defined against a zero baseline, so any appearance counts
        # -- the same rule the reason codes get, for the same reason.
        return True
    return abs(delta) / abs(before) >= MAGNITUDE_MATERIAL_RATIO


def compare(
    baseline: RunSummary,
    current: RunSummary,
    *,
    baseline_metrics: Metrics,
    current_metrics: Metrics,
    baseline_census: Mapping[str, int],
    current_census: Mapping[str, int],
    narrative: str | None = None,
) -> DriftReport:
    """Compare two scored runs and report what moved.

    Every argument is a plain model or a plain dict: the two `RunSummary` rows
    name the runs, the two `Metrics` carry the numbers, and the two censuses are
    `{reason_code: count}` as `Repo.reason_code_census` groups them in SQL. This
    function reads nothing else and calls nothing else.

    `narrative` is carried through onto the report untouched. It is **never**
    consulted: pass it or omit it and the moves are identical.
    """
    moves = [
        _move(metric, getattr(baseline_metrics, metric), getattr(current_metrics, metric))
        for metric in COMPARED_METRICS
    ]

    reason_code_moves = [
        ReasonCodeMove(
            reason_code=code,
            before=before,
            after=after,
            appeared=before == 0 and after > 0,
        )
        for code, before, after in (
            (code, int(baseline_census.get(code, 0)), int(current_census.get(code, 0)))
            # Sorted, so two runs compared twice report in the same order; the
            # union, so a code that stopped firing is reported as well as one
            # that started.
            for code in sorted(set(baseline_census) | set(current_census))
        )
        # A code that fired the same number of times in both runs is not drift.
        if before != after
    ]

    return DriftReport(
        baseline_run_id=baseline.run_id,
        current_run_id=current.run_id,
        moves=moves,
        reason_code_moves=reason_code_moves,
        narrative=narrative,
    )


def _move(metric: str, before: float, after: float) -> MetricMove:
    return MetricMove(
        metric=metric,
        before=float(before),
        after=float(after),
        delta=float(after) - float(before),
        material=is_material(metric, before, after),
    )
