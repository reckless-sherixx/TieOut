"""The ITC report: what the run substantiates, and what it leaves at risk.

`reconcile(result, bank_lines, invoices)` groups the run's matched settlements
by the calendar month of their bank line's `txn_date`, sums the `tax` component
of each, and compares that total to the month's invoice.

Five decisions, in the order they cost most if they are wrong.

**1. The `tax` comes off the `MatchGroup`, and `reconcile` is not given the PSP
rows.** A settlements listing built in this repository was wrong on 14 of 166
rows because it re-summed raw PSP rows while the matcher had already suppressed
a duplicate leg. `MatchGroup.tax` is `core.matcher.batch.reconstruct` over the
settlement's *active* legs -- the engine's own number, already tested, already
free of the suppressed duplicate. This module cannot make that mistake, because
the rows are not in its signature.

**2. Only matched settlements substantiate, and the shortfall is read off the
invoice.** GST attached to a settlement the engine could not close is not
evidenced. Its magnitude is `invoiced - computed`: the invoice covers the whole
month, the matched settlements cover part of it, and the gap is exactly the
unmatched part. Nothing is recomputed from the rows the engine declined -- which
is both the cheap way and the honest way, since a figure recomputed from those
rows would be a figure the run had not reconciled.

That coupling is the point of the capability. A month whose match rate falls
moves rupees from `substantiated_paise` to `at_risk_paise`, and the headline
stops being a percentage.

**3. The T3 tolerance residual is not apportioned into the GST.**
`MatchGroup.net` *is* the bank line credit, by the definition every tier obeys,
so on a T3 match `gross - fees - tax - refunds - holds` sits up to
`TOLERANCE_PAISE` away from `net`. The components do not sum to `net`, by
design.

This module reads `tax` and never reads `net`, so the residual cannot reach the
figure -- and that is the correct treatment, not merely the convenient one. GST
is a stated amount: the settlement's own `tax` leg, the same number the PSP put
on its invoice and the only number an input tax credit can be claimed against. A
50-paise break between a reconstruction and a bank credit is evidence about the
credit, not evidence that 9 paise of GST went uncharged. Spreading it pro rata
across the components would move a rupee figure by an amount that has no
document behind it, which is precisely what a reviewer checking the claim would
find and object to. The residual stays where the engine put it: in that match's
`evidence`.

**4. The period is the bank line's month.** A `MatchGroup` names a bank line, so
that is the only month available; the invoice is keyed the same way
(`docs/CSV_SCHEMAS.md` §4.5). A settlement that settles on the 31st and posts on
the 1st therefore falls in the same period on both sides of the comparison.

**5. `unmatched_settlements` and `over_invoiced` are the same arithmetic and
different findings.** Both are `computed < invoiced`. When the period holds a
bank line the run did not close, the shortfall is the engine's and the report
says so; when every line closed, the PSP has invoiced GST the settlements do not
account for, which is a question for the PSP. Collapsing the two would hide
which one happened.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from core.itc.invoice import GstInvoice
from core.matcher.engine import MatchResult
from core.models import BankLine
from core.money import Money

PeriodStatus = Literal[
    "substantiated",
    "under_invoiced",
    "over_invoiced",
    "no_invoice",
    "unmatched_settlements",
]


class ITCPeriod(BaseModel):
    """One calendar month, reconciled."""

    model_config = ConfigDict(strict=True)

    period: str  # "2026-07"
    #: `None` when no invoice covers the period -- the `missing_gst_invoice`
    #: defect, and the shape a real forfeited credit takes.
    invoice_no: str | None
    computed_gst: Money  # Sigma `tax` over the period's MATCHED settlements
    invoiced_gst: Money | None
    #: `computed - invoiced`, signed; 0 when they agree. An absent invoice
    #: counts as zero invoiced, so the whole computed sum is the disagreement.
    variance: Money
    status: PeriodStatus


class ITCReport(BaseModel):
    """Every period of one run, and the three figures that summarise them.

    The invariant that makes the two headline numbers checkable, per period and
    therefore in total:

        substantiated + at_risk == max(computed_gst, invoiced_gst or 0)

    -- every rupee of GST is either evidenced by a matched settlement AND
    covered by an invoice, or it is not.
    """

    model_config = ConfigDict(strict=True)

    periods: list[ITCPeriod]
    #: Claimable and evidenced: computed from matched settlements AND covered by
    #: an invoice. `min(computed, invoiced)`, which is what both documents agree
    #: on.
    substantiated_paise: Money
    #: Everything else: GST computed but not invoiced, and GST invoiced but not
    #: substantiated by a settlement the engine could close.
    at_risk_paise: Money
    #: Signed total disagreement. Unlike `at_risk_paise` it does not take an
    #: absolute value, so an over-invoiced month and an under-invoiced month of
    #: the same size cancel -- which is the right behaviour for a *net* position
    #: and the wrong one for an exposure, hence two fields.
    variance_paise: Money


def _period_of(day) -> str:
    return f"{day:%Y-%m}"


def reconcile(
    result: MatchResult,
    bank_lines: Sequence[BankLine],
    invoices: Sequence[GstInvoice],
) -> ITCReport:
    """Reconcile one run's matched settlements against the PSP's tax invoices.

    `bank_lines` is the run's own statement: a `MatchGroup` names a bank line
    and the bank line carries the date, so the two are needed together to group
    by month. The PSP rows are deliberately not a parameter -- see the module
    docstring, rule 1.

    Raises `KeyError` if a match names a bank line the statement does not
    contain (the caller has paired the wrong statement with the wrong run) and
    `ValueError` if two invoice rows claim the same period.
    """
    date_of = {line.line_id: line.txn_date for line in bank_lines}

    computed: dict[str, Money] = {}
    matched_line_ids: set[str] = set()
    for match in result.matches:
        if match.bank_line_id not in date_of:
            raise KeyError(
                f"match {match.match_id} names bank line {match.bank_line_id}, "
                f"which is not in the statement handed to reconcile() -- the run "
                f"and the statement do not belong to each other"
            )
        matched_line_ids.add(match.bank_line_id)
        period = _period_of(date_of[match.bank_line_id])
        computed[period] = computed.get(period, 0) + match.tax

    #: Periods holding a credit line the run did not close. This is the only
    #: thing separating `unmatched_settlements` from `over_invoiced`, and it is
    #: derived from the statement rather than from any amount.
    unmatched_periods = {
        _period_of(line.txn_date)
        for line in bank_lines
        if line.credit is not None and line.line_id not in matched_line_ids
    }

    by_period: dict[str, GstInvoice] = {}
    for invoice in invoices:
        if invoice.period in by_period:
            raise ValueError(
                f"two invoice rows claim period {invoice.period} "
                f"({by_period[invoice.period].invoice_no} and "
                f"{invoice.invoice_no}); which one covers it is undetermined"
            )
        by_period[invoice.period] = invoice

    # Every period the statement covers, plus every period an invoice claims. A
    # month the run closed nothing in is a result, not an omission, and a report
    # that dropped it could not be reconciled against the statement by hand.
    universe = (
        {_period_of(line.txn_date) for line in bank_lines}
        | set(by_period)
        | set(computed)
    )

    periods: list[ITCPeriod] = []
    substantiated = at_risk = variance_total = 0
    for period in sorted(universe):
        invoice = by_period.get(period)
        computed_gst = computed.get(period, 0)
        invoiced_gst = invoice.gst_amount if invoice is not None else None
        stated = invoiced_gst if invoiced_gst is not None else 0
        variance = computed_gst - stated

        if invoice is None:
            status: PeriodStatus = "no_invoice"
        elif variance > 0:
            status = "under_invoiced"
        elif variance < 0:
            status = (
                "unmatched_settlements"
                if period in unmatched_periods
                else "over_invoiced"
            )
        else:
            status = "substantiated"

        periods.append(
            ITCPeriod(
                period=period,
                invoice_no=invoice.invoice_no if invoice is not None else None,
                computed_gst=computed_gst,
                invoiced_gst=invoiced_gst,
                variance=variance,
                status=status,
            )
        )
        substantiated += min(computed_gst, stated)
        at_risk += abs(variance)
        variance_total += variance

    return ITCReport(
        periods=periods,
        substantiated_paise=substantiated,
        at_risk_paise=at_risk,
        variance_paise=variance_total,
    )
