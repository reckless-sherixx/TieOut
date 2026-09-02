"""`recon run` wiring.

`core/cli.py` is frozen and registers this sub-app under the name `run`, so the
options must live on a callback: with them on a sub-command the invocation
would be `recon run run --dataset ...`, which is not the documented command.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from core.cli import app

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "tiny"

runner = CliRunner()


def test_the_documented_invocation_works():
    result = runner.invoke(app, ["run", "--dataset", str(FIX), "--no-llm"])
    assert result.exit_code == 0, result.output
    assert "matches            4" in result.output
    assert "exceptions         3" in result.output


def test_the_tier_breakdown_is_printed():
    result = runner.invoke(app, ["run", "--dataset", str(FIX), "--no-llm"])
    assert "T0=2  T1=0  T2=1  T3=1" in result.output


def test_out_writes_a_serialisable_run(tmp_path):
    out = tmp_path / "nested" / "run.json"
    result = runner.invoke(
        app, ["run", "--dataset", str(FIX), "--no-llm", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["matches"]) == 4
    assert {e["reason_code"] for e in payload["exceptions"]} == {
        "AMBIGUOUS_MULTI_CANDIDATE",
        "DUPLICATE_PSP_TXN",
    }
    assert [a["sequence"] for a in payload["audit"]] == list(
        range(len(payload["audit"]))
    )


def test_a_missing_dataset_directory_fails_loudly():
    result = runner.invoke(app, ["run", "--dataset", "does/not/exist", "--no-llm"])
    assert result.exit_code != 0
