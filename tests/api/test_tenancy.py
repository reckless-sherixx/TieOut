"""The route table, walked twice (Phase 4 items 1 and 2).

One table, two proofs, and they are in the same file because they must be the
*same* table:

1. **Auth disabled is exactly today's behaviour.** Every operation the contract
   declares answers with no cookie, no header and no configuration -- the demo
   path, the console on `main`, and the six hundred existing tests all run
   against this configuration and none of them may have to learn about auth.
2. **Auth enabled isolates orgs.** The same dataset ingested by two orgs, and
   every read endpoint returns org A nothing when the session says org B.

The table is **derived from `api/openapi.yaml`, not hand-listed**, and
`test_the_route_table_covers_every_read_operation_in_the_contract` fails if the
contract grows an operation this file does not exercise. That is the mechanism
the brief asks for: a future endpoint cannot be added without tenancy, because
adding it to the contract turns this file red until it is covered.

Isolation is asserted as **"org B sees nothing of org A"**, not as "org B sees
fewer rows". A listing that returned org A's rows with org B's totals would
pass a count-based assertion; the checks below name the ids.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from api import auth, settings

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI = REPO_ROOT / "api" / "openapi.yaml"

#: Hand-written fixtures in two real formats, one per org, both with damage in
#: them. Damage on purpose: `GET /api/uploads/{id}/quarantine` on a clean file
#: answers 200 with an empty page, and a leak check over an empty body cannot
#: fail -- the walk would pass vacuously on the one endpoint that serves a
#: merchant's own rows back verbatim.
REAL_FORMATS = REPO_ROOT / "fixtures" / "real-formats"
UPLOAD_FOR_A = REAL_FORMATS / "razorpay-settlement-dirty.csv"
UPLOAD_FOR_B = REAL_FORMATS / "hdfc-statement-dirty.csv"

SECRET = "tenancy-test-signing-key-not-a-real-one"
PASSWORD = "two-orgs-one-database"

ORG_A = "org-acme"
ORG_B = "org-globex"
USER_A = "alice@acme.test"
USER_B = "bob@globex.test"


# --- the table ----------------------------------------------------------------
#
# `{...}` placeholders are filled from the ids of the org that owns the data.
# A read is listed here once and is then walked by both proofs.

READ_ROUTES: dict[str, str] = {
    "/api/runs": "/api/runs",
    "/api/runs/{id}": "/api/runs/{run_id}",
    "/api/runs/{id}/status": "/api/runs/{run_id}/status",
    "/api/runs/{id}/exceptions": "/api/runs/{run_id}/exceptions?page=1&size=5",
    "/api/runs/{id}/matches": "/api/runs/{run_id}/matches?page=1&size=5",
    "/api/runs/{id}/records": "/api/runs/{run_id}/records?source=order&page=1&size=5",
    "/api/runs/{id}/settlements": "/api/runs/{run_id}/settlements?page=1&size=5",
    "/api/runs/{id}/batches/{settlement_id}": (
        "/api/runs/{run_id}/batches/{settlement_id}"
    ),
    "/api/runs/{id}/drift": "/api/runs/{run_id}/drift",
    "/api/exceptions/{id}": "/api/exceptions/{exception_id}",
    "/api/matches/{id}": "/api/matches/{match_id}",
    "/api/uploads": "/api/uploads",
    "/api/uploads/{id}": "/api/uploads/{upload_id}",
    "/api/uploads/{id}/quarantine": (
        "/api/uploads/{upload_id}/quarantine?page=1&size=5"
    ),
}

#: The three writes. Walked by the disabled-surface proof (they must still work
#: with no auth configured) and, for `POST /api/runs`, by the tenancy proof --
#: a run created under one org must not be startable from another.
#:
#: `POST /api/uploads` is a write of a merchant's own file, and its tenancy
#: property is stronger than "the row is filed under the caller's org": the
#: upload is keyed on content, so two orgs uploading the SAME bytes must get
#: two uploads rather than one shared id.
#: `test_two_orgs_uploading_the_same_file_hold_two_uploads` is that proof.
WRITE_ROUTES = ("/api/datasets/generate", "/api/runs", "/api/uploads")

#: Reachable without a session by design; excluded from both walks.
AUTH_ROUTES = ("/api/auth/login", "/api/auth/logout")


def contract_operations() -> list[tuple[str, str]]:
    """`(method, path)` for every operation `api/openapi.yaml` declares."""
    spec = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    methods = ("get", "post", "put", "patch", "delete")
    return [
        (method, path)
        for path, item in spec["paths"].items()
        for method in item
        if method.lower() in methods
    ]


def test_the_route_table_covers_every_read_operation_in_the_contract():
    """The guard that makes this file impossible to bypass.

    Add a `GET` to `api/openapi.yaml` and this fails until the path is listed
    in `READ_ROUTES` -- at which point the isolation proof below walks it, and
    an endpoint that leaks another org's rows cannot ship green.
    """
    declared = {
        path
        for method, path in contract_operations()
        if method == "get" and path not in AUTH_ROUTES
    }
    assert declared == set(READ_ROUTES), (
        "api/openapi.yaml declares GET operations this file does not walk: "
        f"{sorted(declared - set(READ_ROUTES))}. Add them to READ_ROUTES; "
        "every read of financial data has to be proved org-scoped."
    )


def test_the_route_table_covers_every_write_operation_in_the_contract():
    declared = {
        path
        for method, path in contract_operations()
        if method == "post" and path not in AUTH_ROUTES
    }
    assert declared == set(WRITE_ROUTES), sorted(declared)


# --- fixtures -----------------------------------------------------------------


def generate(client: TestClient, *, seed: int = 42, count: int = 50) -> str:
    dataset = client.post(
        "/api/datasets/generate", json={"seed": seed, "record_count": count}
    )
    assert dataset.status_code == 200, dataset.text
    return dataset.json()["dataset_id"]


def run_to_completion(client: TestClient, dataset_id: str) -> str:
    created = client.post(
        "/api/runs", json={"dataset_id": dataset_id, "use_llm": False}
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    for _ in range(200):
        state = client.get(f"/api/runs/{run_id}/status").json()["state"]
        if state in ("completed", "failed"):
            break
    assert state == "completed"
    return run_id


def seed_two_runs(client: TestClient, *, seed: int = 42, count: int = 50) -> str:
    """Two completed runs over ONE dataset, and the id of the second.

    Two, because `GET /api/runs/{id}/drift` with no `?against` needs an earlier
    completed run on the same dataset and correctly 404s without one -- so a
    walk over a single run would exercise the endpoint's missing-baseline
    branch rather than the endpoint.
    """
    dataset_id = generate(client, seed=seed, count=count)
    run_to_completion(client, dataset_id)
    return run_to_completion(client, dataset_id)


def upload(client: TestClient, path: Path) -> str:
    """Ingest one fixture file and return its upload id."""
    with open(path, "rb") as handle:
        response = client.post(
            "/api/uploads", files={"file": (path.name, handle, "text/csv")}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quarantine_count"] > 0, (
        f"{path.name} quarantined nothing, so the quarantine walk below would "
        "read an empty page and prove nothing"
    )
    return body["upload_id"]


def ids_for(client: TestClient, run_id: str) -> dict[str, str]:
    """The concrete ids the table's placeholders need, read back off the API.

    Read back rather than constructed: an id this test invented would not
    prove much about a filter that keys on ids the store actually holds.
    """
    exceptions = client.get(f"/api/runs/{run_id}/exceptions?page=1&size=1").json()
    matches = client.get(f"/api/runs/{run_id}/matches?page=1&size=1").json()
    assert exceptions["items"] and matches["items"], (
        "the fixture dataset must produce at least one exception and one "
        "match, or the walk below would pass vacuously"
    )
    return {
        "run_id": run_id,
        # Taken from the MATCH, not from the settlements listing: that listing
        # includes the batches nothing closed, and
        # `GET /api/runs/{id}/batches/{sid}` correctly 404s for one of those --
        # which would make the walk assert a 404 it had itself arranged.
        "settlement_id": matches["items"][0]["settlement_id"],
        "exception_id": exceptions["items"][0]["exception_id"],
        "match_id": matches["items"][0]["match_id"],
    }


@pytest.fixture(scope="module")
def disabled_surface(tmp_path_factory):
    """One completed run on an API with no auth configured, plus its ids.

    Module-scoped: the walk is a read-only sweep over one run, and generating
    a dataset per parametrised case would multiply a two-second setup by
    thirteen for no additional coverage.
    """
    import os

    tmp = tmp_path_factory.mktemp("disabled")
    previous = {
        name: os.environ.get(name)
        for name in (
            "RECON_DB_PATH",
            "RECON_DATASETS_DIR",
            "RECON_UPLOADS_DIR",
            settings.AUTH_ENV,
            settings.SECRET_KEY_ENV,
            settings.USERS_ENV,
        )
    }
    os.environ["RECON_DB_PATH"] = str(tmp / "recon.db")
    os.environ["RECON_DATASETS_DIR"] = str(tmp / "datasets")
    os.environ["RECON_UPLOADS_DIR"] = str(tmp / "uploads")
    for name in (settings.AUTH_ENV, settings.SECRET_KEY_ENV, settings.USERS_ENV):
        os.environ.pop(name, None)
    try:
        from api.main import create_app

        with TestClient(create_app()) as client:
            ids = ids_for(client, seed_two_runs(client))
            ids["upload_id"] = upload(client, UPLOAD_FOR_A)
            yield client, ids
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# --- item 1: with auth disabled the surface is unchanged ----------------------


@pytest.mark.parametrize("path", sorted(READ_ROUTES), ids=lambda p: p)
def test_every_read_answers_with_no_authentication_configured(
    disabled_surface, path
):
    """The compatibility contract, asserted endpoint by endpoint.

    Not "the API starts" and not "one endpoint works": the console on `main`
    calls all of these, and a 401 from any one of them is a broken demo.
    """
    client, ids = disabled_surface
    response = client.get(READ_ROUTES[path].format(**ids))
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"


def test_no_read_is_answered_with_an_auth_status_when_auth_is_disabled(
    disabled_surface,
):
    """The same sweep, stated as the negative it is really about.

    A 401 or a 403 anywhere on this surface with no auth configured is the
    regression this whole phase has to not cause.
    """
    client, ids = disabled_surface
    offenders = [
        (path, client.get(template.format(**ids)).status_code)
        for path, template in sorted(READ_ROUTES.items())
    ]
    assert not [p for p, code in offenders if code in (401, 403)], offenders


def test_every_write_still_works_with_no_authentication_configured(
    disabled_surface,
):
    client, _ids = disabled_surface
    dataset = client.post(
        "/api/datasets/generate", json={"seed": 7, "record_count": 50}
    )
    assert dataset.status_code == 200, dataset.text
    created = client.post(
        "/api/runs",
        json={"dataset_id": dataset.json()["dataset_id"], "use_llm": False},
    )
    assert created.status_code == 202, created.text
    # The third write. A different file from the fixture's, so this exercises
    # a first ingest rather than the already-ingested branch.
    with open(UPLOAD_FOR_B, "rb") as handle:
        ingested = client.post(
            "/api/uploads",
            files={"file": (UPLOAD_FOR_B.name, handle, "text/csv")},
        )
    assert ingested.status_code == 200, ingested.text


def test_no_response_carries_a_session_cookie_when_auth_is_disabled(disabled_surface):
    """Nothing about the disabled path may start setting cookies: a console
    that suddenly receives one would begin sending it, and `allow_credentials`
    is `False` in this configuration."""
    client, ids = disabled_surface
    for template in READ_ROUTES.values():
        response = client.get(template.format(**ids))
        assert auth.SESSION_COOKIE not in response.cookies
        assert "set-cookie" not in {k.lower() for k in response.headers}


# --- item 2: with auth enabled, two orgs cannot see each other ---------------


def all_ids(client: TestClient, run_id: str) -> dict[str, set[str]]:
    """Every id a run holds, per kind, for the set differences below.

    `matched_settlement` is the subset of settlements a `MatchGroup` closed:
    `GET /api/runs/{id}/batches/{sid}` correctly 404s for a batch nothing
    closed, so the control test needs an id from this narrower set.
    """
    page = "?page=1&size=1000"
    exceptions = client.get(f"/api/runs/{run_id}/exceptions{page}").json()
    matches = client.get(f"/api/runs/{run_id}/matches{page}").json()
    settlements = client.get(f"/api/runs/{run_id}/settlements{page}").json()
    return {
        "exception_id": {item["exception_id"] for item in exceptions["items"]},
        "match_id": {item["match_id"] for item in matches["items"]},
        "settlement_id": {item["settlement_id"] for item in settlements["items"]},
        "matched_settlement": {
            item["settlement_id"] for item in matches["items"]
        },
    }


@pytest.fixture(scope="module")
def two_orgs(tmp_path_factory):
    """One database, one app, two logged-in clients, two ingested datasets.

    Two `TestClient`s over one app rather than two apps: they share the
    database, the engine and the process, which is the situation the filter has
    to be right in. Separate clients only so they hold separate cookie jars.

    **The two orgs ingest datasets of different sizes, and that is load-bearing
    for the walk below.** `exception_id` is minted by the engine as
    `exc-<subject_id>` and `subject_id` is positional within a dataset, so two
    orgs that ingested datasets of the same size hold *identically named* rows.
    That is a real property of the system and not a defect -- ids are unique
    within a run, never globally -- but it makes "org B asked for org A's
    exception" unfalsifiable: B would be handed its own row of the same name
    and a 200 would prove nothing either way.

    So org A runs the larger dataset, and the walk addresses it by ids that
    exist in A's run and in no run of B's. The fixture computes that difference
    rather than assuming it, and asserts it is non-empty -- a walk over ids
    that turned out to be shared would pass vacuously.
    """
    import os

    tmp = tmp_path_factory.mktemp("tenancy")
    names = (
        "RECON_DB_PATH",
        "RECON_DATASETS_DIR",
        "RECON_UPLOADS_DIR",
        settings.AUTH_ENV,
        settings.SECRET_KEY_ENV,
        settings.USERS_ENV,
    )
    previous = {name: os.environ.get(name) for name in names}
    os.environ["RECON_DB_PATH"] = str(tmp / "recon.db")
    os.environ["RECON_DATASETS_DIR"] = str(tmp / "datasets")
    os.environ["RECON_UPLOADS_DIR"] = str(tmp / "uploads")
    os.environ[settings.AUTH_ENV] = "enabled"
    os.environ[settings.SECRET_KEY_ENV] = SECRET
    verifier = auth.password_hash(PASSWORD)
    os.environ[settings.USERS_ENV] = ",".join(
        (f"{USER_A}|{ORG_A}|{verifier}", f"{USER_B}|{ORG_B}|{verifier}")
    )
    try:
        from api.main import create_app

        app = create_app()
        with TestClient(app) as client_a, TestClient(app) as client_b:
            for client, user in ((client_a, USER_A), (client_b, USER_B)):
                login = client.post(
                    "/api/auth/login", json={"email": user, "password": PASSWORD}
                )
                assert login.status_code == 200, login.text

            a_run = seed_two_runs(client_a, seed=42, count=200)
            b_run = seed_two_runs(client_b, seed=7, count=50)

            held_by_a = all_ids(client_a, a_run)
            held_by_b = all_ids(client_b, b_run)
            only_a = {
                "exception_id": held_by_a["exception_id"] - held_by_b["exception_id"],
                "match_id": held_by_a["match_id"] - held_by_b["match_id"],
                # Matched in A, and absent from B's settlements entirely --
                # not merely from B's *matched* ones, or an id could still be
                # legitimately present in a body B is entitled to.
                "settlement_id": (
                    held_by_a["matched_settlement"] - held_by_b["settlement_id"]
                ),
            }
            missing = sorted(kind for kind, ids in only_a.items() if not ids)
            assert not missing, (
                f"org A holds no {missing} that org B does not. The cross-org "
                "walk cannot then distinguish 'refused' from 'handed its own "
                "row of the same name', and the leak check below would flag an "
                "id org B is entitled to. Widen the gap between the two "
                "datasets' record counts."
            )

            a_ids = ids_for(client_a, a_run)
            a_ids.update({kind: sorted(ids)[0] for kind, ids in only_a.items()})
            b_ids = ids_for(client_b, b_run)

            # Two different files, so nothing about B's upload can be confused
            # for A's -- not the id, not the format, not a quarantined row.
            a_ids["upload_id"] = upload(client_a, UPLOAD_FOR_A)
            b_ids["upload_id"] = upload(client_b, UPLOAD_FOR_B)

            assert a_ids["run_id"] != b_ids["run_id"]
            assert a_ids["upload_id"] != b_ids["upload_id"]
            yield client_a, a_ids, client_b, b_ids
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


#: What org B must get when it asks for a resource of org A's.
#:
#: Every id-addressed read is a **404, not a 403**: "exists but is not yours"
#: and "does not exist" have to be indistinguishable, or the API becomes an
#: oracle for which ids other tenants hold, answerable by anyone willing to
#: enumerate. `/api/runs` is a listing and answers 200 -- with none of A's rows
#: in it, which the next test asserts on the bytes.
CROSS_ORG_EXPECTATION = {path: 404 for path in READ_ROUTES}
CROSS_ORG_EXPECTATION["/api/runs"] = 200
#: A listing, like `/api/runs`, and answered the same way: 200 with none of the
#: other org's rows in it. The next test asserts that on the bytes.
CROSS_ORG_EXPECTATION["/api/uploads"] = 200


@pytest.mark.parametrize("path", sorted(READ_ROUTES), ids=lambda p: p)
def test_org_b_cannot_read_org_a_through_any_endpoint(two_orgs, path):
    """The isolation proof, walked over the whole contract.

    Parametrised over the same table that
    `test_the_route_table_covers_every_read_operation_in_the_contract` pins to
    `api/openapi.yaml`, so an endpoint added tomorrow is walked here or the
    suite goes red.
    """
    _client_a, a_ids, client_b, _b_ids = two_orgs
    response = client_b.get(READ_ROUTES[path].format(**a_ids))
    assert response.status_code == CROSS_ORG_EXPECTATION[path], (
        f"{path} answered {response.status_code} for another org's resource: "
        f"{response.text[:200]}"
    )


@pytest.mark.parametrize("path", sorted(READ_ROUTES), ids=lambda p: p)
def test_no_response_to_org_b_contains_an_identifier_belonging_to_org_a(
    two_orgs, path
):
    """Status codes are not enough.

    A listing that returned org A's rows under org B's totals would satisfy the
    check above on `/api/runs` and still be a total tenancy failure. This reads
    org B's **own** resources -- the ordinary, authorised request -- and asserts
    that none of org A's identifiers appears anywhere in the bytes.

    Org B's own resources rather than org A's, deliberately: a 404 body echoes
    the id the caller asked for, and an echo of the caller's own input is not a
    disclosure. The request here contains nothing of A's, so anything of A's in
    the response is a leak with no other explanation.
    """
    _client_a, a_ids, client_b, b_ids = two_orgs
    body = client_b.get(READ_ROUTES[path].format(**b_ids)).text
    leaked = sorted({value for value in a_ids.values() if value in body})
    assert not leaked, f"{path} leaked org A identifiers to org B: {leaked}"


@pytest.mark.parametrize("path", sorted(READ_ROUTES), ids=lambda p: p)
def test_org_a_still_reads_its_own_data(two_orgs, path):
    """The control, and it is not optional.

    A repository that returned nothing to everybody would pass every assertion
    above. This is what separates "isolated" from "broken".
    """
    client_a, a_ids, _client_b, _b_ids = two_orgs
    response = client_a.get(READ_ROUTES[path].format(**a_ids))
    assert response.status_code == 200, response.text


def test_the_run_listing_shows_each_org_only_its_own_runs(two_orgs):
    client_a, a_ids, client_b, b_ids = two_orgs
    a_runs = {run["run_id"] for run in client_a.get("/api/runs").json()}
    b_runs = {run["run_id"] for run in client_b.get("/api/runs").json()}
    assert a_ids["run_id"] in a_runs and b_ids["run_id"] in b_runs
    assert not a_runs & b_runs
    # Each org ran its dataset twice, so neither listing is empty and neither
    # is the whole table -- the two states in which a filter bug hides.
    assert len(a_runs) == len(b_runs) == 2


def test_two_orgs_that_ingest_the_same_dataset_still_see_only_their_own_rows(
    two_orgs,
):
    """The brief's case, stated exactly: **the same data, ingested twice.**

    The generated datasets live on disk and are shared demo artefacts, not
    tenant data -- so org B can run the very dataset org A ran. What must not
    be shared is what the *run* produced, and after this both orgs hold rows
    that are equal field for field and belong to different tenants.
    """
    client_a, a_ids, client_b, _b_ids = two_orgs
    shared = client_a.get(f"/api/runs/{a_ids['run_id']}").json()
    assert shared["record_count"] == 200

    dataset_id = generate(client_a, seed=42, count=200)
    b_run = run_to_completion(client_b, dataset_id)

    mine = client_b.get(f"/api/runs/{b_run}").json()
    theirs = client_a.get(f"/api/runs/{a_ids['run_id']}").json()
    # Identical inputs and a deterministic engine: same counts, same metrics.
    assert mine["record_count"] == theirs["record_count"]
    assert mine["match_count"] == theirs["match_count"]
    assert mine["exception_count"] == theirs["exception_count"]
    # ... and still two separate tenants' rows.
    assert client_a.get(f"/api/runs/{b_run}").status_code == 404
    assert client_b.get(f"/api/runs/{a_ids['run_id']}").status_code == 404
    assert client_a.get(f"/api/runs/{b_run}/exceptions").status_code == 404
    assert client_b.get(f"/api/runs/{a_ids['run_id']}/exceptions").status_code == 404


def test_two_orgs_uploading_the_same_file_hold_two_uploads(two_orgs):
    """The tenancy question content addressing creates, answered explicitly.

    `POST /api/uploads` is idempotent by content hash: the same bytes return
    the same upload id. That is exactly right within an org and would be a
    disclosure across one -- org B uploading a file and being handed org A's
    upload id would tell B that A holds that document, and would then serve B
    A's quarantine rows.

    So the dedup key is `(org_id, content_sha256)` and not the hash alone. Both
    orgs upload the SAME bytes here; both get their own upload, neither can
    read the other's, and each is still idempotent against itself.
    """
    client_a, _a_ids, client_b, _b_ids = two_orgs
    payload = UPLOAD_FOR_A.read_bytes()

    def send(client):
        return client.post(
            "/api/uploads", files={"file": ("shared.csv", payload, "text/csv")}
        ).json()

    mine, theirs = send(client_a), send(client_b)
    assert mine["upload_id"] != theirs["upload_id"]
    assert mine["content_sha256"] == theirs["content_sha256"]
    # ... and neither org can reach the other's.
    assert client_b.get(f"/api/uploads/{mine['upload_id']}").status_code == 404
    assert client_a.get(f"/api/uploads/{theirs['upload_id']}").status_code == 404
    assert (
        client_b.get(
            f"/api/uploads/{mine['upload_id']}/quarantine"
        ).status_code
        == 404
    )
    # ... while each is still idempotent against itself.
    assert send(client_a)["upload_id"] == mine["upload_id"]
    assert send(client_a)["already_ingested"] is True


def test_a_run_over_another_orgs_uploads_is_refused(two_orgs):
    """`upload_ids` is the second place a caller supplies an id of its own
    choosing -- `?against=` on the drift endpoint being the first. It resolves
    through the same org-scoped `Repo.upload`, so another tenant's upload is
    simply not found and no run is created."""
    client_a, a_ids, _client_b, b_ids = two_orgs
    response = client_a.post(
        "/api/runs", json={"upload_ids": [b_ids["upload_id"]], "use_llm": False}
    )
    assert response.status_code == 404
    assert b_ids["upload_id"] in response.json()["detail"]

    before = {run["run_id"] for run in client_a.get("/api/runs").json()}
    client_a.post(
        "/api/runs",
        json={
            "upload_ids": [a_ids["upload_id"], b_ids["upload_id"]],
            "use_llm": False,
        },
    )
    after = {run["run_id"] for run in client_a.get("/api/runs").json()}
    assert before == after, (
        "a run was created despite one of its uploads belonging to another "
        "org; the refusal has to happen before create_run, or the merchant "
        "gets a run over silently fewer files than they selected"
    )


def test_a_run_created_by_one_org_is_not_visible_to_the_other(two_orgs):
    """The write side. A run is filed under the org that created it, and that
    org comes from the session -- there is no field in `CreateRunRequest` that
    could say otherwise."""
    client_a, _a_ids, client_b, _b_ids = two_orgs
    fresh = run_to_completion(client_b, generate(client_b, seed=99))
    assert client_b.get(f"/api/runs/{fresh}").status_code == 200
    assert client_a.get(f"/api/runs/{fresh}").status_code == 404
    assert client_a.get(f"/api/runs/{fresh}/status").status_code == 404


def test_the_drift_endpoint_will_not_compare_across_orgs(two_orgs):
    """`?against=` takes a run id from the caller, which makes it the one read
    where a cross-org reference is *supplied* rather than guessed. It resolves
    through the same org-scoped `summary()` as everything else, so the baseline
    is simply not found."""
    client_a, a_ids, _client_b, b_ids = two_orgs
    mine, theirs = a_ids["run_id"], b_ids["run_id"]
    response = client_a.get(f"/api/runs/{mine}/drift?against={theirs}")
    assert response.status_code == 404
    # The id the caller asked for is echoed back -- it is their own input.
    # Nothing about org B's run is.
    assert theirs in response.json()["detail"]
    assert "record_count" not in response.text


# --- the enforcement point ----------------------------------------------------


def test_no_route_handler_mentions_an_org_id():
    """`api/routes.py` must not contain the identifier at all.

    The rule is "route handlers never pass org ids from request bodies", and
    the strongest mechanical form of it is that the name does not occur in the
    module -- no parameter, no body field, no query string, no keyword argument
    to a repository call. The org reaches the store through
    `api/deps.get_repo` and the session principal, and through nothing else.
    The two `/api/auth/` handlers live in `api/auth.py` precisely so this rule
    can have no exceptions: the login response legitimately names an org.

    Checked over the AST rather than the raw text so that a comment or a
    docstring explaining the rule cannot trip the check that enforces it -- the
    same discipline `tests/test_boundaries.py` applies to its own string scan.
    """
    import ast

    tree = ast.parse((REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "org_id":
            offenders.append(f"name at line {node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr == "org_id":
            offenders.append(f"attribute at line {node.lineno}")
        elif isinstance(node, ast.arg) and node.arg == "org_id":
            offenders.append(f"parameter at line {node.lineno}")
        elif isinstance(node, ast.keyword) and node.arg == "org_id":
            offenders.append(f"keyword at line {node.lineno}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and "org_id" in node.value
        ):
            offenders.append(f"string literal at line {node.lineno}")
    assert not offenders, (
        "api/routes.py mentions org_id: "
        + ", ".join(offenders)
        + ". The org comes from the session via api/deps.get_repo and nowhere else."
    )


def test_no_repository_method_takes_an_org_id():
    """The other half: `Repo` is *bound* to an org, it does not accept one.

    A method that took an org argument would let a caller above this layer name
    a tenant, which is precisely the design being avoided -- the argument would
    eventually be threaded up into a route and then into a request body, and
    the enforcement point would have moved to the layer least able to hold it.
    """
    import inspect

    from core.store.repo import Repo

    offenders = [
        f"Repo.{name}"
        for name, member in inspect.getmembers(Repo, inspect.isfunction)
        if "org_id" in inspect.signature(member).parameters
        and name not in ("__init__", "scoped")
    ]
    assert not offenders, offenders
