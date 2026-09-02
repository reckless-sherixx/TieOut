"""`score(result, truth_path) -> Metrics` -- every field of spec section 9.

The engine produces a `MatchResult` having never seen the answers; this is
where the answers are read and the run is graded. Nothing here feeds back into
the matcher, and nothing in the matcher imports this package.

`llm_rejection_rate`, `llm_cost_usd_per_100` and `llm_tokens_per_100` belong to
the analyst layer, which is a different lane. On a deterministic run they are
zero -- but the fields stay present and typed, because the shape is frozen and
the API lane serialises it.
"""

from __future__ import annotations

from pathlib import Path

from core.matcher.engine import MatchResult
from core.models import Metrics
from scorer import metrics as m


def score(
    result: MatchResult,
    truth_path: Path | str,
    *,
    elapsed_seconds: float | None = None,
    hypotheses_proposed: int = 0,
    hypotheses_rejected: int = 0,
    llm_cost_usd: float = 0.0,
    llm_tokens: int = 0,
    itc_substantiated_paise: int = 0,
    itc_at_risk_paise: int = 0,
    itc_variance_paise: int = 0,
) -> Metrics:
    """Grade one run.

    `elapsed_seconds` is handed in rather than measured: no wall-clock lives
    inside `core/`, so the run is timed at the boundary that invoked it. An
    untimed run reports a throughput of 0.0 rather than inventing one.

    The three `itc_*` totals arrive the same way, for a stronger version of the
    same reason (spec §6). This function grades matching against truth and must
    not grow a second responsibility: the ITC report reconciles the run against
    the PSP's tax invoice, it consults no ground truth at all, and computing it
    here would hide a second, unrelated job behind the word "score".
    `core/itc/reconcile.py` produces the `ITCReport`; `api/jobs.py` hands its
    totals in, exactly as it hands in `llm_cost_usd` and `llm_tokens`.

    They default to zero so a dataset with no `psp_gst_invoice.csv` -- every
    dataset generated before that capability existed -- still scores normally.
    Zero is the honest answer there: nothing is substantiated when there is no
    invoice to substantiate anything against.

    Unlike every other number returned here they are passed **through
    untouched**: no rate, no per-100 normalisation, no float. They are rupee
    figures in integer paise, and dividing one would produce a fraction of a
    paise no document supports.
    """
    truth = m.load(truth_path)
    comparison = m.compare(result, truth)

    resolvable = len(truth.resolvable_ids)
    correct_on_resolvable = len(comparison.agreed & truth.resolvable_ids)

    false_match_rate = m.rate(
        len(comparison.disagreed), comparison.total_matches
    )

    # Both match rates are `subjects / resolvable subjects`, so the numerator
    # has to be restricted to resolvable subjects too. Counting matches instead
    # -- including matches on the trap lines, which are not in the denominator
    # at all -- let a run report `auto_match_rate = 1.5` while `Metrics`
    # documents "Rates are 0.0-1.0" and strict mode range-checks nothing.
    auto_matched = len(comparison.deterministic_subjects & truth.resolvable_ids)
    assisted_matched = len(comparison.assisted_subjects & truth.resolvable_ids)

    return Metrics(
        auto_match_rate=m.rate(auto_matched, resolvable),
        assisted_match_rate=m.rate(assisted_matched, resolvable),
        # Numerator and denominator both come from truth: every subject truth
        # records that the run did not match, over every subject truth records.
        # A subject the engine loses counts exactly like one it excepts.
        exception_rate=m.rate(
            len(comparison.unmatched_in_truth(truth)), comparison.total_subjects
        ),
        false_match_rate=false_match_rate,
        precision=1.0 - false_match_rate,
        recall_on_resolvable=m.rate(correct_on_resolvable, resolvable),
        # An empty trap set is captured, not failed: declining to resolve
        # something that is not there is the correct behaviour.
        trap_capture_rate=m.rate(
            comparison.traps_left_alone, comparison.total_traps, empty=1.0
        ),
        llm_rejection_rate=m.rate(hypotheses_rejected, hypotheses_proposed),
        throughput_records_per_sec=m.rate(
            result.record_count, elapsed_seconds if elapsed_seconds else 0
        ),
        llm_cost_usd_per_100=m.rate(llm_cost_usd * 100.0, result.record_count),
        llm_tokens_per_100=int(m.rate(llm_tokens * 100, result.record_count)),
        # Straight from the engine's own tier assignment, and deliberately not
        # from anything here. `Comparison` already partitions subjects into
        # deterministic and assisted, but those are *subjects intersected with
        # truth* -- a different quantity that would silently disagree with the
        # tier the engine recorded on the match. The breakdown answers "what did
        # each tier do", which is the engine's claim, not the scorer's.
        tier_counts=dict(result.tier_breakdown),
        # Straight through. Every other field here is derived; these three were
        # computed by `core/itc/` against a document this package never opens.
        itc_substantiated_paise=itc_substantiated_paise,
        itc_at_risk_paise=itc_at_risk_paise,
        itc_variance_paise=itc_variance_paise,
    )


def explain(result: MatchResult, truth_path: Path | str) -> list[str]:
    """One line per subject the run got wrong, for a human to read.

    A metric that cannot be traced back to its subjects is a number nobody can
    check, and the whole point of this package is that the numbers are
    checkable.
    """
    truth = m.load(truth_path)
    comparison = m.compare(result, truth)

    lines = [
        f"{subject}: {reason}" for subject, reason in sorted(comparison.disagreed.items())
    ]
    for trap in sorted(truth.unresolvable_ids & comparison.matched_subjects):
        lines.append(
            f"{trap}: matched, but it is recorded as unresolvable -- the data does "
            f"not determine an answer, so any match here is a guess"
        )
    for missed in sorted(truth.resolvable_ids - comparison.matched_subjects):
        lines.append(f"{missed}: resolvable but left unmatched")
    return lines
