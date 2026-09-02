"""Where the ITC report is assembled, and where it deliberately is not.

Spec §6's wiring paragraph is a division of responsibility, and it is the thing
most likely to be quietly undone by a later change:

* `scorer.score()` grades matching against truth. It does **not** compute the
  ITC report and must not grow a second responsibility -- so it receives three
  already-computed totals as keyword arguments, exactly as `llm_cost_usd` and
  `llm_tokens` already arrive;
* `core/itc/` produces the `ITCReport`;
* `api/jobs.py` is the single place all three are assembled.

The defaults matter as much as the wiring: a dataset with no
`psp_gst_invoice.csv` -- which is any dataset generated before this capability,
and any dataset an operator supplies without the PSP's invoice -- must still
score normally, with the ITC figures at zero rather than absent. The committed
fixtures no longer serve as that witness: `fixtures/seed42-50` and
`fixtures/seed42-500` both carry an invoice now, so
`test_a_dataset_with_no_invoice_file_still_scores` below deletes the file from a
freshly generated copy rather than leaning on a fixture that happens to lack it.

Deliberately **no `tests/api/__init__.py`** (see this directory's conftest), so
the app fixture and the two helpers below are local rather than imported from
`test_routes.py`.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client on an isolated SQLite file and dataset directory."""
    monkeypatch.setenv("RECON_DB_PATH", str(tmp_path / "recon.db"))
    monkeypatch.setenv("RECON_DATASETS_DIR", str(tmp_path / "datasets"))
    from api.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _generate(client, record_count: int) -> str:
    response = client.post(
        "/api/datasets/generate", json={"seed": 42, "record_count": record_count}
    )
    assert response.status_code == 200, response.text
    return response.json()["dataset_id"]


def _run(client, dataset_id: str) -> str:
    created = client.post("/api/runs", json={"dataset_id": dataset_id, "use_llm": False})
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    for _ in range(200):
        state = client.get(f"/api/runs/{run_id}/status").json()["state"]
        if state in ("completed", "failed"):
            break
    assert state == "completed", client.get(f"/api/runs/{run_id}/status").json()
    return run_id


def _metrics(client, run_id: str) -> dict:
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["metrics"] is not None
    return body["metrics"]


# --- the assembled figures ----------------------------------------------------


def test_a_generated_run_reports_itc_figures_in_paise(client):
    run_id = _run(client, _generate(client, 500))
    metrics = _metrics(client, run_id)

    for field in (
        "itc_substantiated_paise",
        "itc_at_risk_paise",
        "itc_variance_paise",
    ):
        assert field in metrics, field
        value = metrics[field]
        assert isinstance(value, int) and not isinstance(value, bool), (
            f"{field} must be integer paise, never a float"
        )

    assert metrics["itc_substantiated_paise"] > 0, "the run substantiated nothing"
    assert metrics["itc_at_risk_paise"] > 0, (
        "a 500-record run leaves settlements unmatched and drops one period's "
        "invoice, so something must be at risk"
    )


def test_the_api_figures_are_the_ones_core_itc_computes(client):
    """The wire carries the report's own totals, not a second derivation."""
    from core.ingest.reader import read_bank, read_orders, read_psp
    from core.itc.invoice import load_invoice
    from core.itc.reconcile import reconcile
    from core.matcher.engine import run_match

    from api import settings

    dataset_id = _generate(client, 500)
    run_id = _run(client, dataset_id)
    directory = settings.datasets_dir() / dataset_id

    bank_lines = read_bank(directory / "bank.csv")
    result = run_match(
        read_orders(directory / "orders.csv"),
        read_psp(directory / "psp.csv"),
        bank_lines,
    )
    report = reconcile(result, bank_lines, load_invoice(directory))

    metrics = _metrics(client, run_id)
    assert metrics["itc_substantiated_paise"] == report.substantiated_paise
    assert metrics["itc_at_risk_paise"] == report.at_risk_paise
    assert metrics["itc_variance_paise"] == report.variance_paise


def test_a_dataset_with_no_invoice_file_still_scores(client):
    from api import settings

    dataset_id = _generate(client, 50)
    (settings.datasets_dir() / dataset_id / "psp_gst_invoice.csv").unlink()

    metrics = _metrics(client, _run(client, dataset_id))

    assert metrics["itc_substantiated_paise"] == 0
    assert metrics["itc_at_risk_paise"] == 0
    assert metrics["itc_variance_paise"] == 0
    # The rest of the run is untouched by the absence.
    assert metrics["auto_match_rate"] > 0.5
    assert metrics["false_match_rate"] == 0.0


def test_the_invoice_file_is_not_required_for_a_dataset_to_resolve():
    """`resolve_dataset` names the three files a run cannot start without.

    Adding the invoice to that list would turn every dataset generated before
    this capability into a 404 rather than a run with zero ITC.
    """
    from api.jobs import _REQUIRED_FILES

    assert "psp_gst_invoice.csv" not in _REQUIRED_FILES


# --- the division of responsibility ------------------------------------------


def _imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append(node.module or "")
            found += [alias.name for alias in node.names]
    return found


def test_the_scorer_does_not_compute_the_itc_report():
    """`score()` grades matching against truth. That is its whole job.

    A scorer that reached for `core/itc/` would have two responsibilities and
    two reasons to change, and the ITC figures would then exist in two places --
    `score()`'s and the job runner's -- with nothing keeping them equal.
    """
    paths = list((REPO_ROOT / "scorer").rglob("*.py"))
    assert paths, "no scorer sources found; this test would pass vacuously"
    for path in paths:
        for module in _imports(path):
            assert "itc" not in module, f"{path} imports {module!r}"


def test_score_takes_the_itc_totals_the_same_way_it_takes_the_llm_ones():
    import inspect

    from scorer.score import score

    parameters = inspect.signature(score).parameters
    for name in (
        "itc_substantiated_paise",
        "itc_at_risk_paise",
        "itc_variance_paise",
    ):
        assert name in parameters, name
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default == 0
        assert type(parameters[name].default) is int


def test_jobs_is_the_only_place_the_report_is_assembled():
    """One caller. A second place that built an `ITCReport` for a run would be a
    second place the three totals could be computed differently."""
    callers = sorted(
        path.name
        for path in REPO_ROOT.rglob("*.py")
        if ".venv" not in path.parts
        and "tests" not in path.parts
        and not path.is_relative_to(REPO_ROOT / "core" / "itc")
        and any("itc.reconcile" in module for module in _imports(path))
    )
    assert callers == ["jobs.py"]
