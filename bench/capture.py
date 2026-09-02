"""Capture the full scorer output for a dataset, deterministically.

The scalability work (spec §8) is accepted on one condition: **every metric at
50, 500 and 5,000 records is byte-identical before and after**. That claim needs
an artefact, not a memory, so this script writes one and the baseline is
committed before a single line of the hot path moves.

What is captured is deliberately wider than the acceptance criterion:

* every field of `Metrics`, which IS the criterion;
* `scorer.explain()`, so a metric that stayed still while the subjects behind it
  moved cannot hide -- two compensating errors keep a rate constant;
* a SHA-256 over the whole run (matches, exceptions and the full audit trail),
  which pins every evidence string the tiers write.

`throughput_records_per_sec` is the one field that must NOT be compared: it is
wall clock, it does not reproduce, and the whole point of the exercise is to
move it. The run here is untimed, so the scorer reports 0.0 for it by its own
`elapsed_seconds=None` rule and the field is constant by construction rather
than by exclusion.

The three `itc_*` totals are left at their zero defaults, and deliberately so:
`api/jobs.py` is the only place in the repository that assembles an `ITCReport`
(`tests/api/test_itc_wiring.py`), and a benchmark script is not a good enough
reason to make it two. Nothing is lost. Those totals are a pure function of
which settlements the run matched and of an invoice this work does not touch,
so `tier_walk` and `run_sha256` -- which pin the matched settlements exactly --
already cover them.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.ingest.reader import read_bank, read_orders, read_psp  # noqa: E402
from core.matcher.engine import run_match  # noqa: E402
from scorer.score import explain, score  # noqa: E402


def capture(dataset: Path) -> dict:
    orders = read_orders(dataset / "orders.csv")
    psp_txns = read_psp(dataset / "psp.csv")
    bank_lines = read_bank(dataset / "bank.csv")

    result = run_match(orders, psp_txns, bank_lines, run_id="acceptance")

    metrics = score(result, dataset / "truth.json", elapsed_seconds=None)

    run_blob = json.dumps(
        {
            "matches": [m.model_dump(mode="json") for m in result.matches],
            "exceptions": [e.model_dump(mode="json") for e in result.exceptions],
            "audit": [a.model_dump(mode="json") for a in result.audit],
        },
        indent=2,
        sort_keys=True,
    )

    return {
        "dataset": dataset.name,
        "record_count": result.record_count,
        "match_count": len(result.matches),
        "exception_count": len(result.exceptions),
        "audit_entry_count": len(result.audit),
        "metrics": metrics.model_dump(mode="json"),
        "tier_walk": [
            f"{m.bank_line_id} {m.tier} {m.settlement_id} net={m.net}"
            for m in sorted(result.matches, key=lambda m: m.bank_line_id)
        ],
        "exception_walk": [
            f"{e.subject_id} {e.reason_code.value} amount={e.amount}"
            for e in sorted(result.exceptions, key=lambda e: (e.subject_id, e.reason_code.value))
        ],
        "explain": explain(result, dataset / "truth.json"),
        "run_sha256": hashlib.sha256(run_blob.encode("utf-8")).hexdigest(),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python bench/capture.py <dataset-dir> <out.json>")
        return 2
    dataset, out = Path(argv[1]), Path(argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(capture(dataset), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"captured {dataset} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
