"""Time the matching stage the way METRICS.md §8 says it is timed.

5 warm-up repetitions discarded, then 25 timed repetitions in-process, over the
matching stage alone -- ingest and scoring excluded. `core/` reads no wall clock
anywhere, so the run is timed here, at the boundary that invokes it, exactly as
`api/jobs.py` does it.

Best, median and worst are all reported. Best-of is the headline because
scheduler noise only ever adds time; the other two are what make the spread
visible, and the spread on this number is real.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.ingest.reader import read_bank, read_orders, read_psp  # noqa: E402
from core.matcher.engine import run_match  # noqa: E402

WARMUPS = 5
REPETITIONS = 25


def measure(dataset: Path, *, warmups: int = WARMUPS, reps: int = REPETITIONS) -> dict:
    orders = read_orders(dataset / "orders.csv")
    psp_txns = read_psp(dataset / "psp.csv")
    bank_lines = read_bank(dataset / "bank.csv")

    for _ in range(warmups):
        run_match(orders, psp_txns, bank_lines)

    elapsed: list[float] = []
    for _ in range(reps):
        started = time.perf_counter()
        result = run_match(orders, psp_txns, bank_lines)
        elapsed.append(time.perf_counter() - started)

    records = result.record_count
    best, worst = min(elapsed), max(elapsed)
    median = statistics.median(elapsed)
    return {
        "dataset": dataset.name,
        "records": records,
        "warmups": warmups,
        "repetitions": reps,
        "best_seconds": best,
        "median_seconds": median,
        "worst_seconds": worst,
        "best_rec_per_s": records / best,
        "median_rec_per_s": records / median,
        "worst_rec_per_s": records / worst,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python bench/throughput.py <dataset-dir> [<dataset-dir> ...]")
        return 2
    out = [measure(Path(a)) for a in argv[1:]]
    for row in out:
        print(
            f"{row['records']:>6} records | "
            f"best {row['best_rec_per_s']:>10,.0f} rec/s ({row['best_seconds']*1000:.2f} ms) | "
            f"median {row['median_rec_per_s']:>10,.0f} rec/s | "
            f"worst {row['worst_rec_per_s']:>10,.0f} rec/s"
        )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
