"""`recon run` -- the matcher's sub-app.

`core/cli.py` registers this module's `app` under the name `run`; that file is
frozen and must not be edited, so everything this command needs lives here.

The options are declared on a callback with `invoke_without_command=True`
rather than on a sub-command, so the command line reads
`recon run --dataset fixtures/tiny --no-llm` and not `recon run run ...`.

This module deliberately knows nothing about scoring. Grading a run means
reading the answers, and nothing under `core/matcher/` may do that -- the
separation is the credibility argument for the numbers this command prints.
`--llm` is accepted and reported, but it does not run the analyst here. The
analyst layer is built and wired -- `core/llm/` proposes and verifies,
`core/llm/pipeline.py` holds the single accept loop, and `api/jobs.py` is its
one caller. Reaching it from this command would need a scorer, and grading a
run means reading the answers, so `--llm` points at the API path instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from core.ingest.reader import read_bank, read_orders, read_psp
from core.matcher.engine import MatchResult, run_match
from core.models import TIER_KEYS

app = typer.Typer(help="Run the deterministic matcher over a dataset directory.")


@app.callback(invoke_without_command=True)
def run(
    ctx: typer.Context,
    dataset: Path = typer.Option(
        ..., "--dataset", help="Directory holding orders.csv, psp.csv and bank.csv."
    ),
    llm: bool = typer.Option(
        False, "--llm/--no-llm", help="Reserved for the analyst layer (Lane C)."
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Write the run's matches, exceptions and audit as JSON."
    ),
    run_id: str = typer.Option("run-local", "--run-id", help="Label for the audit trail."),
) -> None:
    """Reconcile a dataset and report what matched and what did not."""
    if ctx.invoked_subcommand is not None:
        return

    orders = read_orders(dataset / "orders.csv")
    psp_txns = read_psp(dataset / "psp.csv")
    bank_lines = read_bank(dataset / "bank.csv")

    result = run_match(orders, psp_txns, bank_lines, run_id=run_id)

    typer.echo(f"dataset            {dataset}")
    typer.echo(f"records            {result.record_count}")
    typer.echo(f"matches            {len(result.matches)}")
    typer.echo(f"exceptions         {len(result.exceptions)}")
    breakdown = result.tier_breakdown
    # All five tiers, zeros included -- the same set of keys `Metrics.tier_counts`
    # promises. A tier printed as 0 is a result; a tier left off the line is a
    # silence the reader has to guess at.
    typer.echo(
        "tiers              "
        + "  ".join(f"{name}={breakdown[name]}" for name in TIER_KEYS)
    )
    if llm:
        typer.echo(
            "llm                the analyst layer runs through the API job runner, "
            "not this command -- POST /api/runs with use_llm=true"
        )

    for match in result.matches:
        typer.echo(
            f"  {match.bank_line_id}  {match.tier}  {match.settlement_id}  "
            f"net={match.net}  orders={len(match.order_ids)}"
        )
    for exception in result.exceptions:
        typer.echo(
            f"  {exception.subject_id}  {exception.reason_code.value}  "
            f"amount={exception.amount}"
        )

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_as_json(result), encoding="utf-8")
        typer.echo(f"wrote              {out}")


def _as_json(result: MatchResult) -> str:
    return json.dumps(
        {
            "run_id": result.run_id,
            "record_count": result.record_count,
            "matches": [m.model_dump(mode="json") for m in result.matches],
            "exceptions": [e.model_dump(mode="json") for e in result.exceptions],
            "audit": [a.model_dump(mode="json") for a in result.audit],
        },
        indent=2,
        sort_keys=True,
    )
