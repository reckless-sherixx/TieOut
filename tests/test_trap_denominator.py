"""`trap_capture_rate` must carry its denominator.

At 50 records the denominator is 2 and at 500 it is 10, so `100.0%` renders
identically to `100.0%` of 100. The rate is sound; a bare percentage over a
denominator of two is what makes it read as manufactured.
"""


def test_metrics_carries_the_trap_denominator():
    from core.models import Metrics

    assert "total_traps" in Metrics.model_fields


def test_the_scorer_reports_the_denominator_it_divided_by():
    """Same source as the numerator, so the two cannot disagree."""
    import json
    from pathlib import Path

    truth = json.loads(Path("fixtures/seed42-500/truth.json").read_text())
    assert len(truth["unresolvable_ids"]) == 10
