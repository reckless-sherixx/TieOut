"""Drift detection (spec §7): what changed between two runs, and whether it matters.

The system reports on one batch when invoked. It does not watch, compare, or
notice. A finance controller's actual question is rarely "what is the match
rate" -- it is "what changed since last time, and why". A match rate that falls
from 98% to 91% overnight because a new deduction type appeared is the finding;
the 91% on its own is not.

`compare()` in `core.drift.compare` takes two `RunSummary` + `Metrics` pairs and
their reason-code distributions as plain arguments and returns a `DriftReport`.

Two rules give the package its shape:

* **detection is deterministic.** Every threshold is a named constant in
  `compare.py`, `material` is a pure function of two numbers and a metric name,
  and the LLM writes `narrative` and nothing else. The narrative arrives as an
  argument and is never read -- the same division of labour the verifier already
  enforces on the analyst layer.
* **nothing here touches the store.** Everything arrives as a plain model or a
  plain dict, including the reason-code counts, which `Repo.reason_code_census`
  groups in SQL. That is what keeps the whole package testable with no database,
  and `tests/drift/test_determinism_boundary.py` proves it structurally rather
  than trusting it.

Nothing is re-exported here on purpose: `from core.drift import compare` must
name the module, not shadow it with the function inside.
"""
