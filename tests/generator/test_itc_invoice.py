"""`psp_gst_invoice.csv` and the two invoice-level defects (spec §6).

The engine already computes the GST deducted from every settlement. This file
is what lets that number be *claimed*: a PSP tax invoice per calendar month,
against which the run's own matched settlements are reconciled.

Three things are asserted here and nowhere else:

* **the invoice agrees with the legs it is an invoice for.** A clean period's
  `gst_amount` is the sum of that period's `tax` legs, to the paise. If the
  generator computed it as `pct_of(total_mdr, 1800)` instead, every period would
  carry a few paise of flooring residue, every period would read
  `over_invoiced`, and the report's headline number would be noise;
* **the period key is the bank line's month**, the same key
  `core/itc/reconcile.py` groups matched settlements by. A settlement that
  settles on the 31st and posts on the 1st belongs to the month it *posted* on
  both sides of the comparison or neither;
* **both injected defects survive to `truth.json`.** They are the only reason
  the report's at-risk figure can be graded rather than believed.
"""

import csv
import json

import pytest

from core.generator.emit import emit_dataset
from core.generator.pipeline import build_dataset
from core.money import pct_of

INVOICE_COLUMNS = [
    "invoice_no",
    "period",
    "taxable_value",
    "gst_amount",
    "gstin",
    "invoice_date",
]

GST_BPS = 1800

#: The 36 characters a GSTIN is built from, in the order that gives each its
#: numeric value for the check digit.
_GSTIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _gstin_check_digit(first14: str) -> str:
    total = 0
    for index, char in enumerate(first14):
        product = _GSTIN_ALPHABET.index(char) * (1 if index % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _GSTIN_ALPHABET[(36 - total % 36) % 36]


def _rows(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("itc-seed42-500")
    emit_dataset(*build_dataset(seed=42, count=500), out_dir=out, seed=42)
    return {
        "dir": out,
        "psp": _rows(out / "psp.csv"),
        "bank": _rows(out / "bank.csv"),
        "invoice": _rows(out / "psp_gst_invoice.csv"),
        "truth": json.loads((out / "truth.json").read_text(encoding="utf-8")),
    }


def _true_totals(dataset) -> dict[str, tuple[int, int]]:
    """`{period: (Sigma fee legs, Sigma tax legs)}`, keyed on the BANK LINE's
    month -- the same key the reconciler groups matched settlements by.

    Built here from the emitted CSVs rather than from the in-memory batches, so
    this is an independent check on the file and not a restatement of the code
    that wrote it.
    """
    line_of_settlement: dict[str, list[str]] = {}
    for link in dataset["truth"]["linkages"]:
        line_of_settlement.setdefault(link["settlement_id"], []).append(
            link["bank_line_id"]
        )
    date_of_line = {row["line_id"]: row["txn_date"] for row in dataset["bank"]}

    period_of: dict[str, str] = {}
    for settlement_id, line_ids in line_of_settlement.items():
        # A split settlement is paid across two lines; the earliest is the one
        # that dates the settlement, exactly as the generator assigns it.
        period_of[settlement_id] = min(date_of_line[i] for i in line_ids)[:7]

    totals: dict[str, list[int]] = {}
    for row in dataset["psp"]:
        settlement_id = row["settlement_id"]
        if not settlement_id or row["txn_type"] not in ("fee", "tax"):
            continue
        bucket = totals.setdefault(period_of[settlement_id], [0, 0])
        bucket[0 if row["txn_type"] == "fee" else 1] += -int(row["amount"])
    return {period: (fee, tax) for period, (fee, tax) in totals.items()}


def _itc_defects(truth, defect_type):
    return [d for d in truth["injected_defects"] if d["defect_type"] == defect_type]


# --- the file ---------------------------------------------------------------


def test_the_invoice_file_carries_the_spec_columns_in_order(dataset):
    with open(dataset["dir"] / "psp_gst_invoice.csv", encoding="utf-8") as handle:
        header = handle.readline().rstrip("\n")
    assert header.split(",") == INVOICE_COLUMNS


def test_one_row_per_calendar_month_the_dataset_covers(dataset):
    """...minus exactly the period `missing_gst_invoice` took away."""
    covered = set(_true_totals(dataset))
    missing = {
        period
        for entry in _itc_defects(dataset["truth"], "missing_gst_invoice")
        for period in entry["affected_ids"]
    }
    assert missing, "the dataset carries no missing_gst_invoice defect"
    assert {row["period"] for row in dataset["invoice"]} == covered - missing


def test_amounts_are_written_as_integer_paise(dataset):
    for row in dataset["invoice"]:
        assert "." not in row["taxable_value"], row["invoice_no"]
        assert "." not in row["gst_amount"], row["invoice_no"]
        assert int(row["taxable_value"]) > 0
        assert int(row["gst_amount"]) > 0


def test_the_invoice_number_and_date_are_derived_from_the_period(dataset):
    for row in dataset["invoice"]:
        assert row["invoice_no"].endswith(row["period"])
        # Dated the last day of the month it covers -- no clock is read.
        assert row["invoice_date"].startswith(row["period"])
        assert row["invoice_date"] > f"{row['period']}-27"


def test_every_row_carries_one_checksum_valid_gstin(dataset):
    gstins = {row["gstin"] for row in dataset["invoice"]}
    assert len(gstins) == 1, "one PSP issues the dataset, so one GSTIN"
    gstin = gstins.pop()
    assert len(gstin) == 15
    assert gstin[-1] == _gstin_check_digit(gstin[:14]), gstin


# --- the arithmetic ---------------------------------------------------------


def test_a_clean_period_invoices_exactly_the_tax_legs_it_covers(dataset):
    """Sigma of the period's `tax` legs, never `pct_of(total_mdr, 1800)`.

    Sigma-of-floors is below floor-of-Sigma by up to one paise per settlement.
    Computing the invoice the second way would put a few paise of unexplained
    variance in every period, and a report whose every row disagrees slightly
    is a report nobody can read the real defects out of.
    """
    truth_totals = _true_totals(dataset)
    under = {
        period
        for entry in _itc_defects(dataset["truth"], "under_invoiced_gst")
        for period in entry["affected_ids"]
    }
    clean = [row for row in dataset["invoice"] if row["period"] not in under]
    assert len(clean) >= 8, "not enough clean periods to make this assertion mean much"
    for row in clean:
        fee, tax = truth_totals[row["period"]]
        assert int(row["taxable_value"]) == fee, row["period"]
        assert int(row["gst_amount"]) == tax, row["period"]


def test_a_clean_period_is_not_the_flooring_of_its_own_taxable_value(dataset):
    """The distinction the previous test rests on is real on this dataset."""
    truth_totals = _true_totals(dataset)
    differs = [
        period
        for period, (fee, tax) in truth_totals.items()
        if tax != pct_of(fee, GST_BPS)
    ]
    assert differs, (
        "no period distinguishes Sigma-of-floors from floor-of-Sigma, so the "
        "invoice arithmetic is untested here"
    )


# --- the two injected defects ------------------------------------------------


def test_missing_gst_invoice_removes_a_period_that_has_settlements(dataset):
    entries = _itc_defects(dataset["truth"], "missing_gst_invoice")
    assert len(entries) == 1
    (period,) = entries[0]["affected_ids"]
    assert period in _true_totals(dataset), "the period must have settlements"
    assert period not in {row["period"] for row in dataset["invoice"]}


def test_under_invoiced_gst_states_less_than_the_period_actually_bore(dataset):
    entries = _itc_defects(dataset["truth"], "under_invoiced_gst")
    assert len(entries) == 1
    (period,) = entries[0]["affected_ids"]
    fee, tax = _true_totals(dataset)[period]
    (row,) = [r for r in dataset["invoice"] if r["period"] == period]
    assert int(row["gst_amount"]) < tax
    assert int(row["taxable_value"]) < fee


def test_the_under_invoiced_row_stays_internally_consistent(dataset):
    """Taxable value and GST are shorn by the same factor.

    An invoice whose GST alone was cut would be detectable by reading the row --
    `gst_amount` would stop being ~18% of `taxable_value` -- and the defect
    would be a formatting error rather than a reconciliation finding. Scaling
    both keeps the only route to it the one the engine takes: compare the
    invoice against the settlements it claims to cover.
    """
    entries = _itc_defects(dataset["truth"], "under_invoiced_gst")
    (period,) = entries[0]["affected_ids"]
    (row,) = [r for r in dataset["invoice"] if r["period"] == period]
    stated = int(row["gst_amount"]) / int(row["taxable_value"])
    fee, tax = _true_totals(dataset)[period]
    assert abs(stated - tax / fee) < 1e-4


def test_the_shortfall_is_large_enough_to_outrun_an_unmatched_settlement(dataset):
    """The defect has to be bigger than the matcher's own residue in that month.

    `computed_gst` counts MATCHED settlements only, so a period that also
    carries a settlement the engine could not close reports a computed sum
    below the period's true GST. If the under-invoicing were of the same order,
    the two would cancel and the defect would be undetectable -- for a reason
    that has nothing to do with the invoice. It is therefore injected as a
    substantial fraction of the period, not a token one.
    """
    entries = _itc_defects(dataset["truth"], "under_invoiced_gst")
    (period,) = entries[0]["affected_ids"]
    _, tax = _true_totals(dataset)[period]
    (row,) = [r for r in dataset["invoice"] if r["period"] == period]
    assert int(row["gst_amount"]) <= tax // 2


def test_the_two_invoice_defects_never_land_on_the_same_period(dataset):
    truth = dataset["truth"]
    missing = {
        p for e in _itc_defects(truth, "missing_gst_invoice") for p in e["affected_ids"]
    }
    under = {
        p for e in _itc_defects(truth, "under_invoiced_gst") for p in e["affected_ids"]
    }
    assert missing and under
    assert not (missing & under)


def test_the_invoice_defects_do_not_enter_unresolvable_ids(dataset):
    """`unresolvable_ids` drives `trap_capture_rate` and is keyed on bank lines.

    An invoice defect is not a subject the matcher declines -- it is fully
    derivable from the data -- so it is recorded `resolvable: true` and a period
    string never reaches a metric that counts bank lines.
    """
    truth = dataset["truth"]
    for defect_type in ("missing_gst_invoice", "under_invoiced_gst"):
        for entry in _itc_defects(truth, defect_type):
            assert entry["resolvable"] is True
    line_ids = {row["line_id"] for row in dataset["bank"]}
    assert set(truth["unresolvable_ids"]) <= line_ids


# --- reproducibility ---------------------------------------------------------


def test_the_invoice_is_byte_identical_across_runs_at_the_same_seed(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    emit_dataset(*build_dataset(seed=42, count=100), out_dir=a, seed=42)
    emit_dataset(*build_dataset(seed=42, count=100), out_dir=b, seed=42)
    assert (a / "psp_gst_invoice.csv").read_bytes() == (
        b / "psp_gst_invoice.csv"
    ).read_bytes()
    assert b"\r\n" not in (a / "psp_gst_invoice.csv").read_bytes()


def test_a_fifty_record_run_still_carries_both_invoice_defects(tmp_path):
    """50 records spans two calendar months, which is the minimum both need."""
    emit_dataset(*build_dataset(seed=42, count=50), out_dir=tmp_path, seed=42)
    truth = json.loads((tmp_path / "truth.json").read_text(encoding="utf-8"))
    types = {d["defect_type"] for d in truth["injected_defects"]}
    assert {"missing_gst_invoice", "under_invoiced_gst"} <= types


def test_a_dataset_too_small_to_span_two_months_injects_what_it_can(tmp_path):
    """One period cannot carry both defects, and the generator does not pretend.

    Every injector in this project returns nothing rather than damaging a target
    it has no right to; the invoice-level ones follow the same rule.
    """
    emit_dataset(*build_dataset(seed=42, count=5), out_dir=tmp_path, seed=42)
    truth = json.loads((tmp_path / "truth.json").read_text(encoding="utf-8"))
    types = [d["defect_type"] for d in truth["injected_defects"]]
    assert types.count("missing_gst_invoice") + types.count("under_invoiced_gst") <= 1
    rows = _rows(tmp_path / "psp_gst_invoice.csv")
    assert len(rows) <= 1
