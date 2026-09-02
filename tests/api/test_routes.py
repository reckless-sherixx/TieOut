"""Every operation `api/openapi.yaml` declares, checked against it.

Started life as Task D.2 over 8 paths; the contract has grown since and this
file grew with it, most recently with `GET /api/runs/{id}/drift` (spec
2026-08-29 §7). The count is not restated here -- `tests/test_briefs.py` pins
every hand-written copy of it to the contract, and this docstring is not one
more place for it to go stale.

The definition of done for this lane is "every endpoint in the OpenAPI stub
returns contract-valid responses against a seeded SQLite database", so the
contract is *checked* here rather than eyeballed: `_contract_errors` walks the
response body against the schema `api/openapi.yaml` declares for that operation,
following `$ref`, `allOf`, `oneOf`/`anyOf`, nullable type lists and enums.

That matters more than usual on this project. Lane E generates its entire
TypeScript layer from the same file and cannot see this worktree, so a response
that drifts from the contract is a break nothing else in the repo would catch.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI = REPO_ROOT / "api" / "openapi.yaml"


# --- the contract checker -----------------------------------------------------


@pytest.fixture(scope="session")
def spec() -> dict:
    return yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))


def _deref(node: dict, spec: dict) -> dict:
    while "$ref" in node:
        section, name = node["$ref"].rsplit("/", 2)[-2:]
        node = spec["components"][section][name]
    return node


def _is_type(value, name: str) -> bool:
    """JSON type test. `bool` is checked first: in Python it is an `int`, and
    a boolean silently passing as `integer` would let money be `true`."""
    if isinstance(value, bool):
        return name == "boolean"
    return {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "boolean": lambda v: False,
        "integer": lambda v: isinstance(v, int),
        "number": lambda v: isinstance(v, (int, float)),
        "null": lambda v: v is None,
    }[name](value)


def _contract_errors(value, node: dict, spec: dict, path: str = "$") -> list[str]:
    node = _deref(node, spec)
    errors: list[str] = []

    for sub in node.get("allOf", []):
        errors += _contract_errors(value, sub, spec, path)
    for key in ("oneOf", "anyOf"):
        if key in node and not any(
            not _contract_errors(value, sub, spec, path) for sub in node[key]
        ):
            errors.append(f"{path}: matches no branch of {key}")

    if "type" in node:
        declared = node["type"]
        names = declared if isinstance(declared, list) else [declared]
        if not any(_is_type(value, name) for name in names):
            return errors + [f"{path}: {value!r} is not one of {names}"]

    if "enum" in node and value not in node["enum"]:
        errors.append(f"{path}: {value!r} is not in {node['enum']}")

    if isinstance(value, dict):
        properties = node.get("properties", {})
        for required in node.get("required", []):
            if required not in value:
                errors.append(f"{path}.{required}: required key is missing")
        for name, sub in properties.items():
            if name in value:
                errors += _contract_errors(value[name], sub, spec, f"{path}.{name}")
        if node.get("additionalProperties") is False:
            unexpected = sorted(set(value) - set(properties))
            if unexpected:
                errors.append(f"{path}: unexpected keys {unexpected}")

    if isinstance(value, list) and "items" in node:
        for index, item in enumerate(value):
            errors += _contract_errors(item, node["items"], spec, f"{path}[{index}]")

    return errors


def assert_contract_valid(body, spec: dict, path: str, method: str, status: str) -> None:
    response = _deref(
        spec["paths"][path][method]["responses"][status], spec
    )
    schema = response["content"]["application/json"]["schema"]
    errors = _contract_errors(body, schema, spec)
    assert not errors, f"{method.upper()} {path} -> {status} violates the contract:\n" + "\n".join(
        errors
    )


# --- app fixtures -------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client on an isolated SQLite file and dataset directory."""
    monkeypatch.setenv("RECON_DB_PATH", str(tmp_path / "recon.db"))
    monkeypatch.setenv("RECON_DATASETS_DIR", str(tmp_path / "datasets"))
    from api.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def generate(client, seed: int = 42, record_count: int = 50, **extra) -> str:
    response = client.post(
        "/api/datasets/generate",
        json={"seed": seed, "record_count": record_count, **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()["dataset_id"]


def run_to_completion(client, dataset_id: str, *, use_llm: bool = False) -> str:
    created = client.post(
        "/api/runs", json={"dataset_id": dataset_id, "use_llm": use_llm}
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    for _ in range(200):
        state = client.get(f"/api/runs/{run_id}/status").json()["state"]
        if state in ("completed", "failed"):
            break
    assert state == "completed", client.get(f"/api/runs/{run_id}/status").json()
    return run_id


@pytest.fixture
def completed_run(client) -> str:
    return run_to_completion(client, generate(client))


# --- the three the plan names -------------------------------------------------


def test_generate_then_run_then_poll_to_completion(client, spec):
    dataset = client.post(
        "/api/datasets/generate", json={"seed": 42, "record_count": 50}
    )
    assert dataset.status_code == 200
    assert_contract_valid(
        dataset.json(), spec, "/api/datasets/generate", "post", "200"
    )

    run = client.post(
        "/api/runs", json={"dataset_id": dataset.json()["dataset_id"], "use_llm": False}
    )
    assert run.status_code == 202
    assert_contract_valid(run.json(), spec, "/api/runs", "post", "202")

    run_id = run.json()["run_id"]
    for _ in range(200):
        status = client.get(f"/api/runs/{run_id}/status")
        assert status.status_code == 200
        assert_contract_valid(
            status.json(), spec, "/api/runs/{id}/status", "get", "200"
        )
        if status.json()["state"] in ("completed", "failed"):
            break
    assert status.json()["state"] == "completed"
    assert status.json()["progress"] == 1.0


def test_summary_exposes_false_match_rate(client, completed_run, spec):
    body = client.get(f"/api/runs/{completed_run}").json()
    assert_contract_valid(body, spec, "/api/runs/{id}", "get", "200")
    assert "false_match_rate" in body["metrics"]
    assert "trap_capture_rate" in body["metrics"]


@pytest.mark.parametrize(
    "url",
    [
        "/api/runs/does-not-exist",
        "/api/runs/does-not-exist/status",
        "/api/runs/does-not-exist/exceptions",
        "/api/runs/does-not-exist/settlements",
        "/api/runs/does-not-exist/matches",
        "/api/runs/does-not-exist/batches/setl_00001",
        "/api/runs/does-not-exist/drift",
        "/api/exceptions/does-not-exist",
        "/api/matches/does-not-exist",
    ],
)
def test_unknown_run_returns_404(client, url, spec):
    response = client.get(url)
    assert response.status_code == 404
    assert "detail" in response.json()


# --- every remaining operation, against the contract --------------------------


def test_run_history_lists_runs_most_recent_first(client, completed_run, spec):
    second = run_to_completion(client, generate(client, record_count=20))
    body = client.get("/api/runs").json()

    assert_contract_valid(body, spec, "/api/runs", "get", "200")
    assert [r["run_id"] for r in body][:2] == [second, completed_run]
    for run in body:
        # Present on every run, and a real instant -- the history table renders
        # this rather than a client clock.
        assert datetime.fromisoformat(run["created_at"])


def test_exceptions_page_is_contract_valid_and_carries_subjects(
    client, completed_run, spec
):
    response = client.get(f"/api/runs/{completed_run}/exceptions?page=1&size=10")
    assert response.status_code == 200
    body = response.json()
    assert_contract_valid(body, spec, "/api/runs/{id}/exceptions", "get", "200")
    assert body["items"], "the seed-42 dataset produces exceptions"
    assert body["page"] == 1 and body["size"] == 10

    for item in body["items"]:
        identifier = {
            "order": "order_id",
            "psp_txn": "txn_id",
            "bank_line": "line_id",
        }[item["subject_type"]]
        assert item["subject"][identifier] == item["subject_id"]


def test_the_paginated_list_does_not_inline_audit_trails(client, completed_run):
    """The list pages over as many as 5,000 rows; the trail is fetched per row.

    Contract: the list is `ReconExceptionDetail` (exception + subject) and the
    trail lives only on `GET /api/exceptions/{id}`.
    """
    items = client.get(f"/api/runs/{completed_run}/exceptions?size=100").json()["items"]
    assert items
    assert all("audit_trail" not in item for item in items)


def test_exception_detail_adds_the_full_audit_trail(client, completed_run, spec):
    listed = client.get(f"/api/runs/{completed_run}/exceptions?size=100").json()["items"]
    subject = next(
        item for item in listed if item["reason_code"] == "AMBIGUOUS_MULTI_CANDIDATE"
    )

    response = client.get(f"/api/exceptions/{subject['exception_id']}")
    assert response.status_code == 200
    body = response.json()
    assert_contract_valid(body, spec, "/api/exceptions/{id}", "get", "200")

    assert body["subject"]["line_id"] == subject["subject_id"]
    assert body["audit_trail"], "the trap line has a canonicalize and a match entry"
    sequences = [entry["sequence"] for entry in body["audit_trail"]]
    assert sequences == sorted(sequences), "the trail is ordered by sequence"
    assert all(entry["subject_id"] == subject["subject_id"] for entry in body["audit_trail"])


def test_match_detail_carries_the_bank_line_and_its_trail(client, completed_run, spec):
    settlement, match_id, bank_line_id = _a_matched_settlement(client, completed_run)

    response = client.get(f"/api/matches/{match_id}")
    assert response.status_code == 200
    body = response.json()
    assert_contract_valid(body, spec, "/api/matches/{id}", "get", "200")
    assert body["subject"]["line_id"] == bank_line_id
    assert body["net"] == body["subject"]["credit"], (
        "MatchGroup.net must equal the bank line credit -- the engine's invariant, "
        "passed through untouched"
    )


def test_batch_netting_is_contract_valid_and_passes_the_engine_through(
    client, completed_run, spec
):
    settlement, match_id, bank_line_id = _a_matched_settlement(client, completed_run)

    response = client.get(f"/api/runs/{completed_run}/batches/{settlement}")
    assert response.status_code == 200
    body = response.json()
    assert_contract_valid(
        body, spec, "/api/runs/{id}/batches/{settlement_id}", "get", "200"
    )

    match = client.get(f"/api/matches/{match_id}").json()
    for field in ("gross", "fees", "tax", "refunds", "holds", "net", "tier"):
        assert body[field] == match[field], f"{field} was re-derived, not passed through"
    assert {o["order_id"] for o in body["orders"]} == set(match["order_ids"])


def test_unknown_settlement_on_a_known_run_is_404(client, completed_run):
    assert (
        client.get(f"/api/runs/{completed_run}/batches/setl_nope").status_code == 404
    )


def _a_matched_settlement(client, run_id: str) -> tuple[str, str, str]:
    """`(settlement_id, match_id, bank_line_id)` for a settlement this run matched.

    Found through the API alone: the bank lines the run did *not* except are the
    ones it matched, and the engine mints a match at `match-<line_id>`.
    """
    assert client.get(f"/api/runs/{run_id}").json()["match_count"] > 0
    excepted = {
        item["subject_id"]
        for item in client.get(f"/api/runs/{run_id}/exceptions?size=5000").json()["items"]
    }

    from core.ingest.reader import read_bank

    directory = _dataset_dir_of(client, run_id)
    for line in read_bank(directory / "bank.csv"):
        if line.line_id in excepted:
            continue
        match = client.get(f"/api/matches/match-{line.line_id}")
        if match.status_code == 200 and match.json()["settlement_id"]:
            return match.json()["settlement_id"], f"match-{line.line_id}", line.line_id
    raise AssertionError("no matched settlement found in the run")


def _dataset_dir_of(client, run_id: str) -> Path:
    from api.deps import get_repo
    from api.auth import single_user_principal
    from api.settings import datasets_dir

    return datasets_dir() / get_repo(single_user_principal()).dataset_id(run_id)


# --- the rules the brief calls out --------------------------------------------


def test_defect_mix_may_be_omitted_or_null(client):
    """`{"seed": 42, "record_count": 50}` is a complete, valid request.

    The UI has no control for `defect_mix` (spec 13 #1), so a 422 here would
    make the strongest demo beat unusable. Null is passed straight through to
    the generator, which owns the one copy of the default mix.
    """
    assert (
        client.post(
            "/api/datasets/generate", json={"seed": 42, "record_count": 50}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/datasets/generate",
            json={"seed": 42, "record_count": 50, "defect_mix": None},
        ).status_code
        == 200
    )


def test_an_unknown_defect_type_is_a_422_not_a_500(client):
    response = client.post(
        "/api/datasets/generate",
        json={"seed": 42, "record_count": 50, "defect_mix": {"not_a_defect": 1}},
    )
    assert response.status_code == 422
    assert "not_a_defect" in response.text


def test_a_run_against_an_unknown_dataset_is_404(client):
    response = client.post(
        "/api/runs", json={"dataset_id": "ds-nope", "use_llm": False}
    )
    assert response.status_code == 404


def test_a_dataset_id_cannot_escape_the_dataset_directory(client):
    response = client.post(
        "/api/runs", json={"dataset_id": "../../fixtures", "use_llm": False}
    )
    assert response.status_code == 404


def test_use_llm_false_reports_llm_metrics_at_zero_not_null(client, completed_run):
    """Lane C may be cut entirely; the deterministic path must be first class."""
    metrics = client.get(f"/api/runs/{completed_run}").json()["metrics"]
    assert metrics["llm_rejection_rate"] == 0.0
    assert metrics["llm_cost_usd_per_100"] == 0.0
    assert metrics["llm_tokens_per_100"] == 0
    assert metrics["tier_counts"]["LLM"] == 0
    assert set(metrics["tier_counts"]) == {"T0", "T1", "T2", "T3", "LLM"}


def test_money_crosses_the_wire_as_an_integer(client, completed_run):
    """`Money` is `type: integer`. A float here is a contract break even when
    the value happens to be whole."""
    raw = client.get(f"/api/runs/{completed_run}/exceptions?size=1000").text
    items = client.get(f"/api/runs/{completed_run}/exceptions?size=1000").json()["items"]
    for item in items:
        assert isinstance(item["amount"], int) and not isinstance(item["amount"], bool)
    assert ".0," not in raw and ".0}" not in raw


def test_pagination_is_stable_over_http(client):
    """The same guarantee as the store, through the router's own paging."""
    run_id = run_to_completion(client, generate(client, record_count=500))
    total = client.get(f"/api/runs/{run_id}/exceptions?size=1").json()["total"]
    assert total > 6, "the 500-record dataset produces a workable exception list"

    seen: list[str] = []
    size = 3
    for page in range(1, (total + size - 1) // size + 1):
        body = client.get(
            f"/api/runs/{run_id}/exceptions?page={page}&size={size}"
        ).json()
        seen.extend(item["exception_id"] for item in body["items"])
    assert len(seen) == total
    assert len(set(seen)) == total, "a row was served on two different pages"
    assert seen == sorted(seen)


def test_reason_code_filter_narrows_the_page(client, completed_run):
    filtered = client.get(
        f"/api/runs/{completed_run}/exceptions?reason_code=AMBIGUOUS_MULTI_CANDIDATE"
    ).json()
    assert filtered["items"]
    assert all(
        item["reason_code"] == "AMBIGUOUS_MULTI_CANDIDATE" for item in filtered["items"]
    )
    everything = client.get(f"/api/runs/{completed_run}/exceptions?size=1").json()
    assert filtered["total"] < everything["total"]


def test_an_unknown_reason_code_is_a_422(client, completed_run):
    assert (
        client.get(f"/api/runs/{completed_run}/exceptions?reason_code=NOPE").status_code
        == 422
    )


# --- the headline numbers -----------------------------------------------------


def test_the_api_returns_the_engines_own_numbers_for_a_500_record_run(client):
    """The API must not editorialise the engine's metrics.

    These are the values `recon run --dataset fixtures/seed42-500 --no-llm`
    produces. If the API ever reports something else for the same seed, the API
    is wrong -- there is no arithmetic in `api/` that could legitimately move
    them.

    **These numbers moved when `obfuscated_settlement_ref` was added**, and the
    move is the point rather than a regression. `auto_match_rate` was 0.9379
    (151/161) with tiers {T0 136, T1 2, T2 8, T3 5}. The new defect class puts
    ten more subjects into the dataset that the deterministic engine cannot
    resolve *by construction* -- they post outside its two-day window -- so the
    numerator drops by exactly ten and the denominator does not move: 141/161.
    The engine did not get worse; it was handed work it is not allowed to
    guess at, and `assisted_match_rate` is where that work is now measured.

    `false_match_rate` and `trap_capture_rate` are unchanged, which is the
    check that matters: harder data must not buy a wrong answer.
    """
    run_id = run_to_completion(client, generate(client, seed=42, record_count=500))
    body = client.get(f"/api/runs/{run_id}").json()
    metrics = body["metrics"]

    assert body["record_count"] == 500
    assert round(metrics["auto_match_rate"], 4) == 0.8758
    assert metrics["trap_capture_rate"] == 1.0
    assert metrics["false_match_rate"] == 0.0
    assert metrics["precision"] == 1.0
    assert metrics["tier_counts"] == {"T0": 126, "T1": 1, "T2": 9, "T3": 5, "LLM": 0}
    assert metrics["throughput_records_per_sec"] > 0, "timed at the API boundary"


# --- CORS: TestClient cannot exercise it, so pin the configuration -------------


def test_cors_defaults_to_the_next_dev_origin_and_refuses_a_wildcard(monkeypatch):
    from api import settings

    monkeypatch.delenv(settings.CORS_ORIGINS_ENV, raising=False)
    assert settings.cors_origins() == ["http://localhost:3000"]

    monkeypatch.setenv(
        settings.CORS_ORIGINS_ENV, "http://localhost:3000, https://recon.example"
    )
    assert settings.cors_origins() == [
        "http://localhost:3000",
        "https://recon.example",
    ]

    monkeypatch.setenv(settings.CORS_ORIGINS_ENV, "*")
    with pytest.raises(ValueError, match="wildcard"):
        settings.cors_origins()


def test_the_app_mounts_cors_for_the_dev_origin(client):
    """`TestClient` never issues a cross-origin request, so the middleware is
    checked by configuration here and by hand with curl once (see
    LANE-D-REPORT.md). Lane E is forbidden from routing around a CORS error, so
    getting this wrong blocks a lane that cannot see this worktree."""
    from starlette.middleware.cors import CORSMiddleware

    cors = next(
        middleware
        for middleware in client.app.user_middleware
        if middleware.cls is CORSMiddleware
    )
    options = cors.kwargs
    assert options["allow_origins"] == ["http://localhost:3000"]
    assert "*" not in options["allow_origins"]
    assert {"GET", "POST", "OPTIONS"} <= set(options["allow_methods"])


# --- the settlements listing --------------------------------------------------


def test_settlements_listing_is_contract_valid_and_covers_every_batch(
    client, completed_run, spec
):
    """Every settlement the run saw, matched or not, against the contract."""
    response = client.get(f"/api/runs/{completed_run}/settlements?page=1&size=500")
    assert response.status_code == 200
    body = response.json()
    assert_contract_valid(body, spec, "/api/runs/{id}/settlements", "get", "200")
    assert body["items"] and body["page"] == 1 and body["size"] == 500

    _orders, psp_txns, _bank = read_dataset_for(completed_run)
    expected = sorted({t.settlement_id for t in psp_txns if t.settlement_id})
    assert [s["settlement_id"] for s in body["items"]] == expected
    assert body["total"] == len(expected)


def test_every_money_field_on_a_settlement_row_is_an_integer(client, completed_run):
    """Money is int paise on the wire. A float here is the bug this repo exists
    to prevent, and `1.0 == 1` in Python would let a careless assertion miss it."""
    items = client.get(f"/api/runs/{completed_run}/settlements?size=500").json()["items"]
    assert items
    for row in items:
        for field in ("gross", "fees", "tax", "refunds", "holds", "net"):
            assert isinstance(row[field], int) and not isinstance(row[field], bool), (
                f"{row['settlement_id']}.{field} is {type(row[field]).__name__}, "
                "not int paise"
            )


def test_the_settlements_listing_does_not_inline_audit_trails(client, completed_run):
    items = client.get(f"/api/runs/{completed_run}/settlements?size=500").json()["items"]
    assert items
    assert all("audit_trail" not in row for row in items)
    assert all("evidence" not in row for row in items)


def test_settlement_rows_reconcile_with_the_tier_counts(client, completed_run):
    """The check the brief names: if this listing and `tier_counts` disagree
    about how many settlements matched, one of them is wrong.

    Both sides are the engine's own `MatchGroup.tier`, counted twice and
    required to agree -- the listing per settlement, the metric per match.
    """
    items = client.get(f"/api/runs/{completed_run}/settlements?size=5000").json()["items"]
    summary = client.get(f"/api/runs/{completed_run}").json()

    matched = [row for row in items if row["matched"]]
    assert all(row["tier"] is not None for row in matched)
    assert all(row["bank_line_id"] and row["match_id"] for row in matched)
    assert all(
        row["tier"] is None and row["bank_line_id"] is None and row["match_id"] is None
        for row in items
        if not row["matched"]
    )

    from collections import Counter

    by_tier = Counter(row["tier"] for row in matched)
    tier_counts = summary["metrics"]["tier_counts"]
    assert dict(by_tier) == {k: v for k, v in tier_counts.items() if v}, (
        "the settlements listing and Metrics.tier_counts disagree about which "
        "tier closed which batch"
    )
    assert len(matched) == sum(tier_counts.values()) == summary["match_count"], (
        "every match in this run names a settlement, so the two counts are the "
        "same fact and must agree"
    )


def test_a_settlement_row_links_to_its_netting_breakdown(client, completed_run):
    """A matched row is the netting diagram's entry point, so what it carries
    must be what the diagram shows."""
    items = client.get(f"/api/runs/{completed_run}/settlements?size=5000").json()["items"]
    row = next(item for item in items if item["matched"])

    netting = client.get(
        f"/api/runs/{completed_run}/batches/{row['settlement_id']}"
    ).json()
    for field in ("gross", "fees", "tax", "refunds", "holds", "net", "tier"):
        assert row[field] == netting[field], f"{field} disagrees with the batch endpoint"
    assert row["bank_line_id"] == netting["bank_line_id"]

    match = client.get(f"/api/matches/{row['match_id']}").json()
    assert match["settlement_id"] == row["settlement_id"]


def test_settlements_pagination_never_repeats_a_row(client, completed_run):
    """Page 2 may never repeat a row from page 1. The stress test at 5,000
    records lives in `tests/api/test_store.py`; this checks the wiring."""
    first = client.get(f"/api/runs/{completed_run}/settlements?page=1&size=5").json()
    second = client.get(f"/api/runs/{completed_run}/settlements?page=2&size=5").json()
    assert len(first["items"]) == len(second["items"]) == 5
    assert first["total"] == second["total"] > 10
    ids = [s["settlement_id"] for s in first["items"] + second["items"]]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)



# --- the records listing ------------------------------------------------------


@pytest.mark.parametrize(
    "source,identifier",
    [("order", "order_id"), ("psp_txn", "txn_id"), ("bank_line", "line_id")],
)
def test_records_listing_serves_each_source_contract_valid(
    client, completed_run, spec, source, identifier
):
    """The rows the engine read, one source at a time, against the contract."""
    response = client.get(
        f"/api/runs/{completed_run}/records?source={source}&size=5000"
    )
    assert response.status_code == 200
    body = response.json()
    assert_contract_valid(body, spec, "/api/runs/{id}/records", "get", "200")

    assert body["source"] == source
    assert body["items"], f"the seed-42 dataset has {source} rows"
    assert body["total"] == len(body["items"])
    ids = [item[identifier] for item in body["items"]]
    assert ids == sorted(ids), "ORDER BY record_id is the stability guarantee"
    assert len(set(ids)) == len(ids)


def test_records_listing_returns_the_ingested_rows_unchanged(client, completed_run):
    """The listing is the dataset, not a projection of it -- nulls included."""
    orders, psp_txns, bank_lines = read_dataset_for(completed_run)
    for source, expected in (
        ("order", orders),
        ("psp_txn", psp_txns),
        ("bank_line", bank_lines),
    ):
        body = client.get(
            f"/api/runs/{completed_run}/records?source={source}&size=5000"
        ).json()
        assert body["total"] == len(expected)
        assert body["items"] == sorted(
            (item.model_dump(mode="json") for item in expected),
            key=lambda row: next(iter(row.values())),
        )


def test_a_null_on_an_ingested_record_survives_to_the_wire(client, completed_run):
    """`PSPTransaction.order_id` absent is the missing_order_ref defect and
    `BankLine.credit` null is a debit line. Both are data, not gaps."""
    psp = client.get(
        f"/api/runs/{completed_run}/records?source=psp_txn&size=5000"
    ).json()["items"]
    assert any(row["order_id"] is None for row in psp), (
        "the seed-42 dataset carries the missing_order_ref defect"
    )
    assert all("order_id" in row for row in psp), "a null key is never omitted"

    bank = client.get(
        f"/api/runs/{completed_run}/records?source=bank_line&size=5000"
    ).json()["items"]
    assert all("credit" in row and "debit" in row and "utr" in row for row in bank)


def test_every_money_field_on_a_record_row_is_an_integer(client, completed_run):
    """Money is int paise on the wire, on the ingested rows too."""
    money = {
        "order": ("gross_amount",),
        "psp_txn": ("amount",),
        "bank_line": ("credit", "debit", "balance"),
    }
    for source, fields in money.items():
        items = client.get(
            f"/api/runs/{completed_run}/records?source={source}&size=5000"
        ).json()["items"]
        assert items
        for row in items:
            for field in fields:
                value = row[field]
                assert value is None or (
                    isinstance(value, int) and not isinstance(value, bool)
                ), f"{source}.{field} is {type(value).__name__}, not int paise"


def test_records_pagination_never_repeats_a_row(client, completed_run):
    first = client.get(
        f"/api/runs/{completed_run}/records?source=order&page=1&size=10"
    ).json()
    second = client.get(
        f"/api/runs/{completed_run}/records?source=order&page=2&size=10"
    ).json()
    assert len(first["items"]) == len(second["items"]) == 10
    ids = [row["order_id"] for row in first["items"] + second["items"]]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)


def test_records_without_a_source_is_a_422_not_an_empty_page(client, completed_run):
    """An empty page for a missing source reads as "this run has no records"."""
    assert client.get(f"/api/runs/{completed_run}/records").status_code == 422
    assert (
        client.get(f"/api/runs/{completed_run}/records?source=orders").status_code == 422
    )


def test_records_on_an_unknown_run_is_404(client):
    response = client.get("/api/runs/does-not-exist/records?source=order")
    assert response.status_code == 404
    assert "detail" in response.json()



# --- the matches listing ------------------------------------------------------


def test_matches_listing_is_contract_valid_and_carries_tier_and_evidence(
    client, completed_run, spec
):
    """What was matched, not only what failed -- with the tier and the evidence."""
    response = client.get(f"/api/runs/{completed_run}/matches?size=5000")
    assert response.status_code == 200
    body = response.json()
    assert_contract_valid(body, spec, "/api/runs/{id}/matches", "get", "200")

    summary = client.get(f"/api/runs/{completed_run}").json()
    assert body["total"] == summary["match_count"] > 0
    assert len(body["items"]) == body["total"]

    ids = [row["match_id"] for row in body["items"]]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    for row in body["items"]:
        assert row["tier"] in ("T0", "T1", "T2", "T3", "LLM")
        assert row["evidence"], "a match with no evidence cannot be checked"


def test_the_matches_listing_does_not_inline_audit_trails(client, completed_run):
    """Same rule as the exceptions list: the trail is per row, on demand."""
    items = client.get(f"/api/runs/{completed_run}/matches?size=5000").json()["items"]
    assert items
    assert all("audit_trail" not in row for row in items)
    assert all("subject" not in row for row in items)

    detail = client.get(f"/api/matches/{items[0]['match_id']}").json()
    assert detail["audit_trail"] and detail["subject"]


def test_matches_listing_agrees_with_the_tier_counts(client, completed_run):
    """The listing and `Metrics.tier_counts` are the same fact counted twice."""
    from collections import Counter

    items = client.get(f"/api/runs/{completed_run}/matches?size=5000").json()["items"]
    tier_counts = client.get(f"/api/runs/{completed_run}").json()["metrics"][
        "tier_counts"
    ]
    assert Counter(row["tier"] for row in items) == Counter(
        {tier: n for tier, n in tier_counts.items() if n}
    )


def test_every_money_field_on_a_match_row_is_an_integer(client, completed_run):
    items = client.get(f"/api/runs/{completed_run}/matches?size=5000").json()["items"]
    assert items
    for row in items:
        for field in ("gross", "fees", "tax", "refunds", "holds", "net"):
            assert isinstance(row[field], int) and not isinstance(row[field], bool), (
                f"{row['match_id']}.{field} is {type(row[field]).__name__}, not int paise"
            )


def test_matches_pagination_never_repeats_a_row(client, completed_run):
    first = client.get(f"/api/runs/{completed_run}/matches?page=1&size=5").json()
    second = client.get(f"/api/runs/{completed_run}/matches?page=2&size=5").json()
    assert len(first["items"]) == len(second["items"]) == 5
    assert first["total"] == second["total"]
    ids = [row["match_id"] for row in first["items"] + second["items"]]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)


def read_dataset_for(run_id: str):
    """The three input record lists a run was executed over.

    The route tests generate into a temporary datasets root, so a run's inputs
    are not a committed fixture: the dataset is found through the run itself,
    the way the job that executed it found it.
    """
    from api import settings
    from api.deps import get_repo
    from api.auth import single_user_principal
    from api.jobs import resolve_dataset
    from core.ingest.reader import read_bank, read_orders, read_psp

    directory = resolve_dataset(
        settings.datasets_dir(), get_repo(single_user_principal()).dataset_id(run_id)
    )
    assert directory is not None, f"run {run_id} names no dataset on disk"
    return (
        read_orders(directory / "orders.csv"),
        read_psp(directory / "psp.csv"),
        read_bank(directory / "bank.csv"),
    )


# --- drift (spec §7) ----------------------------------------------------------
#
# `GET /api/runs/{id}/drift?against={run_id}`. The route's whole job is to pick
# two runs, refuse the pairs that cannot be compared, read the two censuses out
# of the store and hand plain arguments to `core/drift/compare.py`. There is no
# detection logic here, and these tests are written so that any that crept in
# would show up as a route test that had to know a threshold.


def test_drift_against_an_explicit_run_is_contract_valid(client, spec):
    dataset = generate(client, seed=42, record_count=50)
    baseline = run_to_completion(client, dataset)
    current = run_to_completion(client, dataset)

    response = client.get(f"/api/runs/{current}/drift?against={baseline}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert_contract_valid(body, spec, "/api/runs/{id}/drift", "get", "200")

    assert body["baseline_run_id"] == baseline
    assert body["current_run_id"] == current


def test_the_same_dataset_run_twice_reports_no_material_move(client):
    """The control case. Same inputs, same engine, no LLM: every metric must
    come back flat and no reason code may move. A drift detector that reported
    a finding here would be reporting itself."""
    dataset = generate(client, seed=42, record_count=50)
    baseline = run_to_completion(client, dataset)
    current = run_to_completion(client, dataset)

    body = client.get(f"/api/runs/{current}/drift?against={baseline}").json()

    material = [m for m in body["moves"] if m["material"]]
    assert material == [], f"the same dataset twice moved: {material}"
    assert body["reason_code_moves"] == []
    assert body["moves"], "every metric is reported, moved or not"

    # Throughput is measured wall clock and is the one number two runs of the
    # same data legitimately disagree about; everything else must be identical.
    # This test found the throughput rule: it failed under suite load while
    # throughput still had a threshold.
    moved = [m["metric"] for m in body["moves"] if m["delta"] != 0]
    assert moved in ([], ["throughput_records_per_sec"]), moved


def test_drift_with_no_against_uses_the_previous_completed_run(client):
    dataset = generate(client, seed=42, record_count=50)
    first = run_to_completion(client, dataset)
    second = run_to_completion(client, dataset)
    third = run_to_completion(client, dataset)

    body = client.get(f"/api/runs/{third}/drift").json()
    assert (body["baseline_run_id"], body["current_run_id"]) == (second, third)

    body = client.get(f"/api/runs/{second}/drift").json()
    assert (body["baseline_run_id"], body["current_run_id"]) == (first, second)


def test_a_run_on_another_dataset_is_not_picked_as_the_default_baseline(client):
    """The default baseline is "on the same dataset". Another dataset of the
    same shape is a legal *explicit* comparison and a wrong *implicit* one."""
    other = run_to_completion(client, generate(client, seed=43, record_count=50))
    dataset = generate(client, seed=42, record_count=50)
    first = run_to_completion(client, dataset)
    second = run_to_completion(client, dataset)

    body = client.get(f"/api/runs/{second}/drift").json()
    assert body["baseline_run_id"] == first
    assert body["baseline_run_id"] != other


def test_two_seeds_at_the_same_size_are_a_real_comparison(client, spec):
    """Same shape, different data -- the comparison drift exists for. Different
    `dataset_id`s, so this is the case that proves the 409 rule is about the
    dataset's SHAPE and not about its identity."""
    baseline = run_to_completion(client, generate(client, seed=42, record_count=50))
    current = run_to_completion(client, generate(client, seed=43, record_count=50))

    response = client.get(f"/api/runs/{current}/drift?against={baseline}")
    assert response.status_code == 200, response.text
    assert_contract_valid(response.json(), spec, "/api/runs/{id}/drift", "get", "200")


def test_two_different_record_counts_are_refused_with_409(client):
    """Rates computed over different denominators and paise sums over ten times
    the data are not a comparison. Refusing is better than silently returning
    nonsense."""
    baseline = run_to_completion(client, generate(client, seed=42, record_count=50))
    current = run_to_completion(client, generate(client, seed=42, record_count=500))

    response = client.get(f"/api/runs/{current}/drift?against={baseline}")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "50" in detail and "500" in detail, (
        f"the refusal must name the two shapes it compared: {detail!r}"
    )


def test_the_409_is_symmetric(client):
    baseline = run_to_completion(client, generate(client, seed=42, record_count=50))
    current = run_to_completion(client, generate(client, seed=42, record_count=500))
    assert client.get(f"/api/runs/{current}/drift?against={baseline}").status_code == 409
    assert client.get(f"/api/runs/{baseline}/drift?against={current}").status_code == 409


def test_an_unknown_current_run_is_404(client, completed_run):
    response = client.get(f"/api/runs/nope/drift?against={completed_run}")
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


def test_an_unknown_against_run_is_404(client, completed_run):
    """Both ids are checked. A silently ignored `against` would compare the run
    to whatever the default baseline happened to be, under a run id the caller
    never asked for."""
    response = client.get(f"/api/runs/{completed_run}/drift?against=nope")
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


def test_a_run_with_no_earlier_run_on_its_dataset_is_404(client):
    """There is no drift report to return: the resource does not exist. Not a
    409 -- nothing about these runs conflicts, there is simply only one."""
    only = run_to_completion(client, generate(client, seed=42, record_count=50))
    response = client.get(f"/api/runs/{only}/drift")
    assert response.status_code == 404
    assert only in response.json()["detail"]


def test_a_run_carrying_no_metrics_is_refused_with_409(client, completed_run):
    """A run can exist, be `completed`, and still have no `Metrics` -- a dataset
    with no truth.json scores nothing. There is a run and there is no number to
    compare, which is a conflict with the run's state, not a missing resource."""
    from api.deps import get_repo
    from api.auth import single_user_principal

    repo = get_repo(single_user_principal())
    dataset_id = repo.dataset_id(completed_run)
    scoreless = repo.create_run(
        seed=42,
        record_count=50,
        created_at=datetime.now(),
        dataset_id=dataset_id,
    )
    repo.set_progress(scoreless, state="completed", progress=1.0, stage="complete")

    response = client.get(f"/api/runs/{scoreless}/drift?against={completed_run}")
    assert response.status_code == 409
    assert "metrics" in response.json()["detail"].lower()

    response = client.get(f"/api/runs/{completed_run}/drift?against={scoreless}")
    assert response.status_code == 409


def test_no_model_ran_so_the_narrative_is_null(client):
    """Reading a drift report does not call an LLM. `narrative` is optional and
    is None when no model ran, which on this endpoint is always."""
    dataset = generate(client, seed=42, record_count=50)
    baseline = run_to_completion(client, dataset)
    current = run_to_completion(client, dataset)
    body = client.get(f"/api/runs/{current}/drift?against={baseline}").json()
    assert body["narrative"] is None


def test_the_same_request_twice_returns_the_same_report(client):
    """Detection is deterministic, asserted at the HTTP boundary as well as in
    the unit tests: two identical requests, one identical body."""
    baseline = run_to_completion(client, generate(client, seed=42, record_count=50))
    current = run_to_completion(client, generate(client, seed=43, record_count=50))
    url = f"/api/runs/{current}/drift?against={baseline}"
    assert client.get(url).json() == client.get(url).json()


def test_every_reason_code_move_is_a_code_the_runs_actually_recorded(client):
    """The route reads the census out of the store and hands it over; it does
    not invent a template row for a code neither run produced."""
    from api.deps import get_repo
    from api.auth import single_user_principal

    baseline = run_to_completion(client, generate(client, seed=42, record_count=50))
    current = run_to_completion(client, generate(client, seed=43, record_count=50))

    repo = get_repo(single_user_principal())
    seen = set(repo.reason_code_census(baseline)) | set(
        repo.reason_code_census(current)
    )
    body = client.get(f"/api/runs/{current}/drift?against={baseline}").json()
    assert {m["reason_code"] for m in body["reason_code_moves"]} <= seen


def test_the_route_computes_nothing_the_comparison_module_computes(client):
    """`api/routes.py` has no arithmetic in it, and drift does not become the
    exception. The route's body is checked against `core.drift.compare` called
    directly on the same two runs."""
    from api.deps import get_repo
    from api.auth import single_user_principal
    from core.drift.compare import compare

    baseline = run_to_completion(client, generate(client, seed=42, record_count=50))
    current = run_to_completion(client, generate(client, seed=43, record_count=50))

    repo = get_repo(single_user_principal())
    before, after = repo.summary(baseline), repo.summary(current)
    expected = compare(
        before,
        after,
        baseline_metrics=before.metrics,
        current_metrics=after.metrics,
        baseline_census=repo.reason_code_census(baseline),
        current_census=repo.reason_code_census(current),
    )
    body = client.get(f"/api/runs/{current}/drift?against={baseline}").json()
    assert body == expected.model_dump(mode="json")
