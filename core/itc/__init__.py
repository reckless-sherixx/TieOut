"""Input tax credit recovery (spec §6).

The engine reports a match rate. A match rate is a claim about tidiness. GST on
the MDR a payment processor deducts in settlement is input tax credit, and it is
routinely forfeited when reconciliation is manual, because nothing links the
processor's monthly tax invoice to the settlements it covers.

This package is that link. It takes the run's own `MatchGroup`s -- nothing else
-- and the PSP's `psp_gst_invoice.csv`, and reports per calendar month what is
substantiated, what is at risk, and by how much the two disagree.

Two rules give the package its shape:

* **only matched settlements substantiate.** GST attached to a settlement the
  engine could not reconcile is not evidenced and counts as at risk. That
  coupling is deliberate: it is what makes the match rate a rupee figure rather
  than a percentage.
* **nothing here re-derives money.** The `tax` component comes off the
  `MatchGroup` the engine produced; the invoiced amount comes off the invoice.
  Neither is recomputed from the PSP rows, and `reconcile()` is not even given
  them.
"""
