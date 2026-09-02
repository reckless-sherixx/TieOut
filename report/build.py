"""One run, rendered as a self-contained PDF (spec section 5).

`GET /api/runs/{id}/report.pdf` is wired above this module; nothing here knows
about HTTP, a repository or a session. `build_report` is handed objects and
returns bytes.

THE ONE RULE THIS FILE IS WRITTEN AROUND. **Every figure in the document comes
off the objects passed in.** Nothing here recomputes a rate, supplies a default,
or prints a plausible-looking constant. A value that is `None` is rendered as
the absence it is -- "not recorded" is a fact a reader can act on, and a
number that looks measured and is not is a lie that survives being quoted.

That rule is not abstract. The console carried a hardcoded table of tier
confidences and it drifted: it rendered a reassuring word on the rung the
engine stamps 0.70, and nothing bound the table to the engine, so nothing
caught it. This report reads `RunSummary.tier_confidence` -- the stamp the
engine itself put on this run's matches -- and prints "not reported" when the
run predates the field.

Two consequences worth naming:

* **An unscored run prints no rates.** A run over uploaded files has
  `metrics is None`, because a rate is measured against ground truth and no
  ground truth exists for a merchant's own exports. The report says so, and
  never prints a zero in place of a rate that is not coming.
* **`report/` may not import the generator, the scorer, or anything named
  truth.** `tests/test_boundaries.py` guards `core/matcher` and `core/llm` on
  exactly this argument; a renderer that could see the answer key could quietly
  improve the number it reports on. `tests/report/test_build.py` carries the
  same check for this package.
"""

from __future__ import annotations

import io
from typing import Any, Iterable, Protocol, Sequence
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from core.models import MatchGroup, Metrics, ReconException, RunSummary
from core.money import fmt_inr
from report import copy as prose
from report.fonts import body_faces

class QuarantineRow(Protocol):
    """The shape of `core.store.repo.UploadedRow`.

    Structural rather than nominal so this package does not import
    `core/store/`, which would drag SQLModel and a database engine into a
    renderer. The route hands in the real rows; the protocol is what this file
    is allowed to assume about them.
    """

    row_number: int
    raw: str
    reason: str
    detail: str


class UploadRow(Protocol):
    """The shape of `core.store.repo.UploadSummary`. See `QuarantineRow`."""

    upload_id: str
    filename: str
    content_sha256: str
    byte_size: int
    format_id: str
    state: str
    record_count: int
    quarantine_count: int
    uploaded_at: Any


# --- page geometry ----------------------------------------------------------

PAGE = A4
MARGIN = 18 * mm
CONTENT_WIDTH = PAGE[0] - 2 * MARGIN

INK = colors.HexColor("#14110F")
RULE = colors.HexColor("#C9C4BA")
MUTED = colors.HexColor("#5A554D")
BAND = colors.HexColor("#F2F0E9")


def _styles() -> dict[str, ParagraphStyle]:
    faces = body_faces()
    base = dict(fontName=faces.regular, alignment=TA_LEFT)
    return {
        "title": ParagraphStyle(
            "title", fontName=faces.bold, fontSize=17, leading=21, textColor=INK,
            spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", **base, fontSize=9, leading=13, textColor=MUTED,
            spaceAfter=14,
        ),
        "h2": ParagraphStyle(
            "h2", fontName=faces.bold, fontSize=12, leading=15, textColor=INK,
            spaceBefore=16, spaceAfter=5,
        ),
        "h3": ParagraphStyle(
            "h3", fontName=faces.bold, fontSize=9.5, leading=13, textColor=INK,
            spaceBefore=9, spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "body", **base, fontSize=8.6, leading=12.4, textColor=INK, spaceAfter=5
        ),
        "note": ParagraphStyle(
            "note", **base, fontSize=7.8, leading=11.2, textColor=MUTED, spaceAfter=4
        ),
        "cell": ParagraphStyle(
            "cell", **base, fontSize=7.8, leading=10.4, textColor=INK
        ),
        "cellhead": ParagraphStyle(
            "cellhead", fontName=faces.bold, fontSize=7.4, leading=10, textColor=MUTED
        ),
    }


def _table_style(head: bool = True) -> TableStyle:
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if head:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), BAND))
    return TableStyle(commands)


# --- formatting -------------------------------------------------------------
#
# Three functions, and between them they are every conversion this file makes
# from a wire value to a string. There is no fourth: money goes through
# `core.money.fmt_inr` and nothing else.


def _pct(rate: float) -> str:
    """A rate as the console renders it: one decimal place, from the wire value.

    0.8757763975155279 is 87.6% and is never rounded to 88% or padded to 87.58%.
    """
    return f"{rate * 100:.1f}%"


def _int(value: int) -> str:
    return f"{value:,}"


def _esc(value: object) -> str:
    """Everything drawn passes through here.

    Paragraph parses a small XML dialect, and a quarantined row is raw merchant
    data: an ampersand or an angle bracket in a bank narration must not become
    markup, and must not raise while rendering a report about the file it
    arrived in.
    """
    return escape("" if value is None else str(value))


# --- the public entry point -------------------------------------------------


def build_report(
    run: RunSummary,
    *,
    matches: Sequence[MatchGroup],
    exceptions: Sequence[ReconException],
    quarantine: Sequence[QuarantineRow] | None = None,
    uploads: Sequence[UploadRow] | None = None,
) -> bytes:
    """Render one run as a self-contained PDF.

    `run` supplies every figure the document quotes. `matches` and `exceptions`
    are the run's own rows -- they are counted and listed, never re-derived
    into a metric. `quarantine` and `uploads` are optional because a run over a
    generated dataset has neither; passing nothing produces a report that says
    the argument was not supplied rather than one that shows an empty table and
    lets a reader take it for a measured zero.
    """
    styles = _styles()
    story: list[Any] = []

    story += _cover(run, styles)
    story += _identity(run, uploads, styles)
    story += _metrics(run, styles)
    story += _tiers(run, matches, styles)
    story += _exceptions(exceptions, styles)
    story += _quarantine(quarantine, styles)
    story += _limits(run, exceptions, styles)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=PAGE,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title=f"Reconciliation report {run.run_id}",
        author="Tieout",
        subject="One reconciliation run, its figures and its limits",
    )
    doc.build(story, onLaterPages=_footer(run, styles), onFirstPage=_footer(run, styles))
    return buffer.getvalue()


def _footer(run: RunSummary, styles: dict[str, ParagraphStyle]):
    faces = body_faces()

    def draw(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont(faces.regular, 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, MARGIN * 0.55, f"Run {run.run_id}")
        canvas.drawRightString(
            PAGE[0] - MARGIN, MARGIN * 0.55, f"Page {document.page}"
        )
        canvas.restoreState()

    return draw


# --- 0. cover ---------------------------------------------------------------


def _cover(run: RunSummary, s: dict[str, ParagraphStyle]) -> list[Any]:
    return [
        Paragraph("Reconciliation run report", s["title"]),
        Paragraph(
            f"Run {_esc(run.run_id)} &middot; created {_esc(run.created_at.isoformat())} "
            f"&middot; state {_esc(run.state)}",
            s["subtitle"],
        ),
        Paragraph(
            "Every figure in this document is read off the run it describes. "
            "Nothing is recomputed here and nothing is defaulted: where a value "
            "is absent this report says so rather than printing a number in its "
            "place.",
            s["note"],
        ),
    ]


# --- 1. identity and inputs -------------------------------------------------


def _identity(
    run: RunSummary, uploads: Sequence[UploadRow] | None, s: dict[str, ParagraphStyle]
) -> list[Any]:
    from_uploads = run.seed == prose.UNSEEDED_RUN

    rows = [
        ("Run id", _esc(run.run_id)),
        ("Created at", _esc(run.created_at.isoformat())),
        ("State", _esc(run.state)),
        ("Records", _int(run.record_count)),
        ("Matches", _int(run.match_count)),
        ("Exceptions", _int(run.exception_count)),
        (
            "Seed",
            "none - this run read uploaded files"
            if from_uploads
            else _int(run.seed),
        ),
        ("Scored", "no" if run.metrics is None else "yes"),
    ]

    out: list[Any] = [
        Paragraph("1. Identity and inputs", s["h2"]),
        _kv_table(rows, s),
        Paragraph("Provenance", s["h3"]),
    ]

    if not from_uploads:
        out.append(Paragraph(prose.PROVENANCE_SEEDED, s["body"]))
        return out

    out.append(Paragraph(prose.PROVENANCE_UPLOADS, s["body"]))
    if not uploads:
        out.append(Paragraph(prose.PROVENANCE_UPLOADS_ABSENT, s["note"]))
        return out

    head = ["Upload id", "File", "Format", "State", "Records", "Quarantined", "SHA-256"]
    body = [
        [
            _esc(u.upload_id),
            _esc(u.filename),
            _esc(u.format_id),
            _esc(u.state),
            _int(u.record_count),
            _int(u.quarantine_count),
            _esc(u.content_sha256)[:16] + "...",
        ]
        for u in uploads
    ]
    out.append(
        _table(head, body, [72, 108, 78, 52, 42, 52, 107], s)
    )
    return out


def _kv_table(rows: Iterable[tuple[str, str]], s: dict[str, ParagraphStyle]) -> Table:
    data = [
        [Paragraph(_esc(label), s["cellhead"]), Paragraph(value, s["cell"])]
        for label, value in rows
    ]
    table = Table(data, colWidths=[152, CONTENT_WIDTH - 152], hAlign="LEFT")
    table.setStyle(_table_style(head=False))
    return table


def _table(
    head: Sequence[str],
    body: Sequence[Sequence[str]],
    widths: Sequence[float],
    s: dict[str, ParagraphStyle],
) -> Table:
    data = [[Paragraph(_esc(c), s["cellhead"]) for c in head]]
    data += [[Paragraph(c, s["cell"]) for c in row] for row in body]
    table = Table(data, colWidths=list(widths), hAlign="LEFT", repeatRows=1)
    table.setStyle(_table_style())
    return table


# --- 2. the four metrics ----------------------------------------------------


def _metrics(run: RunSummary, s: dict[str, ParagraphStyle]) -> list[Any]:
    out: list[Any] = [Paragraph("2. The four metrics", s["h2"])]

    metrics = run.metrics
    if metrics is None:
        return out + _unscored(run, s)

    out.append(Paragraph(prose.WHY_ALL_FOUR, s["body"]))
    out.append(Paragraph(prose.SUBJECT_NOTE, s["note"]))

    for derivation in prose.DERIVATIONS:
        out.append(
            KeepTogether(_derivation_block(derivation, metrics, s))
        )

    out.append(Paragraph(prose.RATE_ONLY_NOTE, s["note"]))
    out.append(Paragraph("Everything else this run reported", s["h3"]))
    out.append(_other_figures(metrics, s))
    return out


def _derivation_block(
    derivation: prose.Derivation, metrics: Metrics, s: dict[str, ParagraphStyle]
) -> list[Any]:
    value = getattr(metrics, derivation.key)
    figure = _pct(value)

    heading = f"{_esc(derivation.label)} &mdash; <b>{figure}</b>"
    denominator_note: str | None = None

    if derivation.key == "trap_capture_rate":
        total = metrics.total_traps
        if total is None:
            # `total_traps` was added after runs existed. An absent denominator
            # is not a denominator of zero, and this is the one figure whose
            # whole point is the number it divided by.
            denominator_note = prose.TRAP_DENOMINATOR_ABSENT
        elif total == 0:
            heading += " (no traps in this dataset)"
            denominator_note = prose.NO_TRAPS_CAVEAT
        else:
            # Half-up, matching `web/lib/metric-shape.ts`. The captured count is
            # the rate applied to the run's own denominator, not a second count.
            captured = int(value * total + 0.5)
            heading += f" ({_int(captured)} of {_int(total)} traps)"
            if total < prose.SMALL_TRAP_DENOMINATOR:
                denominator_note = prose.SMALL_TRAP_CAVEAT

    block: list[Any] = [
        Paragraph(heading, s["h3"]),
        Paragraph(_esc(derivation.definition), s["body"]),
    ]
    block.append(
        Paragraph(
            f"<b>Numerator</b> {_esc(derivation.numerator)}. "
            f"<b>Denominator</b> {_esc(derivation.denominator)}.",
            s["body"],
        )
    )
    block.append(
        Paragraph(f"<b>What it does not prove.</b> {_esc(derivation.caveat)}", s["note"])
    )
    if denominator_note is not None:
        block.append(Paragraph(f"<b>Its denominator.</b> {denominator_note}", s["note"]))
    return block


def _other_figures(metrics: Metrics, s: dict[str, ParagraphStyle]) -> Table:
    """The remaining wire fields, printed rather than summarised.

    Money is `int` paise and goes through `core.money.fmt_inr`. The one
    non-paise figure is `llm_cost_usd_per_100`, which is a dollar cost and is
    printed as one -- it is the single number in this product that is not
    integer paise, and it is labelled so it cannot be mistaken for one.
    """
    rows = [
        ("assisted_match_rate", _pct(metrics.assisted_match_rate)),
        ("exception_rate", _pct(metrics.exception_rate)),
        ("precision", _pct(metrics.precision)),
        ("llm_rejection_rate", _pct(metrics.llm_rejection_rate)),
        ("throughput_records_per_sec", f"{metrics.throughput_records_per_sec:,.1f}"),
        ("llm_tokens_per_100", _int(metrics.llm_tokens_per_100)),
        ("llm_cost_usd_per_100 (US dollars, not paise)", f"${metrics.llm_cost_usd_per_100:.4f}"),
        ("itc_substantiated_paise", _esc(fmt_inr(metrics.itc_substantiated_paise))),
        ("itc_at_risk_paise", _esc(fmt_inr(metrics.itc_at_risk_paise))),
        ("itc_variance_paise (signed, a net position)", _esc(fmt_inr(metrics.itc_variance_paise))),
    ]
    return _kv_table(rows, s)


def _unscored(run: RunSummary, s: dict[str, ParagraphStyle]) -> list[Any]:
    """`metrics is None` in three situations, and they are three statements."""
    if run.state == "failed":
        return [
            Paragraph(prose.FAILED_HEADING, s["h3"]),
            Paragraph(prose.FAILED_BODY, s["body"]),
        ]
    if run.state in ("pending", "running"):
        return [
            Paragraph(prose.RUNNING_HEADING, s["h3"]),
            Paragraph(prose.RUNNING_BODY, s["body"]),
        ]
    return [
        Paragraph(prose.UNSCORED_HEADING, s["h3"]),
        Paragraph(prose.UNSCORED_BODY, s["body"]),
        Paragraph("What this run does tell you", s["h3"]),
        Paragraph(prose.UNSCORED_WHAT_IT_DOES_TELL_YOU, s["body"]),
    ]


# --- 3. the tier ladder -----------------------------------------------------


def _tiers(
    run: RunSummary, matches: Sequence[MatchGroup], s: dict[str, ParagraphStyle]
) -> list[Any]:
    out: list[Any] = [
        Paragraph("3. The tier ladder", s["h2"]),
        Paragraph(prose.TIER_LADDER_NOTE, s["body"]),
    ]

    counts, counts_source = _tier_counts(run, matches)

    # NO SHARE COLUMN, DELIBERATELY. The console's identically-shaped table
    # carries one; this document does not, and the reason is the rule the file
    # is written around. A share is `count / total` -- the only arithmetic that
    # would appear anywhere in this report, and the only figure in it that is
    # not read straight off an object it was handed. Worse, on an UNSCORED run
    # it would print `0.0%` beside a rung that produced nothing, on a page whose
    # whole argument is that this run has no rates and that a zero would be a
    # claim it is not entitled to make. A reader who wants the share has the
    # count and the total in front of them.
    head = ["Tier", "Requires", "Matches", "Confidence stamped"]
    body: list[list[str]] = []
    for key in prose.TIER_KEYS:
        tier = prose.TIERS[key]
        count = "not recorded" if counts is None else _int(counts[key])
        body.append(
            [_esc(key), _esc(tier.rule), count, _confidence_figure(run, key)]
        )
    out.append(_table(head, body, [34, 244, 66, 96], s))
    out.append(Paragraph(counts_source, s["note"]))
    out.append(Paragraph(_sum_check(run, counts, matches), s["note"]))

    if run.tier_confidence is None:
        out.append(Paragraph(prose.CONFIDENCE_ABSENT, s["note"]))

    for key in prose.TIER_KEYS:
        out.append(KeepTogether(_tier_block(run, key, counts, s)))

    return out


def _tier_counts(
    run: RunSummary, matches: Sequence[MatchGroup]
) -> tuple[dict[str, int] | None, str]:
    """The count per rung, and a sentence naming where it came from.

    `Metrics.tier_counts` is the engine's own number and is used whenever it
    exists. When it does not -- an unscored run has matches and tiers but no
    metrics -- the rows this report was handed are grouped by the tier each
    `MatchGroup` carries, which is the same field the engine assigns and not a
    second derivation of it. Which of the two produced the column is printed,
    because two sources that could disagree must never be presented as one.
    """
    if run.metrics is not None:
        return dict(run.metrics.tier_counts), (
            "Counts are Metrics.tier_counts, the engine's own tier assignment "
            "for this run."
        )
    if not matches:
        return None, (
            "This run has no Metrics, so there is no tier_counts to read, and "
            "no match rows were supplied to group instead. The column is left "
            "unrecorded rather than filled with zeros."
        )
    counts = {key: 0 for key in prose.TIER_KEYS}
    for match in matches:
        counts[match.tier] = counts.get(match.tier, 0) + 1
    return counts, (
        "This run has no Metrics, so the counts are the supplied MatchGroup "
        "rows grouped by the tier each one carries - the engine's own "
        "assignment, counted, not a second derivation of it."
    )


def _sum_check(
    run: RunSummary, counts: dict[str, int] | None, matches: Sequence[MatchGroup]
) -> str:
    """Three fields that must agree, checked here rather than assumed."""
    supplied = len(matches)
    if counts is None:
        return (
            f"RunSummary.match_count reports {_int(run.match_count)}; "
            f"{_int(supplied)} match rows were supplied to this report."
        )
    total = sum(counts.values())
    if total == run.match_count:
        verdict = (
            f"The five rungs sum to {_int(total)}, which is exactly "
            "RunSummary.match_count. Two independent fields on the wire agree, "
            "so the breakdown accounts for every match this run produced and "
            "there is no residual rung."
        )
    else:
        verdict = (
            f"These counts do not add up: the five rungs sum to {_int(total)} "
            f"but RunSummary.match_count reports {_int(run.match_count)}. Two "
            "fields that must agree do not, so one of them is wrong - treat "
            "every share in this table as unreliable."
        )
    if supplied != total:
        # A separate observation, deliberately not folded into the verdict
        # above. How many match rows a caller chose to hand this renderer is a
        # fact about the call, not about whether the run's own fields agree.
        verdict += (
            f" Separately: {_int(supplied)} match rows were supplied to this "
            "report, which is a count of what this renderer was handed and not "
            "a figure the run reported."
        )
    return verdict


def _confidence_figure(run: RunSummary, key: str) -> str:
    if run.tier_confidence is None:
        return "not reported"
    tier = run.tier_confidence.get(key)
    if tier is None:
        return "not reported"
    if tier.confidence_conflict:
        return "&mdash;"
    if tier.confidence_observed is None:
        return "&mdash;"
    return f"{tier.confidence_observed:.2f}"


def _confidence_meaning(run: RunSummary, key: str) -> str:
    if run.tier_confidence is None:
        return prose.CONFIDENCE_ABSENT
    tier = run.tier_confidence.get(key)
    if tier is None:
        return prose.CONFIDENCE_ABSENT
    if tier.confidence_conflict:
        return prose.CONFIDENCE_CONFLICT
    if tier.confidence_observed is None:
        return prose.CONFIDENCE_NO_MATCHES
    figure = f"{tier.confidence_observed:.2f}"
    return prose.CONFIDENCE_MEANING.get(figure, prose.CONFIDENCE_GENERIC)


def _tier_block(
    run: RunSummary,
    key: str,
    counts: dict[str, int] | None,
    s: dict[str, ParagraphStyle],
) -> list[Any]:
    tier = prose.TIERS[key]
    figure = _confidence_figure(run, key)
    # The one amount in this section, and it goes through the one money
    # formatter like every other: it is 100 paise, not the string "Rs 1".
    tolerance = fmt_inr(prose.TOLERANCE_PAISE)
    block: list[Any] = [
        Paragraph(f"{_esc(key)} &mdash; {_esc(tier.rule)}", s["h3"]),
        _kv_table(
            [
                *(
                    (label, _esc(value).replace("{tolerance}", _esc(tolerance)))
                    for label, value in tier.requires
                ),
                ("Confidence stamped", figure),
            ],
            s,
        ),
        Paragraph(f"<b>What that confidence means.</b> {_esc(_confidence_meaning(run, key))}", s["note"]),
        Paragraph(f"<b>Falls through when.</b> {_esc(tier.declines)}", s["note"]),
    ]
    if counts is not None and counts.get(key) == 0:
        block.append(
            Paragraph(f"<b>Why this rung reads zero.</b> {_esc(tier.zero_means)}", s["note"])
        )
    return block


# --- 4. every exception -----------------------------------------------------


def _exceptions(
    exceptions: Sequence[ReconException], s: dict[str, ParagraphStyle]
) -> list[Any]:
    out: list[Any] = [
        Paragraph("4. Exceptions", s["h2"]),
        Paragraph(prose.EXCEPTIONS_NOTE, s["body"]),
    ]
    if not exceptions:
        out.append(Paragraph(prose.EXCEPTIONS_EMPTY, s["note"]))
        return out

    head = ["Exception", "Subject", "Reason code", "Amount", "Checker"]
    body = []
    for exc in exceptions:
        subject = (
            f"{prose.SUBJECT_TYPE_LABEL.get(exc.subject_type, exc.subject_type)} "
            f"{exc.subject_id}"
        )
        checker = exc.verifier_verdict
        if exc.failed_check:
            checker = f"{checker} ({exc.failed_check})"
        body.append(
            [
                _esc(exc.exception_id),
                _esc(subject),
                _esc(exc.reason_code.value),
                _esc(fmt_inr(exc.amount)),
                _esc(checker),
            ]
        )
    out.append(_table(head, body, [66, 108, 142, 70, 70], s))

    out.append(Paragraph("What each reason code means", s["h3"]))
    seen = sorted({exc.reason_code.value for exc in exceptions})
    out.append(
        _kv_table(
            [(code, _esc(prose.REASON_CODE_DESCRIPTION.get(code, ""))) for code in seen],
            s,
        )
    )

    hypotheses = [exc for exc in exceptions if exc.llm_hypothesis]
    if hypotheses:
        out.append(Paragraph("Proposals the checker refused", s["h3"]))
        out.append(
            Paragraph(
                "A rejected hypothesis survives onto its exception rather than "
                "being discarded, which is where a refusal becomes visible. The "
                "free-text reason is printed verbatim and is never parsed: the "
                "failing check is a typed field beside it.",
                s["note"],
            )
        )
        out.append(
            _table(
                ["Exception", "Failed check", "Proposal", "Reason given"],
                [
                    [
                        _esc(exc.exception_id),
                        _esc(exc.failed_check or "not recorded"),
                        _esc(exc.llm_hypothesis),
                        _esc(exc.verifier_reason or "not recorded"),
                    ]
                    for exc in hypotheses
                ],
                [72, 72, 150, 150],
                s,
            )
        )
    return out


# --- 5. quarantine ----------------------------------------------------------


def _quarantine(
    quarantine: Sequence[QuarantineRow] | None, s: dict[str, ParagraphStyle]
) -> list[Any]:
    out: list[Any] = [Paragraph("5. Quarantine", s["h2"])]
    if quarantine is None or len(quarantine) == 0:
        out.append(Paragraph(prose.QUARANTINE_ABSENT, s["note"]))
        return out

    out.append(Paragraph(prose.QUARANTINE_NOTE, s["body"]))
    out.append(
        _table(
            ["Row", "Reason", "Detail", "Raw"],
            [
                [
                    _int(row.row_number),
                    _esc(row.reason),
                    _esc(row.detail),
                    _esc(row.raw),
                ]
                for row in quarantine
            ],
            [38, 108, 140, 158],
            s,
        )
    )
    return out


# --- 6. standing limits -----------------------------------------------------


def _limits(
    run: RunSummary, exceptions: Sequence[ReconException], s: dict[str, ParagraphStyle]
) -> list[Any]:
    out: list[Any] = [
        PageBreak(),
        Paragraph(f"6. {prose.STANDING_LIMITS_HEADING}", s["h2"]),
        Paragraph(prose.STANDING_LIMITS_LEDE, s["body"]),
    ]

    metrics = run.metrics
    if metrics is None:
        out.append(Paragraph(prose.UNSCORED_HEADING, s["h3"]))
        out.append(Paragraph(prose.UNSCORED_BODY, s["body"]))
        out.append(Paragraph(prose.METHOD_POINTER, s["note"]))
        return out

    for title, text in prose.SCORED_LIMITS:
        out.append(Paragraph(_esc(title), s["h3"]))
        out.append(Paragraph(_esc(text), s["body"]))

    billed = metrics.llm_tokens_per_100 > 0 or metrics.llm_cost_usd_per_100 > 0
    if not billed:
        title, text = prose.LLM_UNBILLED_LIMIT
        out.append(Paragraph(_esc(title), s["h3"]))
        out.append(Paragraph(_esc(text), s["body"]))

    state = _analyst_state(metrics, exceptions)
    out.append(Paragraph("What this run says about the analyst", s["h3"]))
    out.append(Paragraph(_esc(prose.ANALYST_TITLE[state]), s["body"]))
    out.append(Paragraph(_esc(prose.ANALYST_EXPLANATION[state]), s["note"]))

    out.append(Spacer(1, 6))
    out.append(Paragraph(prose.METHOD_POINTER, s["note"]))
    return out


def _analyst_state(metrics: Metrics, exceptions: Sequence[ReconException]) -> str:
    """Which of four states this run's own fields put it in.

    The console reaches "still reading" here because it holds only the page of
    the exception list it has fetched. This report is handed the whole list, so
    the hypothesis census is complete and "no evidence" is decidable -- which
    is still the weaker claim, and deliberately not "no model ran": three
    different runs produce exactly these values and no typed field separates
    them.
    """
    if metrics.tier_counts.get("LLM", 0) > 0:
        return "accepted"
    proposed = sum(1 for exc in exceptions if exc.llm_hypothesis)
    if proposed > 0 or metrics.llm_rejection_rate > 0:
        return "all-rejected"
    if metrics.llm_tokens_per_100 > 0 or metrics.llm_cost_usd_per_100 > 0:
        return "called-silently"
    return "no-evidence"
