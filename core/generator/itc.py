"""The PSP's monthly GST tax invoice, and the two defects injected into it.

This is the input side of spec §6. The engine has always computed the GST
deducted from every settlement; what it has never had is the document that
*claims* it. `psp_gst_invoice.csv` is that document: one row per calendar month
the dataset covers, stating the total MDR the PSP charged and the GST it
charged on top of it.

Four decisions here are load-bearing, and each one is a way the report could
otherwise report noise as a finding.

**A period is keyed on the BANK LINE's month, not `settled_at`'s.**
`core/itc/reconcile.py` groups matched settlements by the month of their bank
line's `txn_date`, because a `MatchGroup` names a bank line and nothing else.
If the invoice were keyed on the settlement cycle instead, every settlement
that settles at a month end and posts in the next month would appear on one
side of the comparison and not the other, and the report would show a variance
in two adjacent periods every single month. Eight of the 166 settlements at
seed 42 / 500 records post in a different month from the one they settled in,
so this is not a hypothetical.

A `split_settlement` is paid across two bank lines; it is dated by the earlier
of them. It is one settlement and it belongs to one month.

**`gst_amount` is the sum of the period's `tax` legs -- never
`pct_of(taxable_value, GST_BPS)`.** `pct_of` floors, so a sum of floored
per-settlement taxes is below the floored tax on the summed fee by up to one
paise per settlement: about fifteen paise a month at this scale. Recomputing
would put that residue in every period, every period would read
`over_invoiced`, and the two deliberate defects would be indistinguishable
from arithmetic dust. The invoice states what was actually deducted.

**The two defects are invoice-level, so they are injected here** rather than in
`defects.py`. They damage no PSP row, no bank line and no order -- there is no
`Batch` for an injector to claim -- and adding them to `DEFECT_REGISTRY` would
put them in `DEFAULT_DEFECT_MIX`, in the `--defect-mix` surface and, fatally, in
`run_injections`'s draw order: every settlement in every existing dataset would
move. They are labelled in `truth.json` alongside the other eleven, keyed on the
period they damage, and recorded `resolvable: true` -- the exposure *is*
derivable from the data, and a period string must never reach `unresolvable_ids`,
which counts bank lines.

**The RNG is a fresh stream.** Drawing from the pipeline's own `SeededRng` would
shift every later draw and change `orders.csv`, `psp.csv` and `bank.csv` for
every seed. The invoice is derived from finished batches, so it takes its own
`SeededRng(seed)` and the three record files stay byte-identical to what they
were before this module existed.
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from datetime import date

from core.itc.invoice import INVOICE_COLUMNS, GstInvoice
from core.money import Money, pct_of

from .batches import Batch
from .defects import InjectionResult
from .rng import SeededRng

#: Re-exported so the emitter reads its column order from the module that also
#: builds the rows. `core/itc/invoice.py` is the single definition of both the
#: column tuple and the row shape: the generator writes the document and the
#: reconciler reads it, and a wire format with two definitions is a wire format
#: with two definitions.
__all__ = [
    "GST_BPS",
    "INVOICE_COLUMNS",
    "MISSING_INVOICE",
    "PSP_GSTIN",
    "UNDER_INVOICED",
    "UNDER_INVOICED_RETAINED_BPS",
    "GstInvoice",
    "build_invoice",
    "period_of",
]

#: The issuing PSP's GST identification number.
#:
#: Synthetic and deliberately so -- `AAACR0000R` is not a PAN any registrar has
#: issued -- but structurally valid: state code 29, a PAN-shaped block, entity
#: code 1, the mandatory `Z`, and a real check digit under the standard GSTIN
#: mod-36 algorithm. A reviewer who runs a GSTIN validator over the emitted file
#: gets a pass rather than a question, and nobody's real registration appears in
#: a generated fixture.
PSP_GSTIN = "29AAACR0000R1ZE"

#: 18%, charged on the MDR. The same rate `core/generator/batches.py` charges on
#: each settlement's fee; restated here only for the docstring's arithmetic.
GST_BPS = 1800

#: What the `under_invoiced_gst` row states, in basis points of the period's
#: true totals. Both `taxable_value` and `gst_amount` are shorn by this same
#: factor, so the row stays internally consistent -- GST is still ~18% of the
#: taxable value it names -- and the only route to the defect is the one the
#: engine takes: reconcile the invoice against the settlements it covers.
#:
#: 25% (a 75% shortfall) rather than something subtler, and the reason is the
#: same one `defects.py` gives for the ambiguity trap: a defect that is merely
#: *difficult* turns its metric into noise. `computed_gst` counts MATCHED
#: settlements only, so a period also carrying a settlement the engine could not
#: close already reports a computed sum below the period's truth. At seed 42 /
#: 500 records the worst month substantiates 62% of its own GST. An
#: under-invoicing of the same order would cancel against that residue and the
#: defect would be undetectable for a reason having nothing to do with the
#: invoice. The shortfall has to be unmistakably larger than the matcher's own
#: shortfall, and this is.
UNDER_INVOICED_RETAINED_BPS = 2500

#: A period needs at least this many settlements to be a candidate for either
#: defect. A month holding one settlement is the ragged edge of the generated
#: window -- 2026-12 holds exactly one at seed 42 / 500 -- and putting the
#: missing-invoice defect there would label a fifty-rupee finding as the
#: headline exposure.
_MIN_PERIOD_SETTLEMENTS = 2

MISSING_INVOICE = "missing_gst_invoice"
UNDER_INVOICED = "under_invoiced_gst"


def period_of(batch: Batch) -> str:
    """The calendar month a settlement's money reached the bank.

    The earliest of its bank lines, so a `split_settlement` -- one settlement,
    two lines, possibly a day apart across a month boundary -- is dated once.
    """
    return f"{min(line.txn_date for line in batch.all_bank_lines):%Y-%m}"


def _month_end(period: str) -> date:
    year, month = (int(part) for part in period.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1])


def _true_totals(batches: Sequence[Batch]) -> dict[str, tuple[Money, Money]]:
    """`{period: (Sigma fee legs, Sigma tax legs)}`, as positive paise.

    Read off the legs the generator itself built, which is the one place these
    numbers are allowed to come from: this module *is* the PSP, and an invoice
    states what was deducted. Nothing downstream re-derives them.

    A duplicated leg is excluded. `duplicate_psp_txn` copies a `payment` row,
    never a `fee` or `tax` one, so this exclusion changes no number today -- it
    is here because the day a duplicate fee leg is injected, an invoice that
    silently billed it twice would be a rupee error nobody could see.
    """
    totals: dict[str, list[Money]] = {}
    for batch in batches:
        bucket = totals.setdefault(period_of(batch), [0, 0])
        for txn in batch.psp_txns:
            if txn.settlement_id != batch.settlement_id:
                continue
            if txn.txn_id in batch.duplicate_txn_ids:
                continue
            if txn.txn_type == "fee":
                bucket[0] += -txn.amount
            elif txn.txn_type == "tax":
                bucket[1] += -txn.amount
    return {period: (fee, tax) for period, (fee, tax) in totals.items()}


def _settlement_counts(batches: Sequence[Batch]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for batch in batches:
        period = period_of(batch)
        counts[period] = counts.get(period, 0) + 1
    return counts


def build_invoice(
    batches: Sequence[Batch], *, seed: int
) -> tuple[list[GstInvoice], list[InjectionResult]]:
    """The month-by-month tax invoice, with both defects already in it.

    Returns `(rows, injections)`. The injections are `truth.json`'s labels for
    the two invoice-level defects and are appended to the ones the batch
    injectors produced; `affected_ids` is the period, which is the join key of
    the ITC report and the only id an invoice-level defect has.

    With fewer than two eligible periods the defects are injected as far as they
    go -- the missing invoice first, because it is the one a single-period
    dataset can still carry -- and no period is ever given both. That is the
    same rule every injector in `defects.py` follows: with no legitimate target,
    return nothing rather than damage something that was not eligible.
    """
    totals = _true_totals(batches)
    counts = _settlement_counts(batches)
    periods = sorted(totals)

    rng = SeededRng(seed)
    eligible = [p for p in periods if counts[p] >= _MIN_PERIOD_SETTLEMENTS]
    chosen = rng.sample(eligible, min(2, len(eligible)))

    missing = chosen[0] if chosen else None
    under = chosen[1] if len(chosen) > 1 else None

    rows: list[GstInvoice] = []
    for period in periods:
        if period == missing:
            continue
        fee, tax = totals[period]
        if period == under:
            fee = pct_of(fee, UNDER_INVOICED_RETAINED_BPS)
            tax = pct_of(tax, UNDER_INVOICED_RETAINED_BPS)
        rows.append(
            GstInvoice(
                invoice_no=f"PSPGST-{period}",
                period=period,
                taxable_value=fee,
                gst_amount=tax,
                gstin=PSP_GSTIN,
                invoice_date=_month_end(period),
            )
        )

    injections: list[InjectionResult] = []
    if missing is not None:
        injections.append(
            InjectionResult(
                defect_type=MISSING_INVOICE, affected_ids=[missing], resolvable=True
            )
        )
    if under is not None:
        injections.append(
            InjectionResult(
                defect_type=UNDER_INVOICED, affected_ids=[under], resolvable=True
            )
        )
    return rows, injections
