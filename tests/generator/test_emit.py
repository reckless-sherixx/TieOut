"""Task A.4 -- the pipeline and the wire format.

The plan's three tests, plus the ones that pin what the emitter is *for*:

* **byte-identity across runs**, which is a headline claim and therefore tested
  rather than assumed -- and tested on `truth.json` too, not only the CSVs;
* **the two requirements `fixtures/tiny/` structurally cannot carry** -- a clean
  single-payment-leg settlement (matcher tier T1) and a `split_settlement` --
  both asserted here because nothing else in the project covers them;
* **truth records the answer, not the damage** (CSV_SCHEMAS 5.1);
* **no accidental ambiguity**: two bank lines sharing an amount and a date are
  a trap, and an *unlabelled* trap would make `trap_capture_rate` a lie.
"""

import csv
import json
from datetime import date, datetime, timedelta

import pytest

from core.generator.emit import emit_dataset
from core.generator.pipeline import build_dataset
from core.models import BankLine, Order, PSPTransaction

SPEC_DEFECTS = {
    "many_to_one_batch",
    "cross_period_refund",
    "fee_plus_gst",
    "garbled_narration",
    "duplicate_psp_txn",
    "rounding_break",
    "chargeback_hold",
    "split_settlement",
    "missing_order_ref",
    "ambiguous_unresolvable",
    "obfuscated_settlement_ref",
}

#: The invoice-level defects (spec §6). They damage `psp_gst_invoice.csv` rather
#: than any record file, so they are not in `DEFECT_REGISTRY` and no `Batch`
#: carries their tag -- but they are injected defects and `truth.json` labels
#: them alongside the rest, which is why they belong in the total here. Their
#: own arithmetic is asserted in `test_itc_invoice.py`.
INVOICE_DEFECTS = {
    "missing_gst_invoice",
    "under_invoiced_gst",
}

ALL_DEFECTS = SPEC_DEFECTS | INVOICE_DEFECTS

OPENING_BALANCE = 10_000_000


# --- helpers ----------------------------------------------------------------


def _rows(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _n(value: str) -> int | None:
    return None if value == "" else int(value)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    """One seed-42 500-record run, shared by every read-only assertion."""
    out = tmp_path_factory.mktemp("seed42-500")
    emit_dataset(*build_dataset(seed=42, count=500), out_dir=out, seed=42)
    return {
        "dir": out,
        "orders": _rows(out / "orders.csv"),
        "psp": _rows(out / "psp.csv"),
        "bank": _rows(out / "bank.csv"),
        "truth": json.loads((out / "truth.json").read_text(encoding="utf-8")),
    }


# --- the plan's three -------------------------------------------------------


def test_same_seed_produces_byte_identical_output(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    emit_dataset(*build_dataset(seed=42, count=100), out_dir=a, seed=42)
    emit_dataset(*build_dataset(seed=42, count=100), out_dir=b, seed=42)
    for name in (
        "orders.csv", "psp.csv", "bank.csv", "psp_gst_invoice.csv", "truth.json"
    ):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_truth_json_labels_every_injected_defect(tmp_path):
    emit_dataset(*build_dataset(seed=42, count=100), out_dir=tmp_path, seed=42)
    truth = json.loads((tmp_path / "truth.json").read_text(encoding="utf-8"))
    types = {d["defect_type"] for d in truth["injected_defects"]}
    assert types == ALL_DEFECTS
    assert len(truth["unresolvable_ids"]) >= 2


def test_amounts_written_as_integer_paise(tmp_path):
    emit_dataset(*build_dataset(seed=42, count=50), out_dir=tmp_path, seed=42)
    assert "." not in (tmp_path / "bank.csv").read_text(encoding="utf-8").split("\n")[1]
    for name, columns in (
        ("orders.csv", ["gross_amount"]),
        ("psp.csv", ["amount"]),
        ("bank.csv", ["credit", "debit", "balance"]),
    ):
        for row in _rows(tmp_path / name):
            for column in columns:
                assert "." not in row[column], f"{name}.{column}"


# --- reproducibility --------------------------------------------------------


def test_a_different_seed_produces_a_different_dataset(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    emit_dataset(*build_dataset(seed=42, count=100), out_dir=a, seed=42)
    emit_dataset(*build_dataset(seed=43, count=100), out_dir=b, seed=43)
    assert (a / "psp.csv").read_bytes() != (b / "psp.csv").read_bytes()


def test_files_are_written_with_lf_line_endings(dataset):
    """`.gitattributes` pins fixtures to eol=lf; CRLF here breaks byte-identity."""
    for name in (
        "orders.csv", "psp.csv", "bank.csv", "psp_gst_invoice.csv", "truth.json"
    ):
        assert b"\r\n" not in (dataset["dir"] / name).read_bytes(), name


# --- the two the hand-written fixture cannot carry --------------------------


def test_at_least_one_clean_single_payment_leg_settlement(dataset):
    """Tier T1 is scored nowhere else in the project (spec 7).

    `fixtures/tiny/` has exactly two single-payment-leg settlements and both are
    spoken for: `setl_D4` carries the rounding break, `setl_M2` is half the
    ambiguity trap. So T1 correctly scores zero there, and a generated dataset
    where every one-payment settlement happens to carry a defect leaves a whole
    tier untested at every scale with nothing to notice.
    """
    truth = dataset["truth"]
    damaged = {i for d in truth["injected_defects"] for i in d["affected_ids"]}
    by_settlement: dict[str, list[dict]] = {}
    for row in dataset["psp"]:
        if row["settlement_id"]:
            by_settlement.setdefault(row["settlement_id"], []).append(row)
    line_of = {ln["line_id"]: ln for ln in dataset["bank"]}
    linkage_count: dict[str, int] = {}
    for link in truth["linkages"]:
        linkage_count[link["bank_line_id"]] = (
            linkage_count.get(link["bank_line_id"], 0) + 1
        )

    clean_t1 = []
    for link in truth["linkages"]:
        rows = by_settlement[link["settlement_id"]]
        payments = [r for r in rows if r["txn_type"] == "payment"]
        if len(payments) != 1:
            continue
        touched = {link["bank_line_id"], link["settlement_id"]}
        touched |= set(link["psp_txn_ids"]) | set(link["order_ids"])
        if touched & damaged:
            continue
        line = line_of[link["bank_line_id"]]
        assert sum(int(r["amount"]) for r in rows) == int(line["credit"]), (
            f"{link['settlement_id']}: a clean settlement must close exactly"
        )
        settled = date.fromisoformat(rows[0]["settled_at"])
        assert abs((date.fromisoformat(line["txn_date"]) - settled).days) <= 2
        assert linkage_count[link["bank_line_id"]] == 1, (
            "no other settlement may close that bank line"
        )
        assert link["bank_line_id"] not in truth["unresolvable_ids"]
        clean_t1.append(link["settlement_id"])

    assert clean_t1, "no clean single-payment-leg settlement: tier T1 is untested"


def test_at_least_one_split_settlement_whose_credits_sum_to_the_net(dataset):
    """Spec 10 #8 -- the one defect that breaks the identity everything leans on.

    Assert the sum, not merely the presence of the label: a split emitted with
    arithmetic that does not close is not the defect, it is a bug wearing its
    name.
    """
    truth = dataset["truth"]
    splits = [
        d for d in truth["injected_defects"] if d["defect_type"] == "split_settlement"
    ]
    assert splits, "split_settlement is exercised nowhere else in the project"

    credit_of = {ln["line_id"]: _n(ln["credit"]) for ln in dataset["bank"]}
    settlement_of = {
        link["bank_line_id"]: link["settlement_id"] for link in truth["linkages"]
    }
    for split in splits:
        assert split["resolvable"] is True
        ids = split["affected_ids"]
        assert len(ids) == 2, "one settlement, two bank lines"
        settlements = {settlement_of[i] for i in ids}
        assert len(settlements) == 1, "both lines must link to the same settlement"
        settlement_id = settlements.pop()
        net = sum(
            int(r["amount"])
            for r in dataset["psp"]
            if r["settlement_id"] == settlement_id
        )
        assert sum(credit_of[i] for i in ids) == net
        for line_id in ids:
            assert credit_of[line_id] > 0
            assert credit_of[line_id] != net, "neither line matches on its own"


def test_both_duplicate_variants_appear_at_scale(dataset):
    """The unsettled mirror AND the harsher in-settlement form."""
    truth = dataset["truth"]
    rows = {r["txn_id"]: r for r in dataset["psp"]}
    duplicates = [
        d for d in truth["injected_defects"] if d["defect_type"] == "duplicate_psp_txn"
    ]
    assert duplicates
    settled, unsettled = 0, 0
    for entry in duplicates:
        duplicate_id, canonical_id = entry["affected_ids"]
        duplicate, canonical = rows[duplicate_id], rows[canonical_id]
        # CSV_SCHEMAS 3.2.1 -- the dedup key, meaningful only with an order
        assert duplicate["order_id"] == canonical["order_id"] != ""
        assert duplicate["txn_type"] == canonical["txn_type"] == "payment"
        assert duplicate["captured_at"] == canonical["captured_at"]
        assert duplicate["amount"] == canonical["amount"]
        for link in truth["linkages"]:
            assert duplicate_id not in link["psp_txn_ids"], "only the canonical links"
        if duplicate["settlement_id"] == "":
            unsettled += 1
            assert duplicate["settled_at"] == ""
        else:
            settled += 1
    assert settled > 0, "the harsh in-settlement variant is missing"
    assert unsettled > 0, "the settled-plus-unsettled-mirror variant is missing"


# --- truth records the answer, not the damage -------------------------------


def test_every_linkage_order_id_resolves_to_a_real_order(dataset):
    """The dangling chargeback ref is not a real order, so it is not in truth."""
    real = {row["order_id"] for row in dataset["orders"]}
    for link in dataset["truth"]["linkages"]:
        missing = set(link["order_ids"]) - real
        assert not missing, f"{link['settlement_id']} names phantom orders {missing}"


def test_missing_order_ref_keeps_the_recovered_order_in_truth(dataset):
    """CSV_SCHEMAS 5.1 -- recovering the order IS what solving the defect means."""
    truth = dataset["truth"]
    rows = {r["txn_id"]: r for r in dataset["psp"]}
    blanked = [
        d for d in truth["injected_defects"] if d["defect_type"] == "missing_order_ref"
    ]
    assert blanked
    for entry in blanked:
        row = rows[entry["affected_ids"][0]]
        assert row["order_id"] == "", "the CSV must carry the damage"
        assert row["txn_type"] == "payment", "empty order_id is legal on a fee leg"
        link = next(
            lk
            for lk in truth["linkages"]
            if lk["settlement_id"] == row["settlement_id"]
        )
        payments = [
            r
            for r in dataset["psp"]
            if r["settlement_id"] == row["settlement_id"]
            and r["txn_type"] == "payment"
        ]
        assert len(link["order_ids"]) >= len(payments), (
            "truth lost the order the CSV was made to forget"
        )


def test_cross_period_refund_order_appears_in_both_linkages(dataset):
    """`ORD-004472` is in the linkage of both `setl_A1` and `setl_B2`."""
    truth = dataset["truth"]
    rows = {r["txn_id"]: r for r in dataset["psp"]}
    refunds = [
        d for d in truth["injected_defects"] if d["defect_type"] == "cross_period_refund"
    ]
    assert refunds
    for entry in refunds:
        refund_id, order_id = entry["affected_ids"]
        carrier_id = rows[refund_id]["settlement_id"]
        naming = {
            lk["settlement_id"]
            for lk in truth["linkages"]
            if order_id in lk["order_ids"]
        }
        assert carrier_id in naming, "the carrier's true set must include the order"
        assert len(naming) >= 2, "the paying settlement must still name it too"


def test_every_settled_row_appears_in_its_linkage_and_no_other(dataset):
    """CSV_SCHEMAS 5.1 -- every row carrying that settlement id, and no other,
    duplicates excepted."""
    truth = dataset["truth"]
    duplicates = {
        d["affected_ids"][0]
        for d in truth["injected_defects"]
        if d["defect_type"] == "duplicate_psp_txn"
    }
    expected: dict[str, set[str]] = {}
    for row in dataset["psp"]:
        if row["settlement_id"] and row["txn_id"] not in duplicates:
            expected.setdefault(row["settlement_id"], set()).add(row["txn_id"])
    for link in truth["linkages"]:
        assert set(link["psp_txn_ids"]) == expected[link["settlement_id"]]


def test_unresolvable_ids_are_exactly_the_trap_bank_lines(dataset):
    truth = dataset["truth"]
    from_defects = sorted(
        {
            i
            for d in truth["injected_defects"]
            if not d["resolvable"]
            for i in d["affected_ids"]
        }
    )
    assert truth["unresolvable_ids"] == from_defects
    line_ids = {ln["line_id"] for ln in dataset["bank"]}
    assert set(truth["unresolvable_ids"]) <= line_ids


def test_the_trap_strips_every_signal_in_the_emitted_csvs(dataset):
    """If any signal survives, the trap is broken and the honesty metric is noise."""
    truth = dataset["truth"]
    lines = {ln["line_id"]: ln for ln in dataset["bank"]}
    traps = [
        d
        for d in truth["injected_defects"]
        if d["defect_type"] == "ambiguous_unresolvable"
    ]
    assert traps
    for entry in traps:
        a, b = (lines[i] for i in entry["affected_ids"])
        assert a["credit"] == b["credit"]
        assert a["txn_date"] == b["txn_date"]
        assert a["narration"] == b["narration"]
        assert a["utr"] == b["utr"] == ""
        settlements = [
            lk["settlement_id"]
            for lk in truth["linkages"]
            if lk["bank_line_id"] in entry["affected_ids"]
        ]
        assert len(settlements) == 2
        for settlement_id in settlements:
            assert settlement_id not in a["narration"]
            rows = [
                r for r in dataset["psp"] if r["settlement_id"] == settlement_id
            ]
            assert sum(int(r["amount"]) for r in rows) == int(a["credit"])
            assert {r["settled_at"] for r in rows} == {
                next(
                    r2["settled_at"]
                    for r2 in dataset["psp"]
                    if r2["settlement_id"] == settlements[0]
                )
            }


def test_no_unlabelled_ambiguous_pair(dataset):
    """Two lines sharing an amount and a date are a trap. An unlabelled one
    would be scored as a matcher failure it had no way to avoid."""
    trap = set(dataset["truth"]["unresolvable_ids"])
    seen: dict[tuple[str, str], str] = {}
    for line in dataset["bank"]:
        if line["line_id"] in trap:
            continue
        key = (line["txn_date"], line["credit"])
        clash = seen.get(key)
        assert clash is None, f"{line['line_id']} is indistinguishable from {clash}"
        seen[key] = line["line_id"]


# --- wire format ------------------------------------------------------------


def test_record_count_is_the_orders_row_count(dataset):
    assert dataset["truth"]["record_count"] == len(dataset["orders"]) == 500
    assert dataset["truth"]["seed"] == 42


def test_csv_columns_match_the_frozen_schema(dataset):
    assert list(dataset["orders"][0]) == [
        "order_id",
        "order_date",
        "customer_ref",
        "gross_amount",
        "currency",
        "status",
    ]
    assert list(dataset["psp"][0]) == [
        "txn_id",
        "txn_type",
        "order_id",
        "captured_at",
        "amount",
        "settlement_id",
        "settled_at",
    ]
    assert list(dataset["bank"][0]) == [
        "line_id",
        "txn_date",
        "narration",
        "credit",
        "debit",
        "balance",
        "utr",
    ]


def test_absent_values_are_the_empty_string_not_the_word_none(dataset):
    blob = "\n".join(
        (dataset["dir"] / name).read_text(encoding="utf-8")
        for name in ("orders.csv", "psp.csv", "bank.csv")
    )
    for forbidden in ("None", "NULL", "nan"):
        assert forbidden not in blob


def test_rows_parse_back_into_the_frozen_models(dataset):
    for row in dataset["orders"]:
        Order(
            order_id=row["order_id"],
            order_date=row["order_date"],
            customer_ref=row["customer_ref"],
            gross_amount=int(row["gross_amount"]),
            currency=row["currency"],
            status=row["status"],
        )
    for row in dataset["psp"]:
        PSPTransaction(
            txn_id=row["txn_id"],
            txn_type=row["txn_type"],
            order_id=row["order_id"] or None,
            captured_at=row["captured_at"],
            amount=int(row["amount"]),
            settlement_id=row["settlement_id"] or None,
            settled_at=row["settled_at"] or None,
        )
    for row in dataset["bank"]:
        BankLine(
            line_id=row["line_id"],
            txn_date=row["txn_date"],
            narration=row["narration"],
            credit=_n(row["credit"]),
            debit=_n(row["debit"]),
            balance=int(row["balance"]),
            utr=row["utr"] or None,
        )


def test_dates_use_the_wire_formats(dataset):
    for row in dataset["orders"]:
        assert date.fromisoformat(row["order_date"]).isoformat() == row["order_date"]
    for row in dataset["psp"]:
        assert len(row["captured_at"]) == 19 and row["captured_at"][10] == "T"
        assert datetime.fromisoformat(row["captured_at"]).isoformat() == (
            row["captured_at"]
        )


def test_settlement_level_legs_carry_no_order_id(dataset):
    """CSV_SCHEMAS 3.2.1 -- breaking this hands Lane B false duplicates."""
    for row in dataset["psp"]:
        if row["txn_type"] in ("fee", "tax", "reserve"):
            assert row["order_id"] == "", row["txn_id"]
        if row["txn_type"] == "payment" and row["order_id"] == "":
            damaged = {
                i
                for d in dataset["truth"]["injected_defects"]
                if d["defect_type"] == "missing_order_ref"
                for i in d["affected_ids"]
            }
            assert row["txn_id"] in damaged, "an unlabelled blank order_id"


def test_ids_are_unique_across_the_dataset(dataset):
    for key, rows in (
        ("order_id", dataset["orders"]),
        ("txn_id", dataset["psp"]),
        ("line_id", dataset["bank"]),
    ):
        values = [row[key] for row in rows]
        assert len(values) == len(set(values)), key


def test_bank_statement_is_chronological_and_the_balance_chains(dataset):
    balance = OPENING_BALANCE
    previous = None
    for line in dataset["bank"]:
        txn_date = date.fromisoformat(line["txn_date"])
        assert previous is None or txn_date >= previous
        previous = txn_date
        balance += (_n(line["credit"]) or 0) - (_n(line["debit"]) or 0)
        assert int(line["balance"]) == balance, line["line_id"]


def test_every_settlement_closes_except_the_named_allowances(dataset):
    """Sigma legs == the bank credit, everywhere the truth file does not say
    otherwise. The allowance is derived from `injected_defects`, never from a
    loosened tolerance."""
    truth = dataset["truth"]
    in_settlement_duplicates: dict[str, int] = {}
    rows = {r["txn_id"]: r for r in dataset["psp"]}
    for entry in truth["injected_defects"]:
        if entry["defect_type"] != "duplicate_psp_txn":
            continue
        duplicate = rows[entry["affected_ids"][0]]
        if duplicate["settlement_id"]:
            in_settlement_duplicates[duplicate["settlement_id"]] = int(
                duplicate["amount"]
            )
    rounding = {
        lk["settlement_id"]
        for entry in truth["injected_defects"]
        if entry["defect_type"] == "rounding_break"
        for lk in truth["linkages"]
        if lk["bank_line_id"] in entry["affected_ids"]
    }
    split_lines = {
        i
        for entry in truth["injected_defects"]
        if entry["defect_type"] == "split_settlement"
        for i in entry["affected_ids"]
    }
    credit_of = {ln["line_id"]: _n(ln["credit"]) for ln in dataset["bank"]}

    naive: dict[str, int] = {}
    for row in dataset["psp"]:
        if row["settlement_id"]:
            naive[row["settlement_id"]] = naive.get(row["settlement_id"], 0) + int(
                row["amount"]
            )
    credited: dict[str, int] = {}
    for link in truth["linkages"]:
        credited[link["settlement_id"]] = credited.get(link["settlement_id"], 0) + (
            credit_of[link["bank_line_id"]]
        )

    for settlement_id, total in naive.items():
        expected = credited[settlement_id]
        expected += in_settlement_duplicates.get(settlement_id, 0)
        if settlement_id in rounding:
            expected += 50  # residual is net - credit, in that order
        assert total == expected, settlement_id
    assert split_lines, "no split settlement to exempt"


def test_settled_at_is_within_two_days_of_the_bank_line(dataset):
    """Every credit posts on its settlement's own cycle -- except the one
    defect whose entire mechanism is that it does not.

    `obfuscated_settlement_ref` posts days late on purpose. The two-day window
    is the only thing the deterministic tiers check that the LLM verifier does
    not, so it is the only gap an analyst layer can legitimately fill. Those
    lines are exempted here and then asserted from the other side -- outside
    the window, and always AFTER the settlement, because money arriving before
    it settled would fail `_causality` and be resolvable by nobody.
    """
    late = {
        line_id
        for d in dataset["truth"]["injected_defects"]
        if d["defect_type"] == "obfuscated_settlement_ref"
        for line_id in d["affected_ids"]
    }
    assert late, "no obfuscated_settlement_ref lines to exempt"

    lines = {ln["line_id"]: ln for ln in dataset["bank"]}
    settled_of = {}
    for row in dataset["psp"]:
        if row["settled_at"]:
            settled_of[row["settlement_id"]] = date.fromisoformat(row["settled_at"])
    for link in dataset["truth"]["linkages"]:
        settled = settled_of[link["settlement_id"]]
        txn_date = date.fromisoformat(lines[link["bank_line_id"]]["txn_date"])
        delta = (txn_date - settled).days
        if link["bank_line_id"] in late:
            assert delta > 2, link["bank_line_id"]
        else:
            assert abs(delta) <= 2, link["bank_line_id"]


def test_the_defect_mix_can_be_overridden(tmp_path):
    emit_dataset(
        *build_dataset(seed=42, count=100, defect_mix={"garbled_narration": 7}),
        out_dir=tmp_path,
        seed=42,
    )
    truth = json.loads((tmp_path / "truth.json").read_text(encoding="utf-8"))
    garbled = [
        d for d in truth["injected_defects"] if d["defect_type"] == "garbled_narration"
    ]
    assert len(garbled) == 7


def test_a_fifty_record_run_still_carries_all_ten_defects(tmp_path):
    """50 is the bar the plan names, and the smallest canonical dataset."""
    emit_dataset(*build_dataset(seed=42, count=50), out_dir=tmp_path, seed=42)
    truth = json.loads((tmp_path / "truth.json").read_text(encoding="utf-8"))
    assert {d["defect_type"] for d in truth["injected_defects"]} == ALL_DEFECTS
    assert truth["record_count"] == 50


def test_orders_precede_their_settlement(dataset):
    settled_of = {}
    for row in dataset["psp"]:
        if row["settled_at"]:
            settled_of[row["settlement_id"]] = date.fromisoformat(row["settled_at"])
    order_dates = {r["order_id"]: date.fromisoformat(r["order_date"]) for r in dataset["orders"]}
    for row in dataset["psp"]:
        if row["txn_type"] != "payment" or not row["order_id"]:
            continue
        settled = settled_of.get(row["settlement_id"])
        if settled is None:
            continue
        assert order_dates[row["order_id"]] <= settled + timedelta(days=0)
