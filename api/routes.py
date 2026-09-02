"""The route handlers for every operation `api/openapi.yaml` declares.

Every handler does the same four things and nothing else: validate the request,
call `core/`, translate a missing row into a 404, and serialise the model the
contract names. There is no arithmetic in this file. `net`, `fees`, `tax` and
every rate arrive already computed from `core/matcher/` and `scorer/`, and are
passed through untouched -- a "helpful" recomputation in a route is
indistinguishable from an engine bug on the screen (LANE-D-api.md 5.1, 7).

Responses are serialised with `model_dump(mode="json")` and declared
`response_model=None`. That is deliberate: the response shapes are unions
(`SubjectRecord` is one of three record types) and letting FastAPI re-validate
through an inferred response model risks it picking a different branch and
dropping fields. The models are already validated -- they came out of pydantic --
so the router's job is to hand them over, and `tests/api/test_routes.py` checks
each body against `api/openapi.yaml` itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.drift.compare import compare
from core.models import MatchGroup, ReasonCode, TierConfidence
from core.store.repo import RecordSource, Repo

from api import settings
from api.auth import require_principal
from api.deps import get_repo
from api.ingest import (
    MAX_UPLOAD_BYTES,
    UploadRefused,
    UploadTooLarge,
    ingest_upload,
)
from api.jobs import (
    UnknownDefectType,
    execute_run,
    execute_run_over_uploads,
    generate_dataset,
    dataset_facts,
    resolve_dataset,
    utc_now,
)

#: **Every data route is mounted behind `require_principal`**, on the router
#: rather than on the handlers. Two consequences, both deliberate:
#:
#: * a route added tomorrow is authenticated by having been added at all --
#:   there is no per-handler decorator for a future author to forget;
#: * with `RECON_AUTH` disabled the dependency resolves to the single-user
#:   principal and costs one environment read, so the demo path is unchanged.
#:
#: The org id travels from that principal into `get_repo`, and from there into
#: every query. **It never appears in this module** -- no handler reads one, no
#: request body carries one, and `tests/api/test_tenancy.py` asserts the name
#: does not occur in this file's source at all.
router = APIRouter(dependencies=[Depends(require_principal)])

RepoDep = Annotated[Repo, Depends(get_repo)]


# --- request bodies -----------------------------------------------------------


class GenerateDatasetRequest(BaseModel):
    seed: int
    record_count: int
    #: OPTIONAL, and **not defaulted here**. Absent and explicit null both mean
    #: `None`, which is passed straight to the generator so its own
    #: `DEFAULT_DEFECT_MIX` applies. The new-run dialog (spec 13 #1) offers seed,
    #: record count and LLM on/off and nothing else, so a required `defect_mix`
    #: would be a field no client could populate.
    defect_mix: dict[str, int] | None = None


class CreateRunRequest(BaseModel):
    """One run, over exactly one of two sources.

    **Both fields are optional and exactly one must be given.** They are not a
    tagged union with a `source` discriminator because a discriminator would be
    a third field a client could set inconsistently with the other two; here
    the presence of the id *is* the choice, and the handler refuses both and
    neither with a message naming what it got.

    `upload_ids` is a set in meaning and a list on the wire: order does not
    affect the run (`Repo.upload_inputs` sorts), and a repeated id contributes
    its records once.
    """

    dataset_id: str | None = None
    upload_ids: list[str] | None = None
    use_llm: bool


# --- datasets -----------------------------------------------------------------


@router.post("/api/datasets/generate", response_model=None, tags=["datasets"])
def generate_dataset_endpoint(body: GenerateDatasetRequest) -> dict:
    """Generate a synthetic adversarial dataset. The strongest demo beat.

    Run synchronously: the contract answers 200 with the id, not 202, and 500
    records generate in well under a second. The failure mode is kept legible --
    an unknown defect name is a 422 naming it and the known set, never a 500.
    """
    try:
        dataset_id = generate_dataset(
            settings.datasets_dir(),
            seed=body.seed,
            record_count=body.record_count,
            defect_mix=body.defect_mix,
        )
    except (UnknownDefectType, ValueError) as exc:
        # `build_dataset` owns the validity rules for seed/count; surfacing its
        # message keeps one copy of them rather than restating them here.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"dataset_id": dataset_id}


# --- runs ---------------------------------------------------------------------


#: `RunSummary.seed` for a run whose inputs a merchant uploaded.
#:
#: There is no seed: nothing generated these records. `RunSummary` is frozen
#: and `seed` is a non-nullable integer on it, so the choice is between a value
#: that reads as a real seed and one that cannot. **-1 is not a seed any caller
#: could have supplied** -- the generator refuses negatives -- so a client can
#: test for it and say "run from uploaded files" instead of printing "Seed 0",
#: which is a sentence about an experiment that did not happen.
UNSEEDED_RUN = -1


@router.post("/api/runs", status_code=202, response_model=None, tags=["runs"])
def create_run(
    body: CreateRunRequest, background: BackgroundTasks, repo: RepoDep
) -> dict:
    """Queue a reconciliation run and return immediately.

    Two sources, one run. `dataset_id` reconciles a generated dataset from
    disk; `upload_ids` reconciles the canonical records a merchant's own files
    produced. **Below this handler they are the same job** -- see
    `api/jobs._execute` -- which is what makes "the console showed you a demo
    and your files take a different path" untrue by construction.

    `created_at` is stamped **here**, once, and persisted: the global constraint
    forbids a wall-clock inside `core/`, and `RunSummary.created_at` deliberately
    has no default so nothing downstream can quietly acquire one.
    """
    if (body.dataset_id is None) == (body.upload_ids is None):
        raise HTTPException(
            status_code=422,
            detail=(
                "a run reconciles exactly one source: give either dataset_id "
                "or upload_ids, and not both"
            ),
        )

    if body.upload_ids is not None:
        return _run_over_uploads(body, background, repo)

    directory = resolve_dataset(settings.datasets_dir(), body.dataset_id)
    if directory is None:
        raise HTTPException(
            status_code=404, detail=f"no dataset with id {body.dataset_id!r}"
        )

    seed, record_count = dataset_facts(directory)
    run_id = repo.create_run(
        seed=seed,
        record_count=record_count,
        created_at=utc_now(),
        dataset_id=body.dataset_id,
    )
    background.add_task(execute_run, repo, run_id, directory, use_llm=body.use_llm)
    return {"run_id": run_id}


def _run_over_uploads(
    body: CreateRunRequest, background: BackgroundTasks, repo: RepoDep
) -> dict:
    """The upload branch of `create_run`.

    Every id is resolved before the run is created, and an unknown one is a
    404 naming it rather than a run that quietly reconciles a smaller set than
    the merchant selected. An id belonging to another tenant is unknown here
    for the same reason it is unknown everywhere else: the repository is bound
    to one org.

    `record_count` is the number of ORDERS across the selected uploads, which
    is what the field means on the dataset path too -- `truth.json`'s
    `record_count` is the order count the generator was asked for. Counting
    every canonical row instead would make the same data report a different
    size depending on which door it came through.
    """
    requested = list(dict.fromkeys(body.upload_ids or []))
    if not requested:
        raise HTTPException(
            status_code=422,
            detail="upload_ids is empty: a run needs at least one upload to read",
        )

    uploads = []
    for upload_id in requested:
        found = repo.upload(upload_id)
        if found is None:
            raise HTTPException(
                status_code=404, detail=f"no upload with id {upload_id!r}"
            )
        uploads.append(found)

    if not any(upload.record_count for upload in uploads):
        raise HTTPException(
            status_code=422,
            detail=(
                "the selected uploads hold no canonical records between them, "
                "so there is nothing to reconcile. Every row of "
                f"{[u.filename for u in uploads]} was quarantined or the files "
                "were empty; review the quarantine before starting a run."
            ),
        )

    run_id = repo.create_run(
        seed=UNSEEDED_RUN,
        record_count=sum(upload.order_count for upload in uploads),
        created_at=utc_now(),
        # No dataset. The column stays null, which is also what makes the drift
        # endpoint's default baseline -- "the previous completed run on this
        # run's dataset" -- correctly find nothing for an upload run.
        dataset_id=None,
    )
    background.add_task(
        execute_run_over_uploads, repo, run_id, requested, use_llm=body.use_llm
    )
    return {"run_id": run_id}


@router.get("/api/runs", response_model=None, tags=["runs"])
def list_runs(repo: RepoDep) -> list[dict]:
    """Run history, most recent first."""
    return [summary.model_dump(mode="json") for summary in repo.list_runs()]


#: Every tier key, always, for the same reason `tier_counts` carries all five:
#: a missing key reads as "we do not know", which is a different claim from
#: "this rung produced nothing".
_TIER_KEYS = ("T0", "T1", "T2", "T3", "LLM")


def tier_confidence_map(
    matches: Sequence[MatchGroup],
) -> dict[str, TierConfidence]:
    """The confidence the engine stamped, per tier, for THIS run.

    Derived from the matches rather than read off a table, so a change in the
    engine's stamp reaches the console without anybody remembering to edit a
    constant. `web/lib/tiers.ts` used to hold that constant and drifted: it
    rendered the word "verified" on the LLM rung, where the engine stamps 0.70.

    A tier that stamped two different confidences reports NEITHER. One of them
    would be a true statement about some of its matches and a false statement
    about the rest, and nothing on the wire would say which.
    """
    seen: dict[str, set[float]] = {key: set() for key in _TIER_KEYS}
    for match in matches:
        if match.tier in seen:
            seen[match.tier].add(match.confidence)

    out: dict[str, TierConfidence] = {}
    for key in _TIER_KEYS:
        values = seen[key]
        out[key] = TierConfidence(
            confidence_observed=next(iter(values)) if len(values) == 1 else None,
            confidence_conflict=len(values) > 1,
        )
    return out


@router.get("/api/runs/{id}", response_model=None, tags=["runs"])
def get_run(id: str, repo: RepoDep) -> dict:
    """The full `RunSummary`, including `false_match_rate` and
    `trap_capture_rate` -- the two numbers that make the headline honest, and
    the two this lane may never drop."""
    summary = repo.summary(id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"no run with id {id!r}")
    # Set on the MODEL, not on the dumped dict: `RunSummary` and
    # `api/openapi.yaml` are held to each other by
    # `tests/test_models.py::test_openapi_mirrors_the_pydantic_model`, and a key
    # that exists only on the response would pass every test here while making
    # the published contract a lie.
    #
    # Derived per request rather than stored, so it is always this run's own
    # figure. The console used to carry a hardcoded table instead and it
    # drifted -- rendering "verified" on the rung where the engine stamps 0.70.
    enriched = summary.model_copy(
        update={"tier_confidence": tier_confidence_map(repo.all_matches(id))}
    )
    return enriched.model_dump(mode="json")


@router.get("/api/runs/{id}/status", response_model=None, tags=["runs"])
def get_run_status(id: str, repo: RepoDep) -> dict:
    """The polled endpoint. One indexed row, no joins -- Lane E hits it at 500 ms."""
    status = repo.status(id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"no run with id {id!r}")
    return status.model_dump(mode="json")


@router.get("/api/runs/{id}/exceptions", response_model=None, tags=["runs", "exceptions"])
def list_run_exceptions(
    id: str,
    repo: RepoDep,
    reason_code: Annotated[ReasonCode | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1)] = 50,
) -> dict:
    """One page of exceptions, each with its subject record and **no audit trail**.

    The trail is deliberately absent: this response pages over as many as 5,000
    rows and almost none of them are opened, so the trail is fetched once, for
    the row a reviewer actually clicks, from `GET /api/exceptions/{id}`.

    Ordering is `exception_id`, so page 2 can never repeat a row from page 1.
    """
    if not repo.run_exists(id):
        raise HTTPException(status_code=404, detail=f"no run with id {id!r}")
    result = repo.exceptions_page(
        id,
        page=page,
        size=size,
        reason_code=None if reason_code is None else reason_code.value,
    )
    return result.model_dump(mode="json")


@router.get("/api/runs/{id}/matches", response_model=None, tags=["runs", "matches"])
def list_run_matches(
    id: str,
    repo: RepoDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1)] = 50,
) -> dict:
    """One page of the run's accepted matches, with their tiers and evidence.

    The counterpart of the exceptions list, and it exists for the same reason
    that list does: a console that shows only what failed lets a reviewer check
    the failures and take the successes on trust, when the successes are what
    the match rate is made of.

    Ordering is `match_id`. **No audit trail and no subject record** -- both live
    on `GET /api/matches/{id}`, fetched for the row a reviewer opens.
    """
    if not repo.run_exists(id):
        raise HTTPException(status_code=404, detail=f"no run with id {id!r}")
    return repo.matches_page(id, page=page, size=size).model_dump(mode="json")


@router.get("/api/runs/{id}/records", response_model=None, tags=["runs", "records"])
def list_run_records(
    id: str,
    repo: RepoDep,
    source: Annotated[RecordSource, Query()],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1)] = 50,
) -> dict:
    """One page of the rows the engine read, of one source.

    Every other endpoint hands a record over attached to a verdict -- an
    exception's subject, a match's bank line. A row that matched cleanly and was
    never excepted therefore had no way to reach the screen at all, which made
    the ingested data the one thing the console could not show.

    `source` is **required**, and a missing or misspelled one is a 422 naming
    the three legal values rather than an empty page: "no such source" and "this
    run has no orders" are different facts and an empty table reads as the
    second. Ordering is `record_id` within the source, so page 2 can never
    repeat a row from page 1.
    """
    if not repo.run_exists(id):
        raise HTTPException(status_code=404, detail=f"no run with id {id!r}")
    return repo.records_page(id, source, page=page, size=size).model_dump(mode="json")


@router.get(
    "/api/runs/{id}/settlements", response_model=None, tags=["runs", "settlements"]
)
def list_run_settlements(
    id: str,
    repo: RepoDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1)] = 50,
) -> dict:
    """One page of settlements: every batch the run saw, matched or not.

    The index for the netting diagram. `GET /api/runs/{id}/batches/{sid}` can
    only be opened for an id the caller already holds, so without this listing a
    reviewer browses the batches they can guess and no others -- and the batches
    that never closed, which are the interesting ones, are unreachable entirely.

    Ordering is `settlement_id`, so page 2 can never repeat a row from page 1,
    and no audit trail is inlined: the same reason as the exceptions list.
    """
    if not repo.run_exists(id):
        raise HTTPException(status_code=404, detail=f"no run with id {id!r}")
    return repo.settlements_page(id, page=page, size=size).model_dump(mode="json")


@router.get(
    "/api/runs/{id}/batches/{settlement_id}", response_model=None, tags=["runs", "batches"]
)
def get_batch_netting(id: str, settlement_id: str, repo: RepoDep) -> dict:
    """The netting breakdown behind the diagram: N orders -> batch -> bank line."""
    if not repo.run_exists(id):
        raise HTTPException(status_code=404, detail=f"no run with id {id!r}")
    netting = repo.batch_netting(id, settlement_id)
    if netting is None:
        raise HTTPException(
            status_code=404,
            detail=f"run {id!r} matched no settlement {settlement_id!r}",
        )
    return netting.model_dump(mode="json")


@router.get("/api/runs/{id}/drift", response_model=None, tags=["runs", "drift"])
def get_run_drift(
    id: str,
    repo: RepoDep,
    against: Annotated[str | None, Query()] = None,
) -> dict:
    """What changed between this run and a baseline, and whether it matters.

    The system reports on one batch when invoked; this is the endpoint that
    makes it compare. A match rate that falls from 98% to 91% because a new
    deduction type appeared is the finding, and the 91% on its own is not.

    Like every other handler here this one does **no arithmetic**. It picks two
    runs, refuses the pairs that cannot be compared, reads the two censuses out
    of the store and hands plain arguments to `core/drift/compare.py`, which
    owns every threshold. `tests/api/test_routes.py::
    test_the_route_computes_nothing_the_comparison_module_computes` checks the
    response body against that function called directly.

    **404** when either run id is unknown, and when no `against` was given and
    the run has no earlier completed run on its dataset -- in that last case
    there is genuinely no drift report to return, and nothing conflicts.

    **409** for the two states that make a comparison meaningless. Both runs
    exist; what is wrong is the pair.

    *Different dataset shapes.* Every rate in `Metrics` is computed over a
    denominator drawn from the run's own subjects and every `itc_*_paise` figure
    is a sum over its own settlements, so a 500-record run and a 5,000-record
    run share no scale on either. §7 calls this "different datasets"; the
    comparable thing is the dataset's **shape**, not its identity. Two seeds at
    500 records are different data over the same shape, and that is exactly the
    comparison drift exists for -- so `record_count` is the test and
    `dataset_id` deliberately is not. Refusing beats silently returning
    nonsense.

    *No metrics.* A run can be `completed` and still carry `metrics: null` -- a
    dataset with no `truth.json` is scored against nothing. That is a conflict
    with the run's state rather than a missing resource, so it is a 409 too,
    with a detail that names which run.

    No model runs here, so `narrative` is always null. Detection never needed
    one: it is a pure function of two `Metrics` and two censuses.
    """
    current = repo.summary(id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"no run with id {id!r}")

    if against is None:
        baseline = repo.previous_completed_run(id)
        if baseline is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"run {id!r} has no earlier completed run on its dataset to "
                    "compare against; pass ?against=<run_id> to choose one"
                ),
            )
    else:
        baseline = repo.summary(against)
        if baseline is None:
            raise HTTPException(status_code=404, detail=f"no run with id {against!r}")

    if baseline.record_count != current.record_count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"run {baseline.run_id!r} ran on {baseline.record_count} records "
                f"and run {current.run_id!r} on {current.record_count}; every rate "
                "is computed over a different denominator and every rupee figure "
                "over a different scale, so the comparison would be meaningless"
            ),
        )

    scoreless = [r.run_id for r in (baseline, current) if r.metrics is None]
    if scoreless:
        raise HTTPException(
            status_code=409,
            detail=(
                f"run(s) {scoreless} carry no metrics (state must be completed "
                "over a dataset with truth.json); there is nothing to compare"
            ),
        )

    report = compare(
        baseline,
        current,
        baseline_metrics=baseline.metrics,
        current_metrics=current.metrics,
        baseline_census=repo.reason_code_census(baseline.run_id),
        current_census=repo.reason_code_census(current.run_id),
        # No model runs on a read. `narrative` is prose over facts already
        # computed and is never an input to `material`, so its absence costs the
        # report nothing a caller can act on.
        narrative=None,
    )
    return report.model_dump(mode="json")


# --- uploads ------------------------------------------------------------------


@router.post("/api/uploads", response_model=None, tags=["uploads"])
def create_upload(repo: RepoDep, file: Annotated[UploadFile, File()]):
    """Ingest one file a merchant actually has.

    The whole of spec 2026-08-30 §3 arrives here: content-addressed storage,
    header-shape detection, canonical records and quarantine, and re-upload
    that is idempotent by content. `api/ingest.py` owns the sequence and why it
    is in that order; this handler validates, calls it once, and renders the
    three outcomes.

    **200 for a new upload and 200 for a re-upload of the same bytes**, told
    apart by `already_ingested`. Not 201-then-200: the id is the same resource
    either way and a client that branched on the status code would be branching
    on a distinction it already has as a field. `already_ingested: true` is
    recognition -- "we have this, here is what it produced" -- and the console
    renders it as such rather than as an error.

    **422 when nothing could read the file.** The body names which of the two
    refusals it was, the threshold, and every candidate format with the score
    it got, so "why did it not read my export" is answerable from the response.
    It quotes no row of the file: quarantine is behind a session and an error
    body is not.

    **413 for a file past the ceiling**, which is a different fact from a file
    nothing recognised and would be a misleading 422.
    """
    payload = file.file.read()
    try:
        ingestion = ingest_upload(
            repo,
            filename=file.filename or "",
            payload=payload,
            uploaded_at=utc_now(),
        )
    except UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except UploadRefused as exc:
        return JSONResponse(status_code=422, content=exc.body())

    return {
        **ingestion.upload.model_dump(mode="json"),
        "already_ingested": ingestion.already_ingested,
    }


@router.get("/api/uploads", response_model=None, tags=["uploads"])
def list_uploads(repo: RepoDep) -> list[dict]:
    """Every file this org has uploaded, most recent first."""
    return [upload.model_dump(mode="json") for upload in repo.list_uploads()]


@router.get("/api/uploads/{id}", response_model=None, tags=["uploads"])
def get_upload(id: str, repo: RepoDep) -> dict:
    """One upload: the format detected, the confidence, and what it produced."""
    upload = repo.upload(id)
    if upload is None:
        raise HTTPException(status_code=404, detail=f"no upload with id {id!r}")
    return upload.model_dump(mode="json")


@router.get("/api/uploads/{id}/quarantine", response_model=None, tags=["uploads"])
def list_upload_quarantine(
    id: str,
    repo: RepoDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1)] = 50,
) -> dict:
    """One page of the rows this upload could not read, raw text included.

    **This is the one endpoint that serves a merchant's own file content back
    verbatim**, which is exactly why the raw line appears here and in no error
    body anywhere else in this API (spec 2026-08-30 §5). It is behind the same
    session every other financial read is behind, and the middleware in
    `api/main.py` writes an `access_log` row for it like any other.

    Ordering is by line number, so paging through a damaged export walks it
    top to bottom and never shows a row twice.
    """
    if not repo.upload_exists(id):
        raise HTTPException(status_code=404, detail=f"no upload with id {id!r}")
    return repo.upload_quarantine_page(id, page=page, size=size).model_dump(
        mode="json"
    )


# --- exceptions and matches ---------------------------------------------------


@router.get("/api/exceptions/{id}", response_model=None, tags=["exceptions"])
def get_exception(id: str, repo: RepoDep) -> dict:
    """One exception, its subject record, and its full audit trail.

    This is what the audit slide-over reads (spec 13 #3).
    """
    detail = repo.exception_detail(id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no exception with id {id!r}")
    return detail.model_dump(mode="json")


@router.get("/api/matches/{id}", response_model=None, tags=["matches"])
def get_match(id: str, repo: RepoDep) -> dict:
    """One match, the bank line it resolved, and its audit trail."""
    detail = repo.match_detail(id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no match with id {id!r}")
    return detail.model_dump(mode="json")
