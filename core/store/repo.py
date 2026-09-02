"""`Repo` -- everything the API needs to persist and read back a run (Task D.1).

This module is the persistence half of Lane D. It lives under `core/` and
therefore **never imports a web dependency** (`tests/test_boundaries.py::
test_core_has_no_web_dependency`): SQLModel and SQLAlchemy, yes; `fastapi.Depends`,
`HTTPException` and `BackgroundTasks`, no. It returns plain models and raises
plain exceptions, and `api/` is what turns those into HTTP.

It also **never reads a clock**. `created_at` is handed in, which is why
`create_run` takes it as a required keyword rather than defaulting it: a default
would have to call `datetime.now()`, and that call belongs at the API boundary
(LANE-D-api.md 5.5).

**Tenancy is enforced here and nowhere else.** A `Repo` is bound to exactly one
`org_id` at construction; every write stamps it and every read filters on it. No
method takes an org as an argument, so no caller above this module can widen its
own scope, and `api/routes.py` does not contain the identifier at all
(`tests/api/test_tenancy.py` asserts that against the file's own AST). Single-user
mode is not an exemption from the rule but an instance of it: with `RECON_AUTH`
disabled every request is scoped to `DEFAULT_ORG_ID`, which means the filter is
exercised by the demo path rather than only by the multi-tenant one.

The scoping is a *view*, not a second database: `Repo.scoped()` returns a sibling
sharing this instance's engine and connection pool, so a per-request org costs an
object and not a SQLite connection.

**Two invariants the rest of the lane leans on.**

*Deterministic paging.* Every listing orders by a key that is unique within the
run and never by nothing: `exceptions_page` by `exception_id`, `matches_page` by
`match_id`, `records_page` by `record_id` within the source, `settlements_page`
by `settlement_id`. These tables page over as many as 5,000 rows on camera, and
a query with no ORDER BY is free to return page 2 with a row page 1 already
showed. SQLite usually returns rowid order and usually looks fine, which is
exactly what makes the bug expensive to find later.

*No listing inlines an audit trail.* A 5,000-record run has hundreds of
exceptions and almost none of them are opened, so the trail is fetched once, for
the row a reviewer clicks, from the detail endpoint that already serves it.

*The subject join is resolved per page, not per row.* Each exception names its
subject by `subject_type` + `subject_id`; the record itself is joined out of the
`records` table. Done row by row that is one query per row, or 5,000 queries for
one page; done here it is at most three, one per subject shape.

The read models at the top compose the frozen `core/models.py` classes into the
envelope shapes `api/openapi.yaml` declares. They **subclass** the frozen models
rather than restating their fields, so a field can never drift between the
contract and the wire.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlmodel import Session, SQLModel, col, create_engine, select

from core.matcher.batch import payment_leg_count, reconstruct
from core.matcher.engine import DEDUP_SUPPRESSED_RULE, MatchResult
from core.models import (
    AuditEntry,
    BankLine,
    MatchGroup,
    Metrics,
    Order,
    PSPTransaction,
    ReconException,
    RunSummary,
    Settlement,
)
from core.money import Money
from core.store.schema import (
    DEFAULT_ORG_ID,
    AccessLog,
    Audit,
    Exception_,
    Match,
    Record,
    Run,
    Upload,
    UploadQuarantine,
    UploadRecord,
)

__all__ = [
    "AccessRecord",
    "BatchNetting",
    "BatchNettingOrderLine",
    "ExceptionsPage",
    "MatchDetail",
    "MatchesPage",
    "ReconExceptionAudited",
    "ReconExceptionDetail",
    "RecordSource",
    "RecordsPage",
    "ReadAccess",
    "Repo",
    "RunStatus",
    "SettlementsPage",
    "SubjectNotFound",
    "SubjectRecord",
    "UnknownRun",
    "UploadIngestion",
    "UploadQuarantinePage",
    "UploadSummary",
    "UploadedRow",
]

#: `SubjectRecord` in the contract: exactly one of the three spec 6.2 input
#: shapes, narrowed by the sibling `subject_type`.
SubjectRecord = Order | PSPTransaction | BankLine

#: The canonical shapes an upload can produce. Spelled as its own alias rather
#: than reusing `SubjectRecord`: that name means "the subject of an exception",
#: and an upload's records are not the subject of anything yet.
CanonicalUpload = Order | PSPTransaction | BankLine

#: `RecordSource` in the contract: which of the three spec 6.2 input tables a
#: record came from. The same three values `ReconException.subject_type` takes
#: and the same three keys `_SUBJECT_MODELS` is built on, spelled once so the
#: query parameter, the page envelope and the join cannot drift apart.
RecordSource = Literal["order", "psp_txn", "bank_line"]

_SUBJECT_MODELS: dict[str, type[BaseModel]] = {
    "order": Order,
    "psp_txn": PSPTransaction,
    "bank_line": BankLine,
}

#: The identifier field of each subject shape. The keys match the values
#: `ReconException.subject_type` takes, so the join needs no translation.
_RECORD_ID_FIELD = {"order": "order_id", "psp_txn": "txn_id", "bank_line": "line_id"}

#: SQLite's default host-parameter ceiling is 999. An `IN (...)` built from a
#: caller-supplied page size would blow through it on a large page, so every
#: `IN` in this module is chunked well under the limit.
_IN_CHUNK = 400


# --- errors -------------------------------------------------------------------


class UnknownRun(LookupError):
    """No run with this id. `api/` renders it as 404."""


class SubjectNotFound(LookupError):
    """An exception or match named a record that is not in the `records` table.

    Raised, never nulled. `ReconExceptionDetail.subject` is required and
    non-null in the contract: an exception whose subject cannot be resolved is a
    bug to surface, not a hole to serialise (LANE-D-api.md 6).
    """


# --- read models: the contract's envelope shapes ------------------------------


class ReconExceptionDetail(ReconException):
    """`ReconException` + the record it is about.

    `core/models.py` has no class for this and is frozen; the record is joined
    here because `web/` may not read `core/models.py` and the audit slide-over
    has to render the subject (spec 13 #3).
    """

    subject: SubjectRecord


class ReconExceptionAudited(ReconExceptionDetail):
    """What `GET /api/exceptions/{id}` returns: detail + the full audit trail.

    Deliberately not what the paginated list returns -- the trail is fetched
    once, for the row a reviewer actually opens.
    """

    audit_trail: list[AuditEntry]


class ExceptionsPage(BaseModel):
    """`PaginatedReconExceptions`. `total` counts the *filtered* set."""

    model_config = ConfigDict(strict=True)

    items: list[ReconExceptionDetail]
    total: int
    page: int
    size: int


class RecordsPage(BaseModel):
    """`PaginatedRecords`. One page of ingested rows of a single source.

    `source` is echoed back rather than left for the caller to remember. The
    contract's `SubjectRecord` is a bare `oneOf` with no discriminator inside
    the records themselves -- "do not sniff fields" -- so the tag has to travel
    with the page.
    """

    model_config = ConfigDict(strict=True)

    items: list[SubjectRecord]
    total: int
    page: int
    size: int
    source: RecordSource


class SettlementsPage(BaseModel):
    """`PaginatedSettlements`. One row per settlement the run saw."""

    model_config = ConfigDict(strict=True)

    items: list[Settlement]
    total: int
    page: int
    size: int


class MatchesPage(BaseModel):
    """`PaginatedMatches`. The run's accepted matches, ordered by `match_id`.

    `MatchGroup` as the engine emitted it -- tier, confidence and the evidence
    lines included, which are what make a match checkable rather than asserted.
    No subject record and no audit trail: those are `GET /api/matches/{id}`, for
    the row a reviewer opens.
    """

    model_config = ConfigDict(strict=True)

    items: list[MatchGroup]
    total: int
    page: int
    size: int


class MatchDetail(MatchGroup):
    """`MatchGroup` + the bank line it resolved + the trail for its subjects."""

    subject: BankLine
    audit_trail: list[AuditEntry]


class BatchNettingOrderLine(BaseModel):
    model_config = ConfigDict(strict=True)

    order_id: str
    gross_amount: Money


class BatchNetting(BaseModel):
    """The netting diagram's data: N orders -> one settlement -> one bank line.

    Every money field is passed through from the `MatchGroup` the engine
    produced. Nothing here recomputes a fee, a tax or a net -- re-deriving a
    number in the API is indistinguishable from an engine bug on the screen
    (LANE-D-api.md 5.1, 7).
    """

    model_config = ConfigDict(strict=True)

    settlement_id: str
    bank_line_id: str
    orders: list[BatchNettingOrderLine]
    psp_txn_ids: list[str]
    gross: Money
    fees: Money
    tax: Money
    refunds: Money
    holds: Money
    net: Money
    tier: Literal["T0", "T1", "T2", "T3", "LLM"]
    evidence: list[str]


class UploadSummary(BaseModel):
    """`Upload` in the contract: one file a merchant sent, and what it became.

    Everything on it is a recorded fact rather than a derivation. `state` is the
    one field that is computed, and it exists because three outcomes that a
    single count cannot tell apart are three different things to say to a human:

    * **ingested** -- at least one canonical record came out. Rows may still
      have been quarantined beside them; a review queue next to usable records
      is not a failure.
    * **quarantined** -- the file was read and every row of it was refused.
    * **empty** -- the file was read, its header was recognised, and it carried
      no data rows at all.

    The last two are the distinction the console is built on: an export that
    quarantined everything is a file full of damage, and an empty export is a
    file with nothing in it. One is a data-quality problem the merchant can
    fix from the quarantine table; the other means they exported the wrong
    date range. A single "0 records" would say neither.

    A file no adapter recognised has no row here at all -- it is a 422 from
    `POST /api/uploads` naming the candidates, and nothing is retained.
    """

    model_config = ConfigDict(strict=True)

    upload_id: str
    filename: str
    content_sha256: str
    byte_size: int
    format_id: str
    format_version: str
    confidence: float
    encoding: str
    state: Literal["ingested", "quarantined", "empty"]
    record_count: int
    quarantine_count: int
    skipped_rows: int
    order_count: int
    psp_txn_count: int
    bank_line_count: int
    uploaded_at: datetime = Field(strict=False)


class UploadIngestion(BaseModel):
    """What `Repo.record_upload` decided: the upload, and whether it is new.

    `already_ingested` is the A3 idempotency answer and it is produced HERE
    rather than by the caller, because the uniqueness it reports on lives in
    this layer. A route that looked the hash up, found nothing and then
    inserted would have a window between the two in which a second request
    could insert the same file -- and the merchant would hold two upload ids
    for one document, which is precisely what content addressing exists to
    prevent.
    """

    model_config = ConfigDict(strict=True)

    upload: UploadSummary
    already_ingested: bool


class UploadedRow(BaseModel):
    """One quarantined row, exactly as it arrived.

    `raw` is merchant data. It reaches a client only through
    `GET /api/uploads/{id}/quarantine`, which is authenticated like every other
    financial read, and it never appears in an error body.
    """

    model_config = ConfigDict(strict=True)

    row_number: int
    raw: str
    reason: str
    detail: str


class UploadQuarantinePage(BaseModel):
    """`PaginatedQuarantine`. Ordered by `row_number`, then by insertion id.

    `row_number` alone is not unique -- a file-level refusal and a malformed
    first line both sit at line 1 -- so the tiebreak is the surrogate key,
    which makes the ordering total and the paging stable in the same way every
    other listing in this module is.
    """

    model_config = ConfigDict(strict=True)

    items: list[UploadedRow]
    total: int
    page: int
    size: int


class ReadAccess(BaseModel):
    """One access-log row, as it comes back out.

    A plain model rather than the SQLModel row: the table is append-only and
    handing a caller an ORM-attached instance is handing them something with a
    `session.add` waiting to happen.
    """

    model_config = ConfigDict(strict=True)

    actor: str
    resource: str
    resource_id: str | None
    action: str
    at: str
    status: int


class AccessRecord(BaseModel):
    """One access to be recorded. Built at the API boundary, which owns the clock.

    `at` is a string in ISO-8601 rather than a `datetime` for the same reason
    `create_run` takes a `datetime` and stores its `isoformat()`: this package
    must not be able to produce a timestamp, and a model that accepted `None`
    here would be one default away from producing one.
    """

    model_config = ConfigDict(strict=True)

    actor: str
    resource: str
    resource_id: str | None
    action: str
    at: str
    status: int


class RunStatus(BaseModel):
    """What the 500 ms poller reads. Cheap: one row, no joins."""

    model_config = ConfigDict(strict=True)

    state: Literal["pending", "running", "completed", "failed"]
    progress: float
    stage: str


# --- the repository -----------------------------------------------------------


class Repo:
    """SQLite persistence for runs. One instance per database file."""

    #: Every table `_migrate` has to consider. Listed rather than derived from
    #: `SQLModel.metadata`, because that registry also holds tables other
    #: packages define, and a migration that silently widened its own scope
    #: would be worse than one that has to be edited when a table is added.
    _TENANT_TABLES = (
        "runs",
        "records",
        "matches",
        "exceptions",
        "audit",
        "access_log",
        "uploads",
        "upload_records",
        "upload_quarantine",
    )

    def __init__(self, db_path: Path | str, *, org_id: str = DEFAULT_ORG_ID) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(
            # `as_posix()` because a Windows backslash in a URL is not a
            # separator; `check_same_thread=False` because the background job
            # runs in a worker thread and `timeout` so a concurrent write waits
            # rather than raising "database is locked".
            f"sqlite:///{self.path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 30.0},
        )
        SQLModel.metadata.create_all(self._engine)
        self._migrate()
        self._org_id = org_id
        # This instance is in its own scope map, so a sibling scoping back to
        # the org this one holds finds *this* object rather than building a
        # third that means the same thing.
        self._scoped: dict[str, Repo] = {org_id: self}

    # --- tenancy --------------------------------------------------------------

    @property
    def org_id(self) -> str:
        """The one org this instance reads and writes. Read-only on purpose."""
        return self._org_id

    def scoped(self, org_id: str) -> "Repo":
        """A sibling bound to `org_id`, sharing this instance's engine.

        The engine owns a connection pool and `api/deps.py` caches one `Repo`
        per database file for exactly that reason; building a fresh engine per
        request -- or per org -- would open a SQLite connection per request and
        the 500 ms status poll would pay for it. So the org scope is a view
        over the same engine, memoised per org.

        `object.__new__` rather than `__init__`: re-running the constructor
        would re-create the engine, which is the thing this is avoiding. The
        two lines below are the whole of the sibling's state.
        """
        if org_id == self._org_id:
            return self
        existing = self._scoped.get(org_id)
        if existing is not None:
            return existing
        sibling = object.__new__(Repo)
        sibling.path = self.path
        sibling._engine = self._engine
        sibling._org_id = org_id
        # Shared, so `a.scoped("b").scoped("a")` finds the original rather than
        # building a third object that means the same thing.
        sibling._scoped = self._scoped
        self._scoped[org_id] = sibling
        return sibling

    def _mine(self, model) -> object:
        """`model.org_id == this repo's org`, spelled once.

        Every read below composes this into its WHERE clause. Written as a
        helper rather than repeated as a literal so that the one thing a
        reviewer has to check per query is that the helper is present.
        """
        return col(model.org_id) == self._org_id

    def _migrate(self) -> None:
        """Add `org_id` to a database written before this column existed.

        Additive and lossless, and it has to be: the file this runs against is
        somebody's `out/recon.db` with real runs in it, and the alternative --
        "delete your database" -- is the kind of migration note that turns a
        security feature into a reason not to upgrade.

        SQLite's `ADD COLUMN` with a constant `DEFAULT` is a metadata-only
        operation: existing rows read back the default without being rewritten.
        The default is `DEFAULT_ORG_ID`, which is the *correct* owner of those
        rows rather than a placeholder -- they were written by a deployment
        that had exactly one operator.

        `create_all` above cannot do this. It creates missing tables and
        ignores tables that already exist, so a table from before this column
        would keep its old shape and every query below would fail on a missing
        column. This is the smallest thing that is a migration; the project
        does not carry Alembic for one additive column, and if it ever needs a
        second migration it should.
        """
        with self._engine.begin() as connection:
            for table in self._TENANT_TABLES:
                columns = {
                    row[1]
                    for row in connection.exec_driver_sql(
                        f"PRAGMA table_info({table})"
                    )
                }
                if not columns or "org_id" in columns:
                    continue
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN org_id VARCHAR NOT NULL "
                    f"DEFAULT '{DEFAULT_ORG_ID}'"
                )
                connection.exec_driver_sql(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_org_id "
                    f"ON {table} (org_id)"
                )

    # --- writes ---------------------------------------------------------------

    def create_run(
        self,
        *,
        seed: int,
        record_count: int,
        created_at: datetime,
        dataset_id: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Insert a `pending` run and return its id.

        `created_at` is required and must be a `datetime`. It is the one field
        of the frozen `RunSummary` contract that the API produces, and it is
        stamped by the caller: nothing in `core/` may read a clock, so a default
        here would be the constraint's only violation.
        """
        if not isinstance(created_at, datetime):
            raise TypeError(
                "create_run(created_at=...) must be a datetime stamped by the "
                "caller: core/ never reads a clock (LANE-D-api.md 5.5)"
            )
        run_id = run_id or f"run-{uuid4().hex[:12]}"
        with Session(self._engine) as session:
            session.add(
                Run(
                    org_id=self._org_id,
                    run_id=run_id,
                    dataset_id=dataset_id,
                    seed=seed,
                    record_count=record_count,
                    state="pending",
                    created_at=created_at.isoformat(),
                    progress=0.0,
                    stage="queued",
                )
            )
            session.commit()
        return run_id

    def set_progress(
        self,
        run_id: str,
        *,
        state: str | None = None,
        progress: float | None = None,
        stage: str | None = None,
    ) -> None:
        """Update the polled fields. The job calls this; nothing else should."""
        with Session(self._engine) as session:
            run = self._own_run(session, run_id)
            if run is None:
                raise UnknownRun(run_id)
            if state is not None:
                run.state = state
            if progress is not None:
                run.progress = float(progress)
            if stage is not None:
                run.stage = stage
            session.add(run)
            session.commit()

    def save_records(
        self,
        run_id: str,
        orders: Sequence[Order],
        psp_txns: Sequence[PSPTransaction],
        bank_lines: Sequence[BankLine],
    ) -> None:
        """Persist the run's inputs so subjects can be joined back later."""
        rows: list[Record] = []
        for kind, items in (
            ("order", orders),
            ("psp_txn", psp_txns),
            ("bank_line", bank_lines),
        ):
            id_field = _RECORD_ID_FIELD[kind]
            rows.extend(
                Record(
                    org_id=self._org_id,
                    run_id=run_id,
                    record_type=kind,
                    record_id=getattr(item, id_field),
                    payload=item.model_dump_json(),
                )
                for item in items
            )
        self._insert_all(rows)

    def save_result(
        self,
        run_id: str,
        result: MatchResult,
        *,
        metrics: Metrics | None = None,
        state: str = "completed",
        stage: str = "complete",
        progress: float = 1.0,
    ) -> None:
        """Persist a run's matches, exceptions and audit trail, and close it out.

        `match_count` and `exception_count` are the lengths of what the engine
        returned -- counted, never re-derived from a filtered view.
        """
        self._insert_all(
            [
                Match(
                    org_id=self._org_id,
                    run_id=run_id,
                    match_id=match.match_id,
                    bank_line_id=match.bank_line_id,
                    settlement_id=match.settlement_id,
                    payload=match.model_dump_json(),
                )
                for match in result.matches
            ]
        )
        self._insert_all(
            [
                Exception_(
                    org_id=self._org_id,
                    run_id=run_id,
                    exception_id=exception.exception_id,
                    reason_code=exception.reason_code.value,
                    subject_type=exception.subject_type,
                    subject_id=exception.subject_id,
                    payload=exception.model_dump_json(),
                )
                for exception in result.exceptions
            ]
        )
        self._insert_all(
            [
                Audit(
                    org_id=self._org_id,
                    run_id=run_id,
                    entry_id=entry.entry_id,
                    subject_id=entry.subject_id,
                    sequence=entry.sequence,
                    payload=entry.model_dump_json(),
                )
                for entry in result.audit
            ]
        )

        with Session(self._engine) as session:
            run = self._own_run(session, run_id)
            if run is None:
                raise UnknownRun(run_id)
            run.match_count = len(result.matches)
            run.exception_count = len(result.exceptions)
            run.metrics_json = None if metrics is None else metrics.model_dump_json()
            run.state = state
            run.stage = stage
            run.progress = float(progress)
            session.add(run)
            session.commit()

    def record_access(self, accesses: Sequence[AccessRecord]) -> None:
        """Append rows to the access log. **The only write path to that table.**

        Takes a sequence rather than a single row because the brief allows
        batching and the shape should not have to change to take it up: today
        `api/main.py` passes one row per request, and a future buffered writer
        passes twenty without this method or its tests moving.

        There is deliberately no counterpart that updates or deletes. Retention
        will eventually have to remove rows (COMPLIANCE.md); when it does it
        will be a named operation with its own path and its own test, not a
        method that has been sitting here callable by every route in the
        meantime. `tests/api/test_access_log.py` walks this module's AST and
        fails if a third method ever names the table.
        """
        self._insert_all(
            [
                AccessLog(
                    org_id=self._org_id,
                    actor=access.actor,
                    resource=access.resource,
                    resource_id=access.resource_id,
                    action=access.action,
                    at=access.at,
                    status=access.status,
                )
                for access in accesses
            ]
        )

    def access_log(self, *, limit: int = 500) -> list[ReadAccess]:
        """This org's access log, oldest first. Scoped like everything else.

        Ordered on the primary key rather than on `at`: `at` is a wall-clock
        string stamped per request and two reads inside the same millisecond
        would tie, where the autoincrementing id is the order the rows were
        actually appended in -- which is what an append-only log is claiming to
        preserve.
        """
        with Session(self._engine) as session:
            rows = session.exec(
                select(AccessLog)
                .where(self._mine(AccessLog))
                .order_by(col(AccessLog.id))
                .limit(limit)
            ).all()
        return [
            ReadAccess(
                actor=row.actor,
                resource=row.resource,
                resource_id=row.resource_id,
                action=row.action,
                at=row.at,
                status=row.status,
            )
            for row in rows
        ]

    def _insert_all(self, rows: Sequence[SQLModel]) -> None:
        if not rows:
            return
        with Session(self._engine) as session:
            session.add_all(list(rows))
            session.commit()

    # --- reads ----------------------------------------------------------------

    def _own_run(self, session: Session, run_id: str) -> Run | None:
        """The run with this id **belonging to this repo's org**, or `None`.

        A run of another org is `None`, not a row: every caller renders `None`
        as a 404, and 404 is the right answer -- "exists but is not yours" and
        "does not exist" must be indistinguishable, or the API becomes an
        oracle for which run ids other tenants hold.

        Fetched by primary key and then checked, rather than selected with the
        org in the WHERE clause, only because `run_id` is the primary key and
        `session.get` uses the identity map. The check is not optional and the
        method exists so it cannot be forgotten at one of its seven call sites.
        """
        run = session.get(Run, run_id)
        if run is None or run.org_id != self._org_id:
            return None
        return run

    def run_exists(self, run_id: str) -> bool:
        with Session(self._engine) as session:
            return self._own_run(session, run_id) is not None

    def dataset_id(self, run_id: str) -> str | None:
        with Session(self._engine) as session:
            run = self._own_run(session, run_id)
            return None if run is None else run.dataset_id

    def summary(self, run_id: str) -> RunSummary | None:
        with Session(self._engine) as session:
            run = self._own_run(session, run_id)
            return None if run is None else _as_summary(run)

    def list_runs(self) -> list[RunSummary]:
        """Run history, most recent first.

        Ordered on `created_at` then `run_id`, both descending: the second key
        is what makes two runs stamped in the same millisecond come back in a
        fixed order rather than whatever SQLite feels like.
        """
        with Session(self._engine) as session:
            runs = session.exec(
                select(Run)
                .where(self._mine(Run))
                .order_by(col(Run.created_at).desc(), col(Run.run_id).desc())
            ).all()
            return [_as_summary(run) for run in runs]

    def previous_completed_run(self, run_id: str) -> RunSummary | None:
        """The immediately previous **completed** run on the same dataset.

        The default baseline for `GET /api/runs/{id}/drift` when no `against`
        is given. Three conditions, each of which the endpoint would otherwise
        have to get right in Python over the whole run history:

        * **same `dataset_id`**, including when it is NULL -- a run created
          outside the API carries none, and grouping those together is the same
          rule applied consistently rather than a special case.
        * **`completed`**, because a failed or in-flight run has no `Metrics`
          to compare against. A baseline that silently resolved to a run with
          `metrics=None` would turn a missing baseline into a crash.
        * **strictly earlier**, on `(created_at, run_id)` -- the same composite
          key and the same descending order `list_runs` uses, so the drift
          report and the run-history table can never disagree about which run
          came before which. The tuple comparison is spelled out rather than
          written as a row comparison because SQLite's support for the latter
          is version-dependent and this is one `OR` either way.

        Raises `UnknownRun` rather than returning `None` for a run that does not
        exist: `None` already means "no earlier run on this dataset", and the
        route renders the two differently.
        """
        with Session(self._engine) as session:
            current = self._own_run(session, run_id)
            if current is None:
                raise UnknownRun(run_id)

            dataset = (
                col(Run.dataset_id).is_(None)
                if current.dataset_id is None
                else col(Run.dataset_id) == current.dataset_id
            )
            earlier = (col(Run.created_at) < current.created_at) | (
                (col(Run.created_at) == current.created_at)
                & (col(Run.run_id) < current.run_id)
            )
            found = session.exec(
                select(Run)
                .where(self._mine(Run), Run.state == "completed", dataset, earlier)
                .order_by(col(Run.created_at).desc(), col(Run.run_id).desc())
                .limit(1)
            ).first()
            return None if found is None else _as_summary(found)

    def reason_code_census(self, run_id: str) -> dict[str, int]:
        """`{reason_code: count}` for one run, **grouped in SQL**.

        The reason-code distribution `core/drift/compare.py` compares. Drift is
        meant to be cheap enough to call on every run and a 5,000-record run has
        hundreds of exceptions, so this must not become a scan: the natural
        Python implementation reads every row's `payload`, reconstitutes a
        `ReconException` from its JSON and counts eight buckets, which is the
        whole exceptions table parsed to produce at most eight integers.

        `reason_code` is a lifted column on `exceptions` precisely so a query
        like this one never has to open the payload -- the same column
        `exceptions_page` filters on. `tests/api/test_drift_store.py::
        test_the_census_is_grouped_in_sql_and_never_loads_a_row` captures the
        emitted SQL and checks all three claims, because a Python-side
        `Counter` returns exactly the same dict and no assertion on the result
        could tell them apart.

        A code the run never recorded is **absent**, not zero. The census is
        what the run recorded, not a template of every `ReasonCode`, and
        `compare()` reads a missing key as 0 -- which is what makes
        `ReasonCodeMove.appeared` mean "absent before, present now". An unknown
        run therefore has an empty census, which is the same shape as a run that
        produced no exceptions at all; the endpoint checks the run exists.
        """
        with Session(self._engine) as session:
            rows = session.exec(
                select(Exception_.reason_code, func.count())
                .where(self._mine(Exception_), Exception_.run_id == run_id)
                .group_by(col(Exception_.reason_code))
                .order_by(col(Exception_.reason_code))
            ).all()
        return {str(code): int(count) for code, count in rows}

    def status(self, run_id: str) -> RunStatus | None:
        with Session(self._engine) as session:
            run = self._own_run(session, run_id)
            if run is None:
                return None
            return RunStatus(
                state=run.state, progress=float(run.progress), stage=run.stage
            )

    def exceptions_page(
        self,
        run_id: str,
        page: int = 1,
        size: int = 50,
        reason_code: str | None = None,
    ) -> ExceptionsPage:
        """One page of exceptions, each carrying its subject record.

        **`ORDER BY exception_id` is the stability guarantee.** `exception_id` is
        unique within a run, so the ordering is total and an offset into it names
        the same row every time: page 2 can never repeat a row from page 1, and
        paging back and forth over 5,000 rows shows each one exactly once.
        """
        page = max(1, int(page))
        size = max(1, int(size))
        conditions = [self._mine(Exception_), Exception_.run_id == run_id]
        if reason_code is not None:
            conditions.append(Exception_.reason_code == str(reason_code))

        with Session(self._engine) as session:
            total = session.exec(
                select(func.count()).select_from(Exception_).where(*conditions)
            ).one()
            rows = session.exec(
                select(Exception_)
                .where(*conditions)
                .order_by(col(Exception_.exception_id))
                .offset((page - 1) * size)
                .limit(size)
            ).all()
            exceptions = [ReconException.model_validate_json(r.payload) for r in rows]
            subjects = self._subjects(
                session,
                run_id,
                [(e.subject_type, e.subject_id) for e in exceptions],
            )

        return ExceptionsPage(
            items=[_with_subject(e, subjects) for e in exceptions],
            total=int(total),
            page=page,
            size=size,
        )

    def matches_page(
        self, run_id: str, page: int = 1, size: int = 50
    ) -> MatchesPage:
        """One page of the run's accepted matches.

        The counterpart of `exceptions_page`, and it exists for the same reason
        the exception list does: a console that can only show what failed lets a
        reviewer check the failures and take the successes on trust, when the
        successes are what the match rate is made of.

        `ORDER BY match_id` -- unique within a run, so the ordering is total and
        an offset names the same row every time.

        The `MatchGroup` is handed over as the engine emitted it, evidence lines
        and all. What is deliberately absent is the audit trail: this pages over
        every match of the run, and the trail is fetched for the row a reviewer
        actually opens, from `GET /api/matches/{id}`.
        """
        page = max(1, int(page))
        size = max(1, int(size))
        with Session(self._engine) as session:
            total = session.exec(
                select(func.count())
                .select_from(Match)
                .where(self._mine(Match), Match.run_id == run_id)
            ).one()
            rows = session.exec(
                select(Match)
                .where(self._mine(Match), Match.run_id == run_id)
                .order_by(col(Match.match_id))
                .offset((page - 1) * size)
                .limit(size)
            ).all()

        return MatchesPage(
            items=[MatchGroup.model_validate_json(row.payload) for row in rows],
            total=int(total),
            page=page,
            size=size,
        )

    def records_page(
        self, run_id: str, source: str, page: int = 1, size: int = 50
    ) -> RecordsPage:
        """One page of the run's ingested rows, of a single source.

        This is the only endpoint that shows a reviewer the data the engine
        actually *read*. Everything else attaches a record to a verdict -- an
        exception's subject, a match's bank line -- so a row that produced
        neither has, until now, been invisible.

        `ORDER BY record_id`: the id is unique within a run and a source, so the
        ordering is total and an offset names the same row every time.

        The row is reconstituted from the payload, which is the frozen model's
        own JSON, so what comes back is byte-for-byte what was ingested --
        nullability included. `PSPTransaction.order_id` absent is the
        missing_order_ref defect and `BankLine.credit` null is a debit line;
        both are the data, not gaps to fill in on the way out.
        """
        if source not in _SUBJECT_MODELS:
            # Refused rather than answered with an empty page: "no such source"
            # and "this run has no orders" are different facts, and a silently
            # empty table reads as the second one.
            raise ValueError(
                f"unknown record source {source!r}; expected one of "
                f"{sorted(_SUBJECT_MODELS)}"
            )
        page = max(1, int(page))
        size = max(1, int(size))
        model = _SUBJECT_MODELS[source]
        conditions = [
            self._mine(Record),
            Record.run_id == run_id,
            Record.record_type == source,
        ]

        with Session(self._engine) as session:
            total = session.exec(
                select(func.count()).select_from(Record).where(*conditions)
            ).one()
            rows = session.exec(
                select(Record)
                .where(*conditions)
                .order_by(col(Record.record_id))
                .offset((page - 1) * size)
                .limit(size)
            ).all()

        return RecordsPage(
            items=[model.model_validate_json(row.payload) for row in rows],
            total=int(total),
            page=page,
            size=size,
            source=source,
        )

    def settlements_page(
        self, run_id: str, page: int = 1, size: int = 50
    ) -> SettlementsPage:
        """One page of settlements: every `settlement_id` the run's PSP legs name.

        **Every settlement, not every match.** A listing driven off the `matches`
        table would show only the batches that closed and silently drop the ones
        that did not -- which are the rows a reviewer opens this for. On
        `fixtures/seed42-500` that is the difference between 166 rows and 151.

        **`ORDER BY settlement_id`**, so an offset names the same row every time.
        The ordering is done in Python rather than in SQL because `records` has
        no `settlement_id` column: the payload is the frozen model's own JSON and
        the settlement is inside it. That costs one scan of the run's PSP rows
        per page -- 852 rows on the committed 500-record fixture -- which is the
        price of not keeping a second transcription of a frozen field in a
        column that could disagree with it.

        Money comes from exactly one of two places, never a third:

        * matched -> the `MatchGroup`'s own fields, passed through untouched;
        * unmatched -> `reconstruct` over the settlement's **active** legs.

        Those two agree by construction, and `tests/api/test_store.py::
        test_the_two_derivations_of_a_settlement_breakdown_agree` holds them to
        it -- see `settlement_legs` for what "active" means and why it matters.
        """
        page = max(1, int(page))
        size = max(1, int(size))

        with Session(self._engine) as session:
            legs = self._legs_by_settlement(session, run_id)
            ordered = sorted(legs)
            total = len(ordered)
            wanted = ordered[(page - 1) * size : (page - 1) * size + size]
            groups = self._matches_by_settlement(session, run_id, wanted)
            unmatched = [sid for sid in wanted if sid not in groups]
            suppressed = self._suppressed_txn_ids(
                session,
                run_id,
                [txn.txn_id for sid in unmatched for txn in legs[sid]],
            )

        items: list[Settlement] = []
        for settlement_id in wanted:
            group = groups.get(settlement_id)
            if group is None:
                items.append(
                    _unmatched_settlement(
                        settlement_id,
                        [t for t in legs[settlement_id] if t.txn_id not in suppressed],
                    )
                )
            else:
                items.append(_matched_settlement(group, legs[settlement_id]))

        return SettlementsPage(items=items, total=total, page=page, size=size)

    def settlement_legs(self, run_id: str, settlement_id: str) -> list[PSPTransaction]:
        """The settlement's **active** legs, in `txn_id` order.

        Active means "the legs the engine actually reconstructed from": the
        run's PSP rows for this settlement, less any the pool suppressed as a
        duplicate. That subtraction is not cosmetic. `fixtures/seed42-500`
        suppresses a repeated payment leg inside 9 of its 166 settlements, and
        `reconstruct` over the raw rows lands those batches' gross between 2.4
        and 4.0 million paise above the `MatchGroup` the engine emitted -- a
        settlements listing built on the raw rows would put a wrong net beside a
        right one and give a reviewer no way to tell which.

        The suppression is read back out of the run's own audit trail
        (`DEDUP_SUPPRESSED_RULE`), which is the only persisted record of it:
        `MatchResult` carries the matches and the exceptions, never the pool. So
        this is still the engine's decision, read, not a rule re-implemented here.
        """
        with Session(self._engine) as session:
            legs = self._legs_by_settlement(session, run_id).get(settlement_id, [])
            suppressed = self._suppressed_txn_ids(
                session, run_id, [txn.txn_id for txn in legs]
            )
        return [txn for txn in legs if txn.txn_id not in suppressed]

    def exception_detail(self, exception_id: str) -> ReconExceptionAudited | None:
        """One exception, its subject, and every audit entry for that subject.

        `exception_id` is minted by the engine as `exc-<subject_id>` and is
        therefore unique within a run but repeats across runs of the same
        dataset, while `GET /api/exceptions/{id}` carries no run scope. The tie
        is broken deterministically toward the **most recent run**: the audit
        slide-over is opened from the run a reviewer just executed. Reported to
        the human rather than resolved by editing the contract.
        """
        with Session(self._engine) as session:
            found = session.exec(
                select(Exception_, Run)
                .join(Run, col(Exception_.run_id) == col(Run.run_id))
                .where(self._mine(Exception_), Exception_.exception_id == exception_id)
                .order_by(col(Run.created_at).desc(), col(Run.run_id).desc())
                .limit(1)
            ).first()
            if found is None:
                return None
            row, _run = found
            exception = ReconException.model_validate_json(row.payload)
            subjects = self._subjects(
                session, row.run_id, [(exception.subject_type, exception.subject_id)]
            )
            trail = self._audit_trail(session, row.run_id, [exception.subject_id])

        return ReconExceptionAudited(
            **exception.model_dump(),
            subject=subjects[(exception.subject_type, exception.subject_id)],
            audit_trail=trail,
        )

    def match_detail(self, match_id: str) -> MatchDetail | None:
        """One match, the bank line it resolved, and its subjects' audit trail.

        The trail spans the bank line and every PSP leg in the group -- the
        contract's "the AuditEntry rows for the same subject(s)" -- so the
        slide-over shows the dedup and order-recovery decisions that led to the
        match, not only the tier that closed it. Same most-recent-run tie-break
        as `exception_detail`.
        """
        with Session(self._engine) as session:
            found = session.exec(
                select(Match, Run)
                .join(Run, col(Match.run_id) == col(Run.run_id))
                .where(self._mine(Match), Match.match_id == match_id)
                .order_by(col(Run.created_at).desc(), col(Run.run_id).desc())
                .limit(1)
            ).first()
            if found is None:
                return None
            row, _run = found
            group = MatchGroup.model_validate_json(row.payload)
            subjects = self._subjects(
                session, row.run_id, [("bank_line", group.bank_line_id)]
            )
            trail = self._audit_trail(
                session, row.run_id, [group.bank_line_id, *group.psp_txn_ids]
            )

        return MatchDetail(
            **group.model_dump(),
            subject=subjects[("bank_line", group.bank_line_id)],
            audit_trail=trail,
        )

    def batch_netting(self, run_id: str, settlement_id: str) -> BatchNetting | None:
        """The netting breakdown for one settlement of one run.

        Every amount is the engine's. `order_ids` is used exactly as the engine
        recorded it -- it is the *true* order set for the settlement, including
        an order recovered from a leg whose own `order_id` was missing, so
        filtering it against the PSP legs would drop a correct id
        (LANE-D-api.md 7 #2).
        """
        with Session(self._engine) as session:
            row = session.exec(
                select(Match)
                .where(
                    self._mine(Match),
                    Match.run_id == run_id,
                    Match.settlement_id == settlement_id,
                )
                .order_by(col(Match.match_id))
                .limit(1)
            ).first()
            if row is None:
                return None
            group = MatchGroup.model_validate_json(row.payload)
            orders = self._subjects(
                session, run_id, [("order", oid) for oid in group.order_ids]
            )

        return BatchNetting(
            settlement_id=settlement_id,
            bank_line_id=group.bank_line_id,
            orders=[
                BatchNettingOrderLine(
                    order_id=order_id, gross_amount=orders[("order", order_id)].gross_amount
                )
                for order_id in sorted(group.order_ids)
            ],
            psp_txn_ids=list(group.psp_txn_ids),
            gross=group.gross,
            fees=group.fees,
            tax=group.tax,
            refunds=group.refunds,
            holds=group.holds,
            net=group.net,
            tier=group.tier,
            evidence=list(group.evidence),
        )

    # --- uploads ---------------------------------------------------------------

    def record_upload(
        self,
        *,
        upload_id: str,
        filename: str,
        content_sha256: str,
        byte_size: int,
        format_id: str,
        format_version: str,
        confidence: float,
        encoding: str,
        uploaded_at: datetime,
        records: Sequence[CanonicalUpload],
        row_hashes: Sequence[str],
        quarantined: Sequence[UploadedRow],
        skipped_rows: int = 0,
    ) -> UploadIngestion:
        """Persist one upload, or return the one this content already has.

        **The idempotency of A3 is this method.** `(org_id, content_sha256)` is
        looked up first and, if it is there, nothing at all is written: the
        same upload id comes back with `already_ingested=True`, and no second
        set of records or quarantine rows lands beside the first. That is why
        the check is here and not in the route -- see `UploadIngestion`.

        `uploaded_at` is required and must be a `datetime`, for the same reason
        `create_run(created_at=...)` is: nothing in `core/` reads a clock.

        `row_hashes` is positionally parallel to `records`, the parity the
        adapter layer already guarantees and re-checks. It is stored per row so
        row-level dedup has a key; the file-level hash above it is what makes
        the *upload* idempotent.
        """
        if not isinstance(uploaded_at, datetime):
            raise TypeError(
                "record_upload(uploaded_at=...) must be a datetime stamped by "
                "the caller: core/ never reads a clock (LANE-D-api.md 5.5)"
            )
        if len(row_hashes) != len(records):
            raise ValueError(
                "row_hashes must be positionally parallel to records: "
                f"{len(row_hashes)} hashes for {len(records)} records"
            )

        existing = self.upload_for_content(content_sha256)
        if existing is not None:
            return UploadIngestion(upload=existing, already_ingested=True)

        by_kind = {"order": 0, "psp_txn": 0, "bank_line": 0}
        rows: list[UploadRecord] = []
        for record, row_hash in zip(records, row_hashes, strict=True):
            kind = _record_kind(record)
            by_kind[kind] += 1
            rows.append(
                UploadRecord(
                    org_id=self._org_id,
                    upload_id=upload_id,
                    record_type=kind,
                    record_id=getattr(record, _RECORD_ID_FIELD[kind]),
                    row_sha256=row_hash,
                    payload=record.model_dump_json(),
                )
            )

        row = Upload(
            org_id=self._org_id,
            upload_id=upload_id,
            filename=filename,
            content_sha256=content_sha256,
            byte_size=int(byte_size),
            format_id=format_id,
            format_version=format_version,
            confidence=float(confidence),
            encoding=encoding,
            state=_upload_state(len(records), len(quarantined)),
            record_count=len(records),
            quarantine_count=len(quarantined),
            skipped_rows=int(skipped_rows),
            order_count=by_kind["order"],
            psp_txn_count=by_kind["psp_txn"],
            bank_line_count=by_kind["bank_line"],
            uploaded_at=uploaded_at.isoformat(),
        )
        # Read off the row BEFORE it is committed. SQLAlchemy expires every
        # attribute of an instance on commit and this one is detached
        # immediately afterwards, so touching `row.upload_id` below would raise
        # `DetachedInstanceError` rather than return the value we just wrote.
        summary = _as_upload(row)
        with Session(self._engine) as session:
            session.add(row)
            session.commit()
        self._insert_all(rows)
        self._insert_all(
            [
                UploadQuarantine(
                    org_id=self._org_id,
                    upload_id=upload_id,
                    row_number=item.row_number,
                    raw=item.raw,
                    reason=item.reason,
                    detail=item.detail,
                )
                for item in quarantined
            ]
        )
        return UploadIngestion(upload=summary, already_ingested=False)

    def upload_for_content(self, content_sha256: str) -> UploadSummary | None:
        """The upload this org already holds for these bytes, if any.

        Scoped to the org like every other read, and that is the whole of the
        answer to "two tenants uploaded the same file": they hold two uploads.
        Handing the second tenant the first one's id would be a tenancy leak
        wearing deduplication's clothes.
        """
        with Session(self._engine) as session:
            row = session.exec(
                select(Upload)
                .where(self._mine(Upload), Upload.content_sha256 == content_sha256)
                .order_by(col(Upload.uploaded_at), col(Upload.upload_id))
                .limit(1)
            ).first()
        return None if row is None else _as_upload(row)

    def list_uploads(self) -> list[UploadSummary]:
        """Every upload this org holds, most recent first."""
        with Session(self._engine) as session:
            rows = session.exec(
                select(Upload)
                .where(self._mine(Upload))
                .order_by(col(Upload.uploaded_at).desc(), col(Upload.upload_id))
            ).all()
        return [_as_upload(row) for row in rows]

    def upload(self, upload_id: str) -> UploadSummary | None:
        with Session(self._engine) as session:
            row = session.exec(
                select(Upload).where(
                    self._mine(Upload), Upload.upload_id == upload_id
                )
            ).first()
        return None if row is None else _as_upload(row)

    def upload_exists(self, upload_id: str) -> bool:
        return self.upload(upload_id) is not None

    def upload_quarantine_page(
        self, upload_id: str, page: int = 1, size: int = 50
    ) -> UploadQuarantinePage:
        """One page of the rows this upload could not read.

        Ordered by `row_number` then by the surrogate id, which is total -- see
        `UploadQuarantinePage`. Paging over a bank export's quarantine is the
        one screen where a reviewer walks every row, so a page 2 that repeated
        a row from page 1 would be read as two defects rather than one.
        """
        page = max(1, int(page))
        size = max(1, int(size))
        conditions = [
            self._mine(UploadQuarantine),
            UploadQuarantine.upload_id == upload_id,
        ]
        with Session(self._engine) as session:
            total = session.exec(
                select(func.count()).select_from(UploadQuarantine).where(*conditions)
            ).one()
            rows = session.exec(
                select(UploadQuarantine)
                .where(*conditions)
                .order_by(col(UploadQuarantine.row_number), col(UploadQuarantine.id))
                .offset((page - 1) * size)
                .limit(size)
            ).all()
        return UploadQuarantinePage(
            items=[
                UploadedRow(
                    row_number=row.row_number,
                    raw=row.raw,
                    reason=row.reason,
                    detail=row.detail,
                )
                for row in rows
            ],
            total=int(total),
            page=page,
            size=size,
        )

    def upload_inputs(
        self, upload_ids: Sequence[str]
    ) -> tuple[list[Order], list[PSPTransaction], list[BankLine]]:
        """The canonical records of these uploads, as the three engine inputs.

        This is what makes `POST /api/runs` over uploads the SAME run as one
        over a dataset directory: `api/jobs.execute_run` is handed three lists
        either way, and neither the matcher nor the scorer can tell which path
        produced them.

        **Records come back in the order the files carried them.** Within one
        upload that is the surrogate key, which is insertion order, which is
        the order the adapter read the rows -- so a settlement's legs reach the
        engine in the order the merchant's export listed them and
        `MatchGroup.psp_txn_ids` reads the same as it does on the dataset path.
        Sorting by record id instead would reorder those lists, and the two
        paths would then disagree on a field that is on the wire, in the audit
        trail and on the screen.

        Across uploads the order is `(uploaded_at, upload_id)`, which is a
        property of the store rather than of the request: a run over the same
        uploads listed in a different order feeds the engine the same sequence.
        A run whose inputs depended on the order a UI happened to send its
        checkboxes in would be a reproducibility claim with a hole in it.

        Ids missing from this org's uploads contribute nothing; the caller is
        expected to have refused them already, and returning silence for an id
        that is not yours is the same answer every other read here gives.
        """
        wanted = set(upload_ids)

        orders: list[Order] = []
        psp_txns: list[PSPTransaction] = []
        bank_lines: list[BankLine] = []
        buckets: dict[str, list] = {
            "order": orders,
            "psp_txn": psp_txns,
            "bank_line": bank_lines,
        }
        with Session(self._engine) as session:
            ordered = session.exec(
                select(Upload.upload_id)
                .where(self._mine(Upload))
                .order_by(col(Upload.uploaded_at), col(Upload.upload_id))
            ).all()
            for upload_id in [uid for uid in ordered if uid in wanted]:
                rows = session.exec(
                    select(UploadRecord)
                    .where(
                        self._mine(UploadRecord),
                        UploadRecord.upload_id == upload_id,
                    )
                    .order_by(col(UploadRecord.id))
                ).all()
                for row in rows:
                    model = _SUBJECT_MODELS[row.record_type]
                    buckets[row.record_type].append(
                        model.model_validate_json(row.payload)
                    )
        return orders, psp_txns, bank_lines

    # --- joins ----------------------------------------------------------------

    def _subjects(
        self,
        session: Session,
        run_id: str,
        pairs: Sequence[tuple[str, str]],
    ) -> dict[tuple[str, str], SubjectRecord]:
        """Resolve `(subject_type, subject_id)` to records, for the whole page.

        One query per distinct subject shape -- at most three -- rather than one
        per row. At 5,000 exceptions the difference is the endpoint working or
        the endpoint timing out.
        """
        wanted: dict[str, set[str]] = {}
        for kind, identifier in pairs:
            if kind not in _SUBJECT_MODELS:
                raise SubjectNotFound(f"unknown subject_type {kind!r}")
            wanted.setdefault(kind, set()).add(identifier)

        resolved: dict[tuple[str, str], SubjectRecord] = {}
        for kind, identifiers in wanted.items():
            model = _SUBJECT_MODELS[kind]
            ordered = sorted(identifiers)
            for start in range(0, len(ordered), _IN_CHUNK):
                chunk = ordered[start : start + _IN_CHUNK]
                rows = session.exec(
                    select(Record).where(
                        self._mine(Record),
                        Record.run_id == run_id,
                        Record.record_type == kind,
                        col(Record.record_id).in_(chunk),
                    )
                ).all()
                for row in rows:
                    resolved[(kind, row.record_id)] = model.model_validate_json(
                        row.payload
                    )

        missing = sorted({pair for pair in pairs if pair not in resolved})
        if missing:
            raise SubjectNotFound(
                f"run {run_id}: {len(missing)} subject record(s) could not be "
                f"joined, first {missing[:3]}. The subject is required and "
                "non-null on the wire, so this is a bug to fix, not a null to "
                "serialise."
            )
        return resolved

    def _legs_by_settlement(
        self, session: Session, run_id: str
    ) -> dict[str, list[PSPTransaction]]:
        """The run's PSP rows grouped by `settlement_id`, each group sorted.

        A leg carrying no `settlement_id` belongs to no batch and is therefore
        not in any group -- it is a record, reachable through
        `GET /api/runs/{id}/records?source=psp_txn`, and an exception if the
        engine could not place it. It is not a settlement of its own.
        """
        rows = session.exec(
            select(Record).where(
                self._mine(Record),
                Record.run_id == run_id,
                Record.record_type == "psp_txn",
            )
        ).all()
        legs: dict[str, list[PSPTransaction]] = defaultdict(list)
        for row in rows:
            txn = PSPTransaction.model_validate_json(row.payload)
            if txn.settlement_id:
                legs[txn.settlement_id].append(txn)
        for group in legs.values():
            group.sort(key=lambda txn: txn.txn_id)
        return dict(legs)

    def _matches_by_settlement(
        self, session: Session, run_id: str, settlement_ids: Sequence[str]
    ) -> dict[str, MatchGroup]:
        """The `MatchGroup` that closed each of these settlements, if any.

        Same `ORDER BY match_id` tie-break as `batch_netting`, so the row a
        listing shows and the netting diagram it links to name the same match.
        """
        found: dict[str, MatchGroup] = {}
        ordered = sorted(settlement_ids)
        for start in range(0, len(ordered), _IN_CHUNK):
            chunk = ordered[start : start + _IN_CHUNK]
            rows = session.exec(
                select(Match)
                .where(
                    self._mine(Match),
                    Match.run_id == run_id,
                    col(Match.settlement_id).in_(chunk),
                )
                .order_by(col(Match.match_id))
            ).all()
            for row in rows:
                if row.settlement_id not in found:
                    found[row.settlement_id] = MatchGroup.model_validate_json(
                        row.payload
                    )
        return found

    def _suppressed_txn_ids(
        self, session: Session, run_id: str, txn_ids: Sequence[str]
    ) -> set[str]:
        """Which of these legs the pool suppressed as a duplicate.

        Scoped to the ids asked about rather than to the whole run: `subject_id`
        is indexed, and the caller only ever needs this for the settlements on
        one page that have no `MatchGroup` to read the leg set off instead.
        """
        ordered = sorted(set(txn_ids))
        suppressed: set[str] = set()
        for start in range(0, len(ordered), _IN_CHUNK):
            chunk = ordered[start : start + _IN_CHUNK]
            rows = session.exec(
                select(Audit).where(
                    self._mine(Audit),
                    Audit.run_id == run_id,
                    col(Audit.subject_id).in_(chunk),
                )
            ).all()
            for row in rows:
                entry = AuditEntry.model_validate_json(row.payload)
                if entry.rule == DEDUP_SUPPRESSED_RULE:
                    suppressed.add(entry.subject_id)
        return suppressed

    def _audit_trail(
        self, session: Session, run_id: str, subject_ids: Iterable[str]
    ) -> list[AuditEntry]:
        """Every audit entry for these subjects, in `sequence` order.

        Ordered on the log's monotonic `sequence` and never on a timestamp --
        there is no clock in `core/` to order it by, which is the point.
        """
        ordered = sorted(set(subject_ids))
        rows: list[Audit] = []
        for start in range(0, len(ordered), _IN_CHUNK):
            chunk = ordered[start : start + _IN_CHUNK]
            rows.extend(
                session.exec(
                    select(Audit).where(
                        self._mine(Audit),
                        Audit.run_id == run_id,
                        col(Audit.subject_id).in_(chunk),
                    )
                ).all()
            )
        entries = [AuditEntry.model_validate_json(row.payload) for row in rows]
        entries.sort(key=lambda entry: entry.sequence)
        return entries


# --- helpers ------------------------------------------------------------------


def _record_kind(record) -> str:
    """`order` | `psp_txn` | `bank_line`, by the record's own type.

    An `isinstance` ladder rather than a field sniff, for the reason the
    contract gives for `RecordsPage.source`: these three shapes carry no
    discriminator, and guessing from which fields are present is how a
    `PSPTransaction` with a null `order_id` becomes a `BankLine`.
    """
    if isinstance(record, Order):
        return "order"
    if isinstance(record, PSPTransaction):
        return "psp_txn"
    if isinstance(record, BankLine):
        return "bank_line"
    raise TypeError(f"not a canonical record: {type(record).__name__}")


def _upload_state(record_count: int, quarantine_count: int) -> str:
    """Which of the three outcomes an upload had. See `UploadSummary`.

    The order of the tests matters: a file that produced records AND
    quarantined some rows is `ingested`, because the records are usable and the
    quarantine sits beside them as a review queue. Only a file that produced
    nothing is a failure -- and which failure it was is exactly the difference
    between a file full of damage and a file with nothing in it.
    """
    if record_count > 0:
        return "ingested"
    if quarantine_count > 0:
        return "quarantined"
    return "empty"


def _as_upload(row: Upload) -> UploadSummary:
    return UploadSummary(
        upload_id=row.upload_id,
        filename=row.filename,
        content_sha256=row.content_sha256,
        byte_size=row.byte_size,
        format_id=row.format_id,
        format_version=row.format_version,
        confidence=row.confidence,
        encoding=row.encoding,
        state=row.state,
        record_count=row.record_count,
        quarantine_count=row.quarantine_count,
        skipped_rows=row.skipped_rows,
        order_count=row.order_count,
        psp_txn_count=row.psp_txn_count,
        bank_line_count=row.bank_line_count,
        uploaded_at=row.uploaded_at,
    )


def _as_summary(run: Run) -> RunSummary:
    return RunSummary(
        run_id=run.run_id,
        seed=run.seed,
        record_count=run.record_count,
        state=run.state,
        created_at=datetime.fromisoformat(run.created_at),
        match_count=run.match_count,
        exception_count=run.exception_count,
        metrics=(
            None if run.metrics_json is None else Metrics.model_validate_json(run.metrics_json)
        ),
    )


def _matched_settlement(
    group: MatchGroup, legs: Sequence[PSPTransaction]
) -> Settlement:
    """A settlement row built from the match that closed it.

    Every money field is the engine's, copied. `payment_leg_count` is counted
    over the legs the `MatchGroup` itself names -- the engine's active set, so a
    suppressed duplicate is not counted twice here after having been discounted
    there.
    """
    claimed = set(group.psp_txn_ids)
    return Settlement(
        settlement_id=group.settlement_id,
        gross=group.gross,
        fees=group.fees,
        tax=group.tax,
        refunds=group.refunds,
        holds=group.holds,
        net=group.net,
        payment_leg_count=payment_leg_count(
            [txn for txn in legs if txn.txn_id in claimed]
        ),
        matched=True,
        bank_line_id=group.bank_line_id,
        match_id=group.match_id,
        tier=group.tier,
    )


def _unmatched_settlement(
    settlement_id: str, legs: Sequence[PSPTransaction]
) -> Settlement:
    """A settlement row for a batch nothing closed, from the matcher's own sum."""
    totals = reconstruct(legs)
    return Settlement(
        settlement_id=settlement_id,
        gross=totals.gross,
        fees=totals.fees,
        tax=totals.tax,
        refunds=totals.refunds,
        holds=totals.holds,
        net=totals.net,
        payment_leg_count=payment_leg_count(legs),
        matched=False,
        bank_line_id=None,
        match_id=None,
        tier=None,
    )


def _with_subject(
    exception: ReconException, subjects: dict[tuple[str, str], SubjectRecord]
) -> ReconExceptionDetail:
    return ReconExceptionDetail(
        **exception.model_dump(),
        subject=subjects[(exception.subject_type, exception.subject_id)],
    )
