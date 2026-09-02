"""The report is tested as a DOCUMENT, never as bytes.

A byte-length assertion passes for a PDF that renders a blank page, and a
`b"%PDF"` prefix check passes for a PDF whose figures disagree with the run it
claims to describe. Both are the class of defect this report exists to answer:
the console hardcoded a tier confidence and drifted from the engine, and a
report that hardcodes a rate would drift from the run in exactly the same way
with nothing going red.

So every test here reads the PDF back with `pypdf` and asserts on the words and
figures a merchant would see.
"""

from __future__ import annotations

import ast
import io
import pathlib

import pytest
from pypdf import PdfReader

from core.models import (
    MatchGroup,
    Metrics,
    ReasonCode,
    ReconException,
    RunSummary,
    TierConfidence,
)
from report import build_report


# --- helpers ----------------------------------------------------------------


def text_of(pdf: bytes) -> str:
    """Every page's extracted text, with runs of whitespace collapsed.

    Collapsing is necessary and sufficient: a PDF carries no paragraphs, so a
    line break inside a sentence is an artefact of layout rather than of
    content, and every figure this suite asserts on is rendered as a short
    atomic cell that cannot wrap.
    """
    reader = PdfReader(io.BytesIO(pdf))
    return " ".join(" ".join(page.extract_text() for page in reader.pages).split())


def pages_of(pdf: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf)).pages)


def metrics(**overrides) -> Metrics:
    base = dict(
        auto_match_rate=0.8757763975155279,
        assisted_match_rate=0.0,
        exception_rate=0.1789,
        false_match_rate=0.0,
        precision=1.0,
        recall_on_resolvable=0.8757763975155279,
        trap_capture_rate=1.0,
        total_traps=10,
        llm_rejection_rate=0.0,
        throughput_records_per_sec=812.5,
        llm_cost_usd_per_100=0.0,
        llm_tokens_per_100=0,
        tier_counts={"T0": 141, "T1": 2, "T2": 8, "T3": 5, "LLM": 0},
        itc_substantiated_paise=1_234_56,
        itc_at_risk_paise=98_700,
        itc_variance_paise=-4_500,
    )
    base.update(overrides)
    return Metrics(**base)


def tier_confidence(**overrides) -> dict[str, TierConfidence]:
    base = {
        "T0": TierConfidence(confidence_observed=1.0, confidence_conflict=False),
        "T1": TierConfidence(confidence_observed=0.95, confidence_conflict=False),
        "T2": TierConfidence(confidence_observed=0.99, confidence_conflict=False),
        "T3": TierConfidence(confidence_observed=0.80, confidence_conflict=False),
        "LLM": TierConfidence(confidence_observed=0.70, confidence_conflict=False),
    }
    base.update(overrides)
    return base


def run(**overrides) -> RunSummary:
    base = dict(
        run_id="run-7f3a19c4",
        seed=42,
        record_count=500,
        state="completed",
        created_at="2026-09-02T11:04:07+00:00",
        match_count=156,
        exception_count=32,
        metrics=metrics(),
        tier_confidence=tier_confidence(),
    )
    base.update(overrides)
    return RunSummary(**base)


def an_exception(**overrides) -> ReconException:
    base = dict(
        exception_id="exc-0001",
        subject_type="bank_line",
        subject_id="BL-000417",
        reason_code=ReasonCode.AMOUNT_MISMATCH,
        amount=4_932_00,
        llm_hypothesis=None,
        verifier_verdict="not_attempted",
        verifier_reason=None,
        failed_check=None,
    )
    base.update(overrides)
    return ReconException(**base)


def a_match(**overrides) -> MatchGroup:
    base = dict(
        match_id="m1",
        bank_line_id="BL-000001",
        settlement_id="setl_0001",
        psp_txn_ids=["pay_0001"],
        order_ids=["ORD-000001"],
        gross=500_000,
        fees=11_800,
        tax=2_124,
        refunds=0,
        holds=0,
        net=486_076,
        tier="T0",
        confidence=1.0,
        evidence=["settlement id in narration", "reconstruction closes exactly"],
    )
    base.update(overrides)
    return MatchGroup(**base)


class Row:
    """A quarantine row, shaped like `core.store.repo.UploadedRow`."""

    def __init__(self, row_number: int, raw: str, reason: str, detail: str) -> None:
        self.row_number = row_number
        self.raw = raw
        self.reason = reason
        self.detail = detail


class Upload:
    """An upload, shaped like `core.store.repo.UploadSummary`."""

    def __init__(self, upload_id: str, filename: str, **kw) -> None:
        self.upload_id = upload_id
        self.filename = filename
        self.content_sha256 = kw.get("content_sha256", "a" * 64)
        self.byte_size = kw.get("byte_size", 20_481)
        self.format_id = kw.get("format_id", "razorpay-settlement-v1")
        self.format_version = kw.get("format_version", "1")
        self.confidence = kw.get("confidence", 0.94)
        self.encoding = kw.get("encoding", "utf-8")
        self.state = kw.get("state", "ingested")
        self.record_count = kw.get("record_count", 412)
        self.quarantine_count = kw.get("quarantine_count", 1)
        self.skipped_rows = kw.get("skipped_rows", 0)
        self.order_count = kw.get("order_count", 412)
        self.psp_txn_count = kw.get("psp_txn_count", 0)
        self.bank_line_count = kw.get("bank_line_count", 0)
        self.uploaded_at = kw.get("uploaded_at", "2026-09-02T09:15:00+00:00")


# --- the document exists ----------------------------------------------------


def test_it_produces_a_pdf_that_opens_and_has_at_least_one_page():
    pdf = build_report(run(), matches=[], exceptions=[])
    assert pdf[:4] == b"%PDF"
    assert pages_of(pdf) >= 1


def test_the_run_id_is_in_the_document():
    pdf = build_report(run(), matches=[], exceptions=[])
    assert "run-7f3a19c4" in text_of(pdf)


# --- the figures equal the run's wire values --------------------------------


def test_the_auto_match_rate_printed_is_the_run_s_own_value():
    """0.8757763975155279 renders as 87.6% and as nothing else."""
    pdf = build_report(
        run(metrics=metrics(auto_match_rate=0.8757763975155279)),
        matches=[],
        exceptions=[],
    )
    assert "87.6%" in text_of(pdf)


def test_a_different_wire_value_prints_a_different_figure():
    """The figure follows the run, which a hardcoded table would not."""
    pdf = build_report(
        run(
            metrics=metrics(
                auto_match_rate=0.5123, recall_on_resolvable=0.5123
            )
        ),
        matches=[],
        exceptions=[],
    )
    body = text_of(pdf)
    assert "51.2%" in body
    assert "87.6%" not in body


def test_the_trap_capture_rate_prints_its_denominator():
    """`100.0%` of 2 and `100.0%` of 10 are the same string without it."""
    pdf = build_report(
        run(metrics=metrics(trap_capture_rate=1.0, total_traps=10)),
        matches=[],
        exceptions=[],
    )
    body = text_of(pdf)
    assert "10 of 10" in body
    assert "100.0%" in body


def test_a_total_traps_of_none_says_not_recorded_and_never_zero():
    pdf = build_report(
        run(metrics=metrics(trap_capture_rate=1.0, total_traps=None)),
        matches=[],
        exceptions=[],
    )
    body = text_of(pdf)
    assert "not recorded" in body
    assert "0 of 0" not in body


# --- an unscored run --------------------------------------------------------


def test_an_unscored_run_prints_no_rates_at_all():
    """`metrics is None` is an absence. A zero would be a measured claim."""
    pdf = build_report(
        run(seed=-1, metrics=None, tier_confidence=tier_confidence()),
        matches=[],
        exceptions=[],
        uploads=[Upload("upl-1", "january-settlement.csv")],
    )
    body = text_of(pdf)
    assert "0.0%" not in body
    assert "unscored" in body.lower()


def test_an_unscored_run_still_names_the_files_it_read():
    pdf = build_report(
        run(seed=-1, metrics=None),
        matches=[],
        exceptions=[],
        uploads=[Upload("upl-1", "january-settlement.csv")],
    )
    body = text_of(pdf)
    assert "january-settlement.csv" in body
    assert "upl-1" in body


# --- the tier ladder --------------------------------------------------------


def test_the_llm_confidence_is_the_engine_s_stamp_and_never_a_word():
    """The defect that started this work: a table that said "verified"."""
    pdf = build_report(
        run(
            tier_confidence=tier_confidence(
                LLM=TierConfidence(confidence_observed=0.70, confidence_conflict=False)
            )
        ),
        matches=[],
        exceptions=[],
    )
    body = text_of(pdf)
    assert "0.70" in body
    assert "verified" not in body.lower()


def test_a_tier_that_stamped_two_confidences_reports_neither():
    pdf = build_report(
        run(
            tier_confidence=tier_confidence(
                T3=TierConfidence(confidence_observed=None, confidence_conflict=True)
            )
        ),
        matches=[],
        exceptions=[],
    )
    assert "more than one confidence" in text_of(pdf)


def test_a_run_with_no_tier_confidence_says_not_reported():
    pdf = build_report(run(tier_confidence=None), matches=[], exceptions=[])
    assert "not reported" in text_of(pdf)


def test_a_confidence_with_no_prepared_sentence_still_prints_its_figure():
    """The figure is the engine's; the sentence beside it is only a gloss."""
    pdf = build_report(
        run(
            tier_confidence=tier_confidence(
                T3=TierConfidence(confidence_observed=0.55, confidence_conflict=False)
            )
        ),
        matches=[],
        exceptions=[],
    )
    body = text_of(pdf)
    assert "0.55" in body
    assert "0.80" not in body


def test_an_unscored_run_groups_the_supplied_matches_by_their_own_tier():
    """No Metrics means no tier_counts. The MatchGroup rows still carry a tier."""
    pdf = build_report(
        run(seed=-1, metrics=None, match_count=2),
        matches=[a_match(tier="T0"), a_match(match_id="m2", tier="T3")],
        exceptions=[],
    )
    body = text_of(pdf)
    assert "0.0%" not in body
    assert "grouped by the tier each one carries" in body


def test_every_rung_and_its_match_count_appear():
    pdf = build_report(
        run(metrics=metrics(tier_counts={"T0": 141, "T1": 2, "T2": 8, "T3": 5, "LLM": 0})),
        matches=[],
        exceptions=[],
    )
    body = text_of(pdf)
    for tier in ("T0", "T1", "T2", "T3", "LLM"):
        assert tier in body
    assert "141" in body


# --- exceptions -------------------------------------------------------------


def test_an_empty_exception_list_produces_a_valid_pdf():
    pdf = build_report(run(exception_count=0), matches=[], exceptions=[])
    assert pages_of(pdf) >= 1
    assert "run-7f3a19c4" in text_of(pdf)


def test_every_exception_carries_its_reason_code_and_subject():
    pdf = build_report(
        run(),
        matches=[],
        exceptions=[
            an_exception(exception_id="exc-0001", subject_id="BL-000417"),
            an_exception(
                exception_id="exc-0002",
                subject_id="BL-000902",
                reason_code=ReasonCode.AMBIGUOUS_MULTI_CANDIDATE,
            ),
        ],
    )
    body = text_of(pdf)
    assert "BL-000417" in body
    assert "BL-000902" in body
    assert "AMOUNT_MISMATCH" in body
    assert "AMBIGUOUS_MULTI_CANDIDATE" in body


def test_exception_amounts_are_formatted_by_the_project_s_one_money_formatter():
    from core.money import fmt_inr

    pdf = build_report(run(), matches=[], exceptions=[an_exception(amount=4_932_00)])
    assert fmt_inr(493_200) in text_of(pdf)


def test_the_amount_tolerance_is_rendered_by_the_money_formatter():
    """100 paise, formatted once, not typed out as a string with a rupee in it."""
    from core.money import fmt_inr

    pdf = build_report(run(), matches=[], exceptions=[])
    assert fmt_inr(100) in text_of(pdf)


# --- quarantine -------------------------------------------------------------


def test_a_quarantined_row_full_of_markup_characters_still_renders():
    """`raw` is merchant data, and the renderer parses a small XML dialect.

    An ampersand in a bank narration must not become markup, and must not
    raise while rendering the report about the file it arrived in.
    """
    pdf = build_report(
        run(),
        matches=[],
        exceptions=[],
        quarantine=[
            Row(7, "NEFT <M&S> & CO. \"partial\"", "TRUNCATED_ROW", "3 of 9 columns")
        ],
    )
    body = text_of(pdf)
    assert "M&S" in body
    assert "TRUNCATED_ROW" in body


def test_quarantine_rows_are_tabulated_when_passed():
    pdf = build_report(
        run(),
        matches=[],
        exceptions=[],
        quarantine=[Row(41, "31-01-2026,,,,", "TRUNCATED_ROW", "4 of 9 columns")],
    )
    body = text_of(pdf)
    assert "TRUNCATED_ROW" in body
    assert "41" in body


def test_no_quarantine_argument_says_so_rather_than_showing_an_empty_table():
    pdf = build_report(run(), matches=[], exceptions=[])
    assert "not supplied" in text_of(pdf)


# --- standing limits --------------------------------------------------------


def test_the_standing_limits_are_present():
    pdf = build_report(run(), matches=[], exceptions=[])
    assert "What this run cannot tell you" in text_of(pdf)


def test_the_llm_limit_is_conditioned_on_the_run_having_billed_nothing():
    billed = build_report(
        run(metrics=metrics(llm_tokens_per_100=8_412, llm_cost_usd_per_100=0.0031)),
        matches=[],
        exceptions=[],
    )
    unbilled = build_report(
        run(metrics=metrics(llm_tokens_per_100=0, llm_cost_usd_per_100=0.0)),
        matches=[],
        exceptions=[],
    )
    claim = "zero is not proof of absence"
    assert claim in text_of(unbilled)
    assert claim not in text_of(billed)


# --- provenance -------------------------------------------------------------


def test_a_seeded_run_reports_its_seed():
    pdf = build_report(run(seed=42), matches=[], exceptions=[])
    body = text_of(pdf)
    assert "42" in body
    assert "500" in body


# --- the boundary -----------------------------------------------------------

FORBIDDEN = ("generator", "scorer", "truth")


def _report_sources() -> list[pathlib.Path]:
    root = pathlib.Path(__file__).resolve().parents[2] / "report"
    return sorted(root.rglob("*.py"))


@pytest.mark.parametrize("path", _report_sources(), ids=lambda p: p.name)
def test_report_never_imports_the_graded_lanes(path: pathlib.Path):
    """`report/` renders what it is handed and computes no grade of its own.

    `tests/test_boundaries.py` guards `core/matcher` and `core/llm` for the
    same reason: anything that could see ground truth could quietly improve
    the number it reports on.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)

    for module in modules:
        parts = module.split(".")
        assert not any(part in FORBIDDEN for part in parts), (
            f"{path.name} imports {module!r}"
        )
