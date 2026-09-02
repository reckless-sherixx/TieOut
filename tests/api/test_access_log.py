"""Audit-on-read: one append-only row per read of financial data (Phase 4 item 3).

The write trail already existed -- `core/models.py:AuditEntry` records what the
engine decided and why -- and it answers nothing about the threat this table is
for. An employee with a valid login reading a tenant's settlement data they have
no business reading changes nothing, and leaves no trace whatsoever in a system
that logs only changes. That is the single most likely incident for a tool like
this one, and it is the one COMPLIANCE.md names first.

Two things are proved here and neither is provable by reading the code:

* **Exactly one row per read.** Not zero (the middleware ran), not two (it did
  not also fire on the internal request the test client makes, or on a redirect).
* **There is no second write path.** Append-only is not a database grant this
  deployment can issue; it is the absence of code. So the absence is asserted --
  over the module's AST, which cannot be satisfied by a comment.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import auth, settings
from core.store.repo import Repo

REPO_ROOT = Path(__file__).resolve().parents[2]

SECRET = "access-log-test-key"
PASSWORD = "who-read-my-settlements"


@pytest.fixture
def audited(tmp_path, monkeypatch):
    """A client with auth disabled, plus the repository behind it.

    Auth disabled deliberately: the demo configuration is the one that has to
    produce an audit trail, or the control is a thing that only exists in a
    deployment nobody runs.
    """
    monkeypatch.setenv("RECON_DB_PATH", str(tmp_path / "recon.db"))
    monkeypatch.setenv("RECON_DATASETS_DIR", str(tmp_path / "datasets"))
    for name in (settings.AUTH_ENV, settings.SECRET_KEY_ENV, settings.USERS_ENV):
        monkeypatch.delenv(name, raising=False)
    from api.deps import get_repo
    from api.main import create_app

    with TestClient(create_app()) as client:
        yield client, get_repo(auth.single_user_principal())


def completed_run(client: TestClient) -> str:
    dataset = client.post(
        "/api/datasets/generate", json={"seed": 42, "record_count": 50}
    ).json()["dataset_id"]
    run_id = client.post(
        "/api/runs", json={"dataset_id": dataset, "use_llm": False}
    ).json()["run_id"]
    for _ in range(200):
        if client.get(f"/api/runs/{run_id}/status").json()["state"] in (
            "completed",
            "failed",
        ):
            break
    return run_id


# --- one row per read ---------------------------------------------------------


def test_one_read_produces_exactly_one_access_row(audited):
    client, repo = audited
    run_id = completed_run(client)

    before = len(repo.access_log(limit=10_000))
    assert client.get(f"/api/runs/{run_id}/exceptions").status_code == 200
    after = repo.access_log(limit=10_000)

    assert len(after) == before + 1, (
        f"one read produced {len(after) - before} access rows"
    )
    row = after[-1]
    assert row.resource == "/api/runs/{id}/exceptions"
    assert row.resource_id == run_id
    assert row.action == "GET"
    assert row.status == 200
    assert row.actor == auth.SINGLE_USER_SUBJECT


def test_the_resource_recorded_is_the_route_template_not_the_url(audited):
    """`/api/runs/{id}` groups; `/api/runs/run-9f3c…` does not.

    The identifier is in its own column, so nothing is lost -- and "how many
    times was the exceptions list read this month" becomes one GROUP BY instead
    of a regex over URLs.
    """
    client, repo = audited
    run_id = completed_run(client)
    before = len(repo.access_log(limit=10_000))

    client.get(f"/api/runs/{run_id}")
    client.get(f"/api/runs/{run_id}/matches")

    rows = repo.access_log(limit=10_000)[before:]
    assert [row.resource for row in rows] == [
        "/api/runs/{id}",
        "/api/runs/{id}/matches",
    ]
    assert {row.resource_id for row in rows} == {run_id}


def test_a_refused_read_is_logged_with_its_status(audited):
    """A 404 sweep across run ids is what enumeration looks like from the
    inside. A log that recorded only successful reads would be blind to exactly
    the behaviour it exists to catch."""
    client, repo = audited
    before = len(repo.access_log(limit=10_000))

    assert client.get("/api/runs/run-does-not-exist").status_code == 404

    rows = repo.access_log(limit=10_000)[before:]
    assert len(rows) == 1
    assert rows[0].status == 404
    assert rows[0].resource_id == "run-does-not-exist"


def test_writes_and_auth_operations_are_not_in_the_access_log(audited):
    """The table is audit-on-**read**.

    A run's own creation is already recorded -- it is a row in `runs` with a
    `created_at` -- and a login is not a read of anybody's ledger.
    """
    client, repo = audited
    before = len(repo.access_log(limit=10_000))

    client.post("/api/datasets/generate", json={"seed": 7, "record_count": 50})
    client.post("/api/auth/logout")

    assert len(repo.access_log(limit=10_000)) == before


def test_the_log_records_when_but_core_never_reads_a_clock(audited):
    """`at` is stamped at the API boundary and handed down, like `created_at`.

    The global constraint has an AST test of its own
    (`tests/test_models.py::test_no_module_under_core_reads_a_wall_clock`); this
    is the value-level half for the one table whose entire purpose is to record
    a time, which is the most tempting place in the codebase to break it.
    """
    from datetime import datetime

    client, repo = audited
    run_id = completed_run(client)
    before = len(repo.access_log(limit=10_000))
    client.get(f"/api/runs/{run_id}/settlements")
    row = repo.access_log(limit=10_000)[before:][0]

    stamped = datetime.fromisoformat(row.at)
    assert stamped.tzinfo is not None, "the boundary stamps an aware instant"


def test_the_access_log_is_scoped_to_the_org_that_did_the_reading(
    tmp_path, monkeypatch
):
    """Reading someone else's audit trail would be its own disclosure.

    "Who has been looking at our settlements" is a question each tenant may ask
    about itself and no tenant may ask about another, so the log goes through
    the same filter as everything else.
    """
    monkeypatch.setenv("RECON_DB_PATH", str(tmp_path / "recon.db"))
    monkeypatch.setenv("RECON_DATASETS_DIR", str(tmp_path / "datasets"))
    monkeypatch.setenv(settings.AUTH_ENV, "enabled")
    monkeypatch.setenv(settings.SECRET_KEY_ENV, SECRET)
    verifier = auth.password_hash(PASSWORD)
    monkeypatch.setenv(
        settings.USERS_ENV,
        f"alice@acme.test|org-acme|{verifier},bob@globex.test|org-globex|{verifier}",
    )
    from api.deps import get_repo
    from api.main import create_app

    app = create_app()
    with TestClient(app) as client_a, TestClient(app) as client_b:
        for client, user in (
            (client_a, "alice@acme.test"),
            (client_b, "bob@globex.test"),
        ):
            assert (
                client.post(
                    "/api/auth/login", json={"email": user, "password": PASSWORD}
                ).status_code
                == 200
            )
        run_id = completed_run(client_a)
        client_a.get(f"/api/runs/{run_id}/exceptions")
        client_b.get("/api/runs")

        repo_a = get_repo(auth.Principal("alice@acme.test", "org-acme", True))
        repo_b = get_repo(auth.Principal("bob@globex.test", "org-globex", True))

    actors_a = {row.actor for row in repo_a.access_log(limit=10_000)}
    actors_b = {row.actor for row in repo_b.access_log(limit=10_000)}
    assert actors_a == {"alice@acme.test"}
    assert actors_b == {"bob@globex.test"}


# --- append-only, proved as the absence of a code path ------------------------


def _repo_source_tree() -> ast.Module:
    return ast.parse((REPO_ROOT / "core" / "store" / "repo.py").read_text("utf-8"))


def _docstring_ids(tree: ast.Module) -> set[int]:
    """`id()` of every node that IS a docstring.

    Exempted from the SQL-string scan for exactly the reason
    `tests/test_boundaries.py` exempts them from its own: the most natural
    docstring for a method in this module is the one that says "nothing here
    deletes a row", and a value-blind check that went red on its own
    documentation would be weakened rather than reworded. Identity, not value:
    an ordinary literal that happens to share a docstring's text is still
    scanned.
    """
    ids = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _functions_naming(tree: ast.Module, symbol: str) -> set[str]:
    """Names of the top-level or method functions whose body mentions `symbol`."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id == symbol:
                found.add(node.name)
    return found


def test_exactly_two_methods_in_the_repository_name_the_access_log_table():
    """One inserts, one selects. A third is the regression this catches.

    Append-only cannot be enforced by a database grant here -- the deployment
    is one SQLite file owned by the process that writes it -- so it is enforced
    by there being no code that does anything else. That is only true as long
    as it stays true, which is what this asserts.
    """
    assert _functions_naming(_repo_source_tree(), "AccessLog") == {
        "record_access",
        "access_log",
    }


def test_the_repository_has_no_delete_or_update_path_at_all():
    """Not merely none for the access log: none anywhere in the module.

    `session.delete`, `session.execute(delete(...))` and a bare `DELETE`/`UPDATE`
    in raw SQL are the three ways a row could leave, and the store's job is to
    accumulate a run's output and read it back. The only raw SQL this module
    issues is the additive `ALTER TABLE ... ADD COLUMN` of the migration, which
    the check below allows by name and nothing else.
    """
    tree = _repo_source_tree()
    docstrings = _docstring_ids(tree)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("delete", "update"):
                offenders.append(f"call to .{func.attr}() at line {node.lineno}")
            if isinstance(func, ast.Name) and func.id in ("delete", "update"):
                offenders.append(f"call to {func.id}() at line {node.lineno}")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            upper = node.value.upper()
            if any(word in upper for word in ("DELETE ", "UPDATE ", "DROP ")):
                offenders.append(f"SQL string at line {node.lineno}: {node.value!r}")
    assert not offenders, (
        "core/store/repo.py can remove or rewrite rows: " + ", ".join(offenders)
    )


def test_the_access_log_model_handed_back_is_not_an_orm_row():
    """`access_log()` returns plain models.

    Handing a caller a SQLModel instance attached to a session is handing them
    something with a `session.add` waiting to happen -- and the append-only
    claim would then rest on nobody noticing.
    """
    from sqlmodel import SQLModel

    from core.store.repo import ReadAccess

    assert not issubclass(ReadAccess, SQLModel)


def test_a_read_is_logged_even_though_the_repo_exposes_no_way_to_erase_it(
    audited,
):
    """The end-to-end shape of the guarantee, in one test.

    A row goes in through the boundary; there is no public method that takes it
    out. Retention will eventually need one (COMPLIANCE.md) -- and when it
    arrives it will be a named operation with its own test, not a method that
    has been callable by every route in the meantime.
    """
    client, repo = audited
    client.get("/api/runs")
    assert repo.access_log(limit=10_000)

    erasers = [
        name
        for name in dir(repo)
        if not name.startswith("_")
        and any(word in name for word in ("delete", "remove", "purge", "erase", "drop"))
    ]
    assert not erasers, erasers
