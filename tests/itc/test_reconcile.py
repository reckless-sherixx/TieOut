"""ITC reconciliation (spec §6): the match rate as a rupee figure.

Every test here exists to pin one of the four ways this number could quietly
become wrong:

* **the GST comes off the `MatchGroup`, never off the PSP rows.** A settlements
  listing built in this repository was wrong on 14 of 166 rows because it
  re-summed raw PSP rows while the matcher had already suppressed a duplicate
  leg. `reconcile()` is not given the PSP rows at all, and that is the fix;
* **an unmatched settlement moves money from substantiated to at risk.** That
  coupling is the whole point: it is what turns 93% into a rupee figure;
* **the T3 tolerance residual never reaches the GST figure.** `MatchGroup.net`
  is the bank credit by definition, so on a T3 match the components do not sum
  to it. The residual lives between the reconstruction and the credit; the tax
  component is the settlement's own `tax` legs and is untouched by it;
* **money stays `int` paise** through every total.
"""

from datetime import date

import pytest

from core.itc.invoice import GstInvoice, read_invoice
from core.itc.reconcile import ITCPeriod, ITCReport, reconcile
from core.matcher.engine import MatchResult
from core.models import BankLine, MatchGroup


def _line(line_id: str, txn_date: str, credit: int = 1_000) -> BankLine:
    return BankLine(
        line_id=line_id,
        txn_date=txn_date,
        narration="NEFT CR RAZORPAY",
        credit=credit,
        debit=None,
        balance=10_000_000,
        utr=None,
    )


def _match(
    line_id: str, settlement_id: str, tax: int, *, tier: str = "T0", net: int = 1_000
) -> MatchGroup:
    return MatchGroup(
        match_id=f"match-{line_id}",
        bank_line_id=line_id,
        settlement_id=settlement_id,
        psp_txn_ids=[],
        order_ids=[],
        gross=100_000,
        fees=tax * 5,
        tax=tax,
        refunds=0,
        holds=0,
        net=net,
        tier=tier,
        confidence=1.0,
        evidence=[],
    )


def _invoice(period: str, gst: int, taxable: int | None = None) -> GstInvoice:
    return GstInvoice(
        invoice_no=f"PSPGST-{period}",
        period=period,
        taxable_value=taxable if taxable is not None else gst * 5,
        gst_amount=gst,
        gstin="29AAACR0000R1ZE",
        invoice_date=date(int(period[:4]), int(period[5:]), 28),
    )


def _result(*matches: MatchGroup) -> MatchResult:
    return MatchResult(run_id="run-test", matches=list(matches), record_count=1)


def _by_period(report: ITCReport) -> dict[str, ITCPeriod]:
    return {period.period: period for period in report.periods}


# --- the clean case ----------------------------------------------------------


def test_a_fully_matched_period_with_a_matching_invoice_is_substantiated():
    report = reconcile(
        _result(_match("BL-1", "setl_1", 1_800), _match("BL-2", "setl_2", 2_200)),
        [_line("BL-1", "2026-07-03"), _line("BL-2", "2026-07-19")],
        [_invoice("2026-07", 4_000)],
    )

    (period,) = report.periods
    assert period.period == "2026-07"
    assert period.invoice_no == "PSPGST-2026-07"
    assert period.computed_gst == 4_000
    assert period.invoiced_gst == 4_000
    assert period.variance == 0
    assert period.status == "substantiated"
    assert report.substantiated_paise == 4_000
    assert report.at_risk_paise == 0
    assert report.variance_paise == 0


def test_periods_are_reported_in_calendar_order():
    report = reconcile(
        _result(_match("BL-2", "setl_2", 10), _match("BL-1", "setl_1", 10)),
        [_line("BL-1", "2026-03-02"), _line("BL-2", "2026-11-02")],
        [_invoice("2026-03", 10), _invoice("2026-11", 10)],
    )
    assert [p.period for p in report.periods] == ["2026-03", "2026-11"]


# --- the grouping key --------------------------------------------------------


def test_a_settlement_is_grouped_by_its_bank_lines_month_not_the_invoices():
    """The bank line dates the credit, and a credit is what ITC is claimed on.

    A settlement that settles at a month end and posts on the 1st belongs to the
    month it POSTED in. If the two sides of this comparison disagreed about
    that, every month boundary would show a variance in both adjacent periods.
    """
    report = reconcile(
        _result(_match("BL-1", "setl_1", 5_000)),
        [_line("BL-1", "2026-08-01")],
        [_invoice("2026-07", 0), _invoice("2026-08", 5_000)],
    )
    periods = _by_period(report)
    assert periods["2026-08"].computed_gst == 5_000
    assert periods["2026-07"].computed_gst == 0


def test_an_unknown_bank_line_on_a_match_is_an_error_not_a_dropped_period():
    """A match naming a line the statement does not have means the caller paired
    the wrong statement with the wrong run. Silently skipping it would under-
    report the substantiated figure with nothing to notice."""
    with pytest.raises(KeyError, match="BL-9"):
        reconcile(
            _result(_match("BL-9", "setl_1", 100)),
            [_line("BL-1", "2026-07-01")],
            [_invoice("2026-07", 100)],
        )


# --- the coupling: unmatched settlements are not substantiated ---------------


def test_a_settlement_the_engine_could_not_close_moves_gst_to_at_risk():
    """The invoice covers the whole month; only matched settlements substantiate
    it. The gap between the two IS the unmatched GST, and it is read off the
    invoice rather than recomputed from the rows the engine declined."""
    report = reconcile(
        _result(_match("BL-1", "setl_1", 3_000)),
        [_line("BL-1", "2026-07-03"), _line("BL-2", "2026-07-19")],
        [_invoice("2026-07", 5_000)],
    )

    (period,) = report.periods
    assert period.computed_gst == 3_000
    assert period.invoiced_gst == 5_000
    assert period.variance == -2_000
    assert period.status == "unmatched_settlements"
    assert report.substantiated_paise == 3_000
    assert report.at_risk_paise == 2_000


def test_an_invoice_above_the_computed_sum_with_every_line_matched_is_over_invoiced():
    """Same arithmetic, different finding. With no unmatched subject in the
    period the shortfall is not the engine's -- the PSP billed GST the
    settlements do not account for, and the two must not read the same."""
    report = reconcile(
        _result(_match("BL-1", "setl_1", 3_000)),
        [_line("BL-1", "2026-07-03")],
        [_invoice("2026-07", 5_000)],
    )
    (period,) = report.periods
    assert period.status == "over_invoiced"
    assert report.at_risk_paise == 2_000


def test_the_reconciler_is_never_given_the_psp_rows():
    """The structural version of "do not recompute what the engine computed".

    A settlements listing in this repository was wrong on 14 of 166 rows for
    exactly this reason: it re-summed raw PSP rows while the matcher had already
    suppressed a duplicate leg. `reconcile` cannot make that mistake because it
    is not handed the rows.
    """
    import inspect

    parameters = set(inspect.signature(reconcile).parameters)
    assert not parameters & {"psp_txns", "psp", "transactions", "orders"}


# --- the two injected defects ------------------------------------------------


def test_a_period_with_settlements_and_no_invoice_is_wholly_at_risk():
    """The `missing_gst_invoice` defect: the document exists and omits a month.

    Without an invoice there is no input tax credit to claim, however cleanly
    the settlements reconcile -- which is why the whole period is at risk rather
    than merely unmatched.
    """
    report = reconcile(
        _result(_match("BL-1", "setl_1", 4_242), _match("BL-2", "setl_2", 100)),
        [_line("BL-1", "2026-07-03"), _line("BL-2", "2026-08-03")],
        [_invoice("2026-08", 100)],
    )
    period = _by_period(report)["2026-07"]
    assert period.invoice_no is None
    assert period.invoiced_gst is None
    assert period.status == "no_invoice"
    assert period.computed_gst == 4_242
    assert report.substantiated_paise == 100  # August, which the invoice covers
    assert report.at_risk_paise == 4_242
    assert report.variance_paise == 4_242


def test_an_invoice_below_the_computed_sum_makes_the_variance_the_exposure():
    report = reconcile(
        _result(_match("BL-1", "setl_1", 8_000)),
        [_line("BL-1", "2026-07-03")],
        [_invoice("2026-07", 2_000)],
    )
    (period,) = report.periods
    assert period.status == "under_invoiced"
    assert period.variance == 6_000
    # Only what an invoice covers can be claimed; the rest is the exposure.
    assert report.substantiated_paise == 2_000
    assert report.at_risk_paise == 6_000


# --- the T3 residual ---------------------------------------------------------


def test_the_t3_tolerance_residual_does_not_reach_the_gst_figure():
    """`MatchGroup.net` IS the bank credit, so on a T3 match the components do
    not sum to it -- by design, within +/-100 paise. The GST figure reads the
    `tax` component, which is the settlement's own tax legs; it never reads
    `net`, and the residual is therefore not apportioned into it.

    Apportioning it would be wrong on the merits as well as unnecessary: GST is
    a stated amount on a tax invoice, not a derived share of a bank credit. A
    50-paise rounding break between a reconstruction and a credit is not
    evidence that 9 paise of GST was not charged.
    """
    exact = reconcile(
        _result(_match("BL-1", "setl_1", 1_800, tier="T0", net=100_000)),
        [_line("BL-1", "2026-07-03", credit=100_000)],
        [_invoice("2026-07", 1_800)],
    )
    # The same settlement, matched at T3 with the credit 100 paise off.
    broken = reconcile(
        _result(_match("BL-1", "setl_1", 1_800, tier="T3", net=99_900)),
        [_line("BL-1", "2026-07-03", credit=99_900)],
        [_invoice("2026-07", 1_800)],
    )

    assert broken.periods[0].computed_gst == exact.periods[0].computed_gst == 1_800
    assert broken.substantiated_paise == exact.substantiated_paise
    assert broken.at_risk_paise == exact.at_risk_paise == 0
    assert broken.periods[0].status == "substantiated"


# --- totals ------------------------------------------------------------------


def test_substantiated_plus_at_risk_accounts_for_every_period():
    """The invariant that makes the two headline figures checkable: per period
    they sum to the larger of what was computed and what was invoiced."""
    report = reconcile(
        _result(
            _match("BL-1", "setl_1", 3_000),
            _match("BL-3", "setl_3", 9_000),
            _match("BL-4", "setl_4", 500),
        ),
        [
            _line("BL-1", "2026-07-03"),
            _line("BL-2", "2026-07-19"),
            _line("BL-3", "2026-08-04"),
            _line("BL-4", "2026-09-04"),
        ],
        [_invoice("2026-07", 5_000), _invoice("2026-08", 1_000)],
    )
    expected = sum(
        max(p.computed_gst, p.invoiced_gst or 0) for p in report.periods
    )
    assert report.substantiated_paise + report.at_risk_paise == expected
    assert report.variance_paise == sum(p.variance for p in report.periods)


def test_every_money_field_is_an_integer_number_of_paise():
    report = reconcile(
        _result(_match("BL-1", "setl_1", 1_801)),
        [_line("BL-1", "2026-07-03")],
        [_invoice("2026-07", 999)],
    )
    for value in (
        report.substantiated_paise,
        report.at_risk_paise,
        report.variance_paise,
    ):
        assert type(value) is int
    for period in report.periods:
        assert type(period.computed_gst) is int
        assert type(period.variance) is int
        assert type(period.invoiced_gst) is int


def test_an_empty_run_reports_zeroes_rather_than_failing():
    report = reconcile(_result(), [], [])
    assert report.periods == []
    assert report.substantiated_paise == 0
    assert report.at_risk_paise == 0
    assert report.variance_paise == 0


def test_an_invoice_document_that_covers_no_period_is_a_finding_not_an_absence():
    """`[]` is a document claiming nothing over a month that has settlements.

    Distinct from a dataset with no invoice FILE, which never reaches this
    function at all -- see `load_invoice`, which returns `None` for that and
    `[]` for this.
    """
    report = reconcile(
        _result(_match("BL-1", "setl_1", 4_242)), [_line("BL-1", "2026-07-03")], []
    )
    (period,) = report.periods
    assert period.status == "no_invoice"
    assert report.at_risk_paise == 4_242


def test_a_period_that_has_bank_lines_but_matched_nothing_is_still_reported():
    """Completeness. A month the engine closed nothing in is a result, and a
    report that silently omits it cannot be reconciled against the statement."""
    report = reconcile(
        _result(), [_line("BL-1", "2026-07-03")], []
    )
    (period,) = report.periods
    assert period.period == "2026-07"
    assert period.computed_gst == 0
    assert period.status == "no_invoice"


def test_two_invoice_rows_for_one_period_are_rejected():
    """Which of the two is the invoice? There is no honest answer, so the file
    is refused rather than one row silently winning on iteration order."""
    with pytest.raises(ValueError, match="2026-07"):
        reconcile(
            _result(),
            [_line("BL-1", "2026-07-03")],
            [_invoice("2026-07", 10), _invoice("2026-07", 20)],
        )


# --- reading the file --------------------------------------------------------


def test_read_invoice_parses_the_emitted_file(tmp_path):
    from core.generator.emit import emit_dataset
    from core.generator.pipeline import build_dataset

    emit_dataset(*build_dataset(seed=42, count=50), out_dir=tmp_path, seed=42)
    invoices = read_invoice(tmp_path / "psp_gst_invoice.csv")

    assert invoices
    for invoice in invoices:
        assert type(invoice.taxable_value) is int
        assert type(invoice.gst_amount) is int
        assert isinstance(invoice.invoice_date, date)
        assert invoice.period == f"{invoice.invoice_date:%Y-%m}"


def test_read_invoice_refuses_a_decimal_amount(tmp_path):
    """The same rule the other three readers enforce: a `.` in an amount column
    is a hard error naming the row, never a silent `float()`."""
    path = tmp_path / "psp_gst_invoice.csv"
    path.write_text(
        "invoice_no,period,taxable_value,gst_amount,gstin,invoice_date\n"
        "PSPGST-2026-07,2026-07,1000.50,180,29AAACR0000R1ZE,2026-07-31\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="taxable_value"):
        read_invoice(path)


def test_read_invoice_refuses_a_file_with_the_wrong_header(tmp_path):
    path = tmp_path / "psp_gst_invoice.csv"
    path.write_text("period,gst_amount\n2026-07,180\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_invoice(path)


def test_load_invoice_returns_none_when_the_dataset_has_no_invoice_at_all(tmp_path):
    """`None`, not `[]`, and the distinction is the point.

    A dataset generated before this capability carries no invoice; nothing can
    be said about input tax credit either way and the caller reports zero. An
    invoice that exists and covers no period is a different fact -- a document
    claiming nothing -- and puts the run's whole computed GST at risk. Returning
    `[]` for both would turn "the operator never supplied the invoice" into a
    claim about their tax position.
    """
    from core.itc.invoice import load_invoice

    assert load_invoice(tmp_path) is None


def test_load_invoice_returns_rows_when_the_file_is_there(tmp_path):
    from core.generator.emit import emit_dataset
    from core.generator.pipeline import build_dataset
    from core.itc.invoice import load_invoice

    emit_dataset(*build_dataset(seed=42, count=50), out_dir=tmp_path, seed=42)
    rows = load_invoice(tmp_path)
    assert rows is not None and len(rows) >= 1


def test_load_invoice_still_raises_on_a_malformed_file(tmp_path):
    """Absent and broken are different facts and must not read the same."""
    from core.itc.invoice import load_invoice

    (tmp_path / "psp_gst_invoice.csv").write_text("nope\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_invoice(tmp_path)
