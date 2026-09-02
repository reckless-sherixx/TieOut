"""`obfuscated_settlement_ref`, end to end on a generated dataset.

`tests/generator/test_defects.py` pins what the injector writes; this file pins
what the whole path does with it, on data the generator actually produced:
residue -> analyst -> verifier -> accept loop -> scorer.

Three outcomes have to be measurable, and all three are asserted here, because
a capability that can only be observed succeeding is not a measurement:

1. a correct recovery raises `assisted_match_rate` and `tier_counts["LLM"]`;
2. an accepted hypothesis that disagrees with truth raises `false_match_rate`;
3. a recovery the verifier rejects raises `llm_rejection_rate` and the subject
   stays an exception carrying the verdict.

**No live model call is made here.** The analyst is a stub, exactly as in
`tests/llm/test_pipeline.py`; what is under test is the plumbing and the
grading, never a model's skill. The live numbers live in the report.

The dataset is 200 records rather than 500 to keep the suite fast: matching is
O(n^2), and 200 still carries four obfuscated instances (2 per 100 records).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from core.canonicalize.narration import canonicalize
from core.generator.emit import emit_dataset
from core.generator.pipeline import build_dataset
from core.ingest.reader import read_bank, read_orders, read_psp
from core.llm.pipeline import LLM_TIER, merge, run_llm_pass
from core.matcher.engine import run_match
from core.matcher.tiers import WINDOW_DAYS
from scorer.score import score

SEED = 42
COUNT = 200
DEFECT = "obfuscated_settlement_ref"


class StubAnalystClient:
    """The injected client. `call` is the whole `AnalystClient` Protocol."""

    def __init__(self, payload):
        self.payload = payload
        self.prompts: list[str] = []
        self.tokens = 0
        self.cost_usd = 0.0

    def call(self, prompt: str, schema: dict):
        self.prompts.append(prompt)
        return self.payload


@pytest.fixture(scope="module")
def dataset(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp(f"seed{SEED}-{COUNT}")
    emit_dataset(*build_dataset(seed=SEED, count=COUNT), out_dir=out, seed=SEED)
    return out


@pytest.fixture(scope="module")
def truth(dataset) -> dict:
    return json.loads((dataset / "truth.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def obfuscated_lines(truth) -> list[str]:
    lines = sorted(
        line_id
        for d in truth["injected_defects"]
        if d["defect_type"] == DEFECT
        for line_id in d["affected_ids"]
    )
    assert lines, "the dataset carries no obfuscated_settlement_ref instance"
    return lines


def _records(directory: Path):
    return (
        read_orders(directory / "orders.csv"),
        read_psp(directory / "psp.csv"),
        read_bank(directory / "bank.csv"),
    )


def _linkages(truth: dict) -> dict[str, dict]:
    return {entry["bank_line_id"]: entry for entry in truth["linkages"]}


def _hypothesis(subject_id: str, line_id: str | None, legs: list[str]) -> dict:
    return {
        "subject_id": subject_id,
        "proposed_bank_line_id": line_id,
        "proposed_psp_txn_ids": sorted(legs),
        "proposed_order_ids": [],
        "reasoning": "the narration names this settlement in a non-canonical form",
        "self_confidence": 0.9,
    }


def _run(directory: Path, payload):
    """Deterministic run, then one analyst pass, then the scorer."""
    orders, psp, bank = _records(directory)
    deterministic = run_match(orders, psp, bank, run_id="run-obfuscated")
    outcome = run_llm_pass(
        deterministic, orders, psp, bank, client=StubAnalystClient(payload)
    )
    merged = merge(deterministic, outcome)
    metrics = score(
        merged,
        directory / "truth.json",
        elapsed_seconds=1.0,
        hypotheses_proposed=outcome.proposed,
        hypotheses_rejected=outcome.rejected,
    )
    return deterministic, outcome, merged, metrics


# --- the residue the capability exists to create -------------------------------


def test_every_obfuscated_line_reaches_the_residue_unmatched(dataset, obfuscated_lines):
    """If a tier matched one of these, that instance is deterministic work and
    the analyst would never be shown it."""
    orders, psp, bank = _records(dataset)
    result = run_match(orders, psp, bank, run_id="run-residue")

    matched = {m.bank_line_id for m in result.matches}
    excepted = {e.subject_id for e in result.exceptions if e.subject_type == "bank_line"}
    assert not (matched & set(obfuscated_lines))
    assert set(obfuscated_lines) <= excepted


def test_the_canonicaliser_recovers_none_of_the_generated_narrations(
    dataset, obfuscated_lines, truth
):
    """The honesty assertion, on the emitted CSV rather than on a template.

    `SETTLEMENT_RE` is searched on the RAW narration, so this is the same read
    `CandidatePool` makes. Zero recoveries, and the settlement id must not
    appear verbatim anywhere on the row -- including the `utr` column, which is
    the other place `referenced_settlement` looks.
    """
    linkages = _linkages(truth)
    with open(dataset / "bank.csv", encoding="utf-8", newline="") as handle:
        rows = {r["line_id"]: r for r in csv.DictReader(handle)}

    for line_id in obfuscated_lines:
        row = rows[line_id]
        settlement_id = linkages[line_id]["settlement_id"]
        assert canonicalize(row["narration"]).settlement_id is None, row["narration"]
        assert canonicalize(row["utr"]).settlement_id is None, row["utr"]
        assert settlement_id not in row["narration"]
        assert settlement_id not in row["utr"]


def test_the_line_posts_late_and_the_arithmetic_still_closes(
    dataset, obfuscated_lines, truth
):
    """Both halves of the mechanism, on the emitted data.

    The date window is why the subject is in the residue; the closing sum is
    why a correct recovery can be ACCEPTED rather than rejected on
    `arithmetic`. Break either and the capability measures something else.
    """
    orders, psp, bank = _records(dataset)
    lines = {b.line_id: b for b in bank}
    linkages = _linkages(truth)
    by_id = {t.txn_id: t for t in psp}

    for line_id in obfuscated_lines:
        line = lines[line_id]
        linkage = linkages[line_id]
        legs = [by_id[i] for i in linkage["psp_txn_ids"]]
        settled = max(t.settled_at for t in legs if t.settled_at)
        assert (line.txn_date - settled).days > WINDOW_DAYS
        assert sum(t.amount for t in legs) == line.credit


# --- outcome 1: a correct recovery is accepted and raises the assisted rate -----


def test_a_correct_recovery_is_accepted_and_counted_as_llm(
    dataset, obfuscated_lines, truth
):
    linkages = _linkages(truth)
    payload = [
        _hypothesis(line_id, line_id, linkages[line_id]["psp_txn_ids"])
        for line_id in obfuscated_lines
    ]
    deterministic, outcome, merged, metrics = _run(dataset, payload)

    assert outcome.accepted == len(obfuscated_lines)
    assert outcome.rejected == 0
    assert metrics.tier_counts["LLM"] == len(obfuscated_lines)
    assert metrics.assisted_match_rate > 0.0

    # The guardrail numbers must not move: harder data may not buy a wrong answer.
    assert metrics.false_match_rate == 0.0
    assert metrics.precision == 1.0
    assert metrics.trap_capture_rate == 1.0

    # And the deterministic half is untouched -- the analyst added subjects, it
    # did not re-decide any.
    baseline = score(deterministic, dataset / "truth.json", elapsed_seconds=1.0)
    assert metrics.auto_match_rate == baseline.auto_match_rate
    assert metrics.tier_counts["T0"] == baseline.tier_counts["T0"]

    resolved = {m.bank_line_id for m in merged.matches if m.tier == LLM_TIER}
    assert resolved == set(obfuscated_lines)
    still_open = {e.subject_id for e in merged.exceptions}
    assert not (still_open & set(obfuscated_lines))


def test_the_prompt_shows_the_analyst_the_narration_and_the_candidates(
    dataset, obfuscated_lines, truth
):
    """The capability fails for a reason unrelated to the model if the prose is
    never rendered. `render_prompt` must put the bank line's narration text and
    the candidate settlements' ids in front of it."""
    orders, psp, bank = _records(dataset)
    deterministic = run_match(orders, psp, bank, run_id="run-prompt")
    client = StubAnalystClient([])
    run_llm_pass(deterministic, orders, psp, bank, client=client)

    (prompt,) = client.prompts
    lines = {b.line_id: b for b in bank}
    linkages = _linkages(truth)
    for line_id in obfuscated_lines:
        assert lines[line_id].narration in prompt, line_id
        assert linkages[line_id]["settlement_id"] in prompt, line_id


# --- outcome 3: a rejected recovery stays an exception -------------------------


def test_a_recovery_the_verifier_rejects_stays_an_exception(
    dataset, obfuscated_lines, truth
):
    """The model recovers a reference; the verifier does every rupee.

    Here it recovers the WRONG one -- another obfuscated line's settlement,
    which is unclaimed and complete and therefore passes existence,
    exclusivity and coherence. It fails on `arithmetic`, because that
    settlement's net is not this line's credit. Confidence buys nothing.

    The swap is chosen so the decoy settled on or before the subject's bank
    date. Without that, half the pairs fail on `causality` instead -- also a
    correct rejection, but a different one, and a test that accepts either
    check would no longer be pinning that the MONEY is what refused.
    """
    linkages = _linkages(truth)
    orders, psp, bank = _records(dataset)
    lines = {b.line_id: b for b in bank}
    by_id = {t.txn_id: t for t in psp}

    def settled_on(line_id: str):
        legs = [by_id[i] for i in linkages[line_id]["psp_txn_ids"]]
        return max(t.settled_at for t in legs if t.settled_at)

    payload = []
    for line_id in obfuscated_lines:
        decoys = [
            other
            for other in obfuscated_lines
            if other != line_id and settled_on(other) <= lines[line_id].txn_date
        ]
        if decoys:
            payload.append(
                _hypothesis(line_id, line_id, linkages[decoys[0]]["psp_txn_ids"])
            )
    assert payload, "no causally valid decoy: this dataset cannot express the test"

    _, outcome, merged, metrics = _run(dataset, payload)

    assert outcome.accepted == 0
    assert outcome.rejected == len(payload)
    assert metrics.llm_rejection_rate == 1.0
    assert metrics.tier_counts["LLM"] == 0
    assert metrics.assisted_match_rate == 0.0
    assert metrics.false_match_rate == 0.0, "a rejection is not a wrong answer"

    kept = {e.subject_id: e for e in merged.exceptions}
    for proposal in payload:
        exception = kept[proposal["subject_id"]]
        assert exception.verifier_verdict == "rejected"
        assert exception.failed_check == "arithmetic"
        assert exception.llm_hypothesis
        assert exception.verifier_reason


def test_a_confident_wrong_recovery_is_rejected_exactly_as_hard(
    dataset, obfuscated_lines, truth
):
    """`self_confidence` is never read. The mutant this kills is a verifier
    that lets a sufficiently sure model past `arithmetic`."""
    linkages = _linkages(truth)
    other = obfuscated_lines[1]
    subject = obfuscated_lines[0]
    payload = [_hypothesis(subject, subject, linkages[other]["psp_txn_ids"])]
    payload[0]["self_confidence"] = 1.0

    _, outcome, _, metrics = _run(dataset, payload)
    assert outcome.accepted == 0
    assert metrics.llm_rejection_rate == 1.0


# --- outcome 2: an accepted hypothesis that disagrees with truth ---------------


def test_an_accepted_hypothesis_recovers_a_damaged_order_ref_and_is_not_a_false_match(
    tmp_path, obfuscated_lines, truth, dataset
):
    """The accept loop recovers the order behind an anonymous leg, as the pool does.

    **This test used to document a defect and now proves its fix.** Its history,
    because the construction only makes sense with it:

    It was written as `test_an_accepted_hypothesis_that_disagrees_with_truth_is_
    a_false_match`, to show `false_match_rate` live on the LLM path rather than
    only on the tiers. Constructing that took care, and the reason is still worth
    knowing: on this dataset a *wrong settlement* can never survive the verifier.
    The correct settlement closes the credit exactly, so any other one that
    closed it too would make `_uniqueness` see two candidates and reject both.
    The only way past every check and still wrong was to be right about the
    settlement and wrong about what it contains.

    So the leg's `order_id` is blanked in `psp.csv` -- the `missing_order_ref`
    damage -- while `truth.json` keeps the answer (CSV_SCHEMAS 5.1). The verifier
    passed, `_coherence` asking only for the complete leg set, which it is. But
    `pipeline._order_ids` scraped `txn.order_id` off the legs where
    `CandidatePool.order_ids` *recovers* the missing one, so
    `MatchGroup.order_ids` came back short by one order, `disagreement` saw it,
    and a correct match was graded false. `OBFUSCATED-REF-REPORT.md` 9.1 recorded
    that as the only demonstrable false-match route on the LLM path and pinned
    it here rather than fixing it, the accept loop being out of that lane's
    scope.

    `_order_ids` now delegates to `CandidatePool.order_ids`, so the LLM path
    recovers exactly what the deterministic path recovers. The damage is
    unchanged; the grade is not. The verifier's job is still the money and the
    scorer's is still the answer -- they now agree.

    Note what this test no longer demonstrates: `false_match_rate` firing on the
    LLM path. That route is closed, and `OBFUSCATED-REF-REPORT.md` 9.6 already
    explains why the metric is structurally hard to make fire here at all.
    """
    linkages = _linkages(truth)
    subject = obfuscated_lines[0]
    settlement_id = linkages[subject]["settlement_id"]

    damaged = tmp_path / "damaged"
    damaged.mkdir()
    for name in ("orders.csv", "bank.csv", "truth.json"):
        (damaged / name).write_bytes((dataset / name).read_bytes())

    with open(dataset / "psp.csv", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or ())
        rows = list(reader)
    blanked = 0
    for row in rows:
        if (
            row["settlement_id"] == settlement_id
            and row["txn_type"] == "payment"
            and row["order_id"]
            and not blanked
        ):
            row["order_id"] = ""
            blanked = 1
    assert blanked, f"{settlement_id} has no order-bearing payment leg to damage"
    with open(damaged / "psp.csv", "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    payload = [_hypothesis(subject, subject, linkages[subject]["psp_txn_ids"])]
    _, outcome, merged, metrics = _run(damaged, payload)

    assert outcome.accepted == 1, "every verifier check still passes"
    assert outcome.rejected == 0
    assert metrics.tier_counts["LLM"] == 1
    assert metrics.false_match_rate == 0.0, (
        "the blanked order_id is recovered, so the match agrees with truth"
    )
    assert metrics.precision == 1.0

    match = next(m for m in merged.matches if m.bank_line_id == subject)
    # Set equality, where this asserted a strict subset before the fix. The
    # recovered order is back in the set and the match is truth's answer.
    assert set(match.order_ids) == set(linkages[subject]["order_ids"])
