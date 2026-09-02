"""SQLModel tables for one reconciliation run (Task D.1).

Six tables for a run -- the run itself, the input `records` it was computed
from, the `matches`, `exceptions` and `audit` entries it produced, and the
`access_log` of who read them -- and three more for the layer in front of it:
`uploads`, `upload_records` and `upload_quarantine`, which hold a merchant's
own file, what reading it produced and what could not be read. An upload
outlives any one run and can feed several, which is why its records are not
rows of `records`.

**Payloads are stored as the frozen pydantic model's own JSON**, with only the
columns a query needs lifted out beside them. That is deliberate. `core/models.py`
is frozen and every field of it -- including the nullability, which is
load-bearing on `BankLine.credit`, `PSPTransaction.order_id` and
`ReconException.failed_check` -- has to reach the wire exactly as the engine
emitted it. A hand-maintained column per field would be a second transcription of
a frozen contract, and the project has already paid for one of those. Round-trip
fidelity is `model_validate_json(model_dump_json(x))`, which is checkable;
column-by-column agreement is eyeballed.

The lifted columns (`reason_code`, `settlement_id`, `subject_id`, `sequence`,
`exception_id`) exist because they are filtered, joined or ordered on. They are
projections of the payload, never a second source of truth: every read
reconstitutes the model from the payload.

**Every table carries `org_id`** (Phase 4, spec section 5). It is not a column
the API layer passes in: `Repo` is constructed for one org and stamps it on
every insert, and every read filters on it -- so tenancy is a property of the
persistence layer rather than a rule route handlers are trusted to remember.
`DEFAULT_ORG_ID` is the column default, which is what makes the migration for
an existing database purely additive: the rows already in a demo database were
written by the single local operator, and filing them under that org is the
correct answer rather than a placeholder.

`created_at` is stored as an ISO-8601 **string**. It is stamped at the API
boundary (LANE-D-api.md 5.5), and a string column round-trips the exact instant
it was handed -- offset included -- where SQLite's DATETIME affinity would
quietly drop the timezone and change the value the run-history table renders.
Nothing in this package produces a timestamp.
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel

#: The tenant every row written by a deployment with no authentication belongs
#: to. Single-user mode is not a mode *without* an org -- it is a mode with
#: exactly one, and that is what keeps the tenancy filter on the same code path
#: in the demo as in a multi-tenant deployment. A filter that is bypassed when
#: auth is off is a filter nobody exercises, and the demo is the configuration
#: this system actually runs in.
#:
#: The value is also the DEFAULT on every `org_id` column, which is what lets a
#: database written before this column existed be migrated by adding it: the
#: rows already there were written by the single local operator, so filing them
#: under this org is the correct answer and not a placeholder.
DEFAULT_ORG_ID = "org-default"


class Run(SQLModel, table=True):
    """One reconciliation run, including its in-flight polling state."""

    __tablename__ = "runs"

    #: The tenant this row belongs to. Every query in `core/store/repo.py`
    #: filters on it; nothing above the repository ever supplies one.
    org_id: str = Field(default=DEFAULT_ORG_ID, index=True)

    run_id: str = Field(primary_key=True)
    dataset_id: str | None = Field(default=None, index=True)
    seed: int
    record_count: int
    #: pending | running | completed | failed. Mirrors RunSummary.state.
    state: str = Field(index=True)
    #: ISO-8601, handed in by `api/`. See the module docstring.
    created_at: str = Field(index=True)
    #: Polled progress (spec 11: background task + polling, not SSE). Written
    #: here rather than held only in memory so `GET /status` is a cheap read and
    #: a job that dies cannot leave the poller reading a stale in-process dict.
    progress: float = Field(default=0.0)
    stage: str = Field(default="queued")
    match_count: int = Field(default=0)
    exception_count: int = Field(default=0)
    #: `Metrics` as JSON, or NULL while the run has none yet.
    metrics_json: str | None = Field(default=None)


class Record(SQLModel, table=True):
    """An input record, kept so an exception's subject can be joined back.

    `core/models.py:ReconException` carries `subject_type` and `subject_id` and
    is frozen, so the record itself is joined at the API boundary -- which is
    the entire reason this table exists.
    """

    __tablename__ = "records"

    #: The tenant this row belongs to. Every query in `core/store/repo.py`
    #: filters on it; nothing above the repository ever supplies one.
    org_id: str = Field(default=DEFAULT_ORG_ID, index=True)

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    #: order | psp_txn | bank_line -- the values `ReconException.subject_type`
    #: takes, so the join needs no translation table.
    record_type: str = Field(index=True)
    record_id: str = Field(index=True)
    payload: str


class Match(SQLModel, table=True):
    __tablename__ = "matches"

    #: The tenant this row belongs to. Every query in `core/store/repo.py`
    #: filters on it; nothing above the repository ever supplies one.
    org_id: str = Field(default=DEFAULT_ORG_ID, index=True)

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    match_id: str = Field(index=True)
    bank_line_id: str = Field(index=True)
    settlement_id: str | None = Field(default=None, index=True)
    payload: str


class Exception_(SQLModel, table=True):
    """A `ReconException`. Trailing underscore: `Exception` is a builtin."""

    __tablename__ = "exceptions"

    #: The tenant this row belongs to. Every query in `core/store/repo.py`
    #: filters on it; nothing above the repository ever supplies one.
    org_id: str = Field(default=DEFAULT_ORG_ID, index=True)

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    #: Ordered on for pagination. Deterministic ordering is the whole stability
    #: guarantee -- see `Repo.exceptions_page`.
    exception_id: str = Field(index=True)
    reason_code: str = Field(index=True)
    subject_type: str
    subject_id: str = Field(index=True)
    payload: str


class Audit(SQLModel, table=True):
    __tablename__ = "audit"

    #: The tenant this row belongs to. Every query in `core/store/repo.py`
    #: filters on it; nothing above the repository ever supplies one.
    org_id: str = Field(default=DEFAULT_ORG_ID, index=True)

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    entry_id: str
    subject_id: str = Field(index=True)
    #: Monotonic within a run; the trail is ordered on this and never on a clock.
    sequence: int = Field(index=True)
    payload: str


class AccessLog(SQLModel, table=True):
    """One row per read of financial data. **Append-only.**

    Audit-on-read, not audit-on-write. The write trail already exists -- the
    `audit` table records what the engine decided and why -- and it answers
    nothing about the threat this table is for: an employee with a valid login
    reading a tenant's settlement data they have no business reading leaves no
    trace at all in a system that only logs changes.

    Append-only is enforced by the *absence* of a code path, not by a database
    grant this deployment could not issue anyway: `core/store/repo.py` has
    exactly two methods that name this table -- one that inserts and one that
    selects -- and `tests/api/test_access_log.py` walks the module's AST to
    prove there is no third. A retention rule will eventually delete from it
    (COMPLIANCE.md, section on DPDP); that will be a documented operation with
    its own path, not a method sitting here waiting to be called.

    `at` is an ISO-8601 string handed in by `api/`, for the same reason
    `Run.created_at` is: nothing in this package reads a clock, and a string
    column round-trips the exact instant including its offset where SQLite's
    DATETIME affinity would silently drop the timezone.
    """

    __tablename__ = "access_log"

    id: int | None = Field(default=None, primary_key=True)

    #: The tenant whose data was read. Indexed: "show me every read of our
    #: data" is the question this table exists to answer.
    org_id: str = Field(default=DEFAULT_ORG_ID, index=True)

    #: WHO. The session subject, or `single-user` when auth is disabled, or
    #: `anonymous` for a request that was refused before it had a session --
    #: a refused read is still an access attempt and is still worth a row.
    actor: str = Field(index=True)

    #: WHAT, as the contract path template rather than the concrete URL:
    #: `/api/runs/{id}/exceptions`, never `/api/runs/run-9f3.../exceptions`.
    #: The template is the resource *kind*, which is what a reviewer groups by;
    #: the identifier travels beside it.
    resource: str = Field(index=True)

    #: WHICH one, when the path names one. NULL for a listing.
    resource_id: str | None = Field(default=None, index=True)

    #: The HTTP method. `GET` today -- writes are audited by the run itself --
    #: and a column rather than an assumption, so adding an audited write later
    #: does not need a migration.
    action: str

    #: WHEN. ISO-8601, stamped at the API boundary. See the class docstring.
    at: str = Field(index=True)

    #: The response status. A 404 or a 401 is the *interesting* row: it is what
    #: enumeration looks like, and a log that recorded only successful reads
    #: would be blind to exactly the behaviour it is meant to catch.
    status: int


class Upload(SQLModel, table=True):
    """One file a merchant sent, and what reading it produced.

    **The content hash is the identity, and `upload_id` is only a name for it.**
    A merchant who re-uploads January's settlement report gets the row that
    already exists rather than a second one; `Repo.upload_for_content` is the
    lookup and `(org_id, content_sha256)` is the pair it keys on. The pair, not
    the hash alone: two tenants who happen to hold the same file are two
    uploads, because an upload id crossing an org boundary would be a tenancy
    leak dressed up as deduplication.

    `uploaded_at` is an ISO-8601 string stamped at the API boundary, for the
    same reason `Run.created_at` is: nothing in this package reads a clock.

    The three per-source counts are stored rather than derived. They are read
    on every listing -- "which of these files is the orders one" is the first
    question the run-from-uploads flow asks -- and a `GROUP BY` per row over a
    5,000-row `upload_records` table is a query per row on a screen that shows
    a page of them.
    """

    __tablename__ = "uploads"

    #: The tenant this row belongs to. Every query in `core/store/repo.py`
    #: filters on it; nothing above the repository ever supplies one.
    org_id: str = Field(default=DEFAULT_ORG_ID, index=True)

    upload_id: str = Field(primary_key=True)
    #: The name the browser sent. Recorded because a human recognises their
    #: file by it -- and never consulted by detection, which reads bytes.
    filename: str
    #: SHA-256 of the raw bytes, which is also the blob store's address.
    content_sha256: str = Field(index=True)
    byte_size: int
    #: The adapter that read it, or `registry.UNREADABLE_FORMAT_ID`.
    format_id: str = Field(index=True)
    format_version: str
    #: The winning adapter's own sniff score, 0.0-1.0. Zero when nothing read
    #: the file, which is a different fact from a low score that still won.
    confidence: float
    #: Which codec decoded it. Empty when nothing did.
    encoding: str
    #: ingested | quarantined | unreadable. See `Repo._upload_state`.
    state: str = Field(index=True)
    record_count: int
    quarantine_count: int
    skipped_rows: int
    order_count: int
    psp_txn_count: int
    bank_line_count: int
    #: ISO-8601, handed in by `api/`. See the class docstring.
    uploaded_at: str = Field(index=True)


class UploadRecord(SQLModel, table=True):
    """One canonical record an upload produced, ready to feed a run.

    Deliberately a separate table from `records`, which holds the inputs of one
    RUN. An upload exists before any run does and can feed several; folding the
    two would mean either copying every row per run or teaching `records` to
    have no run, and the second is how a foreign key becomes a lie.

    `row_sha256` is the adapter's own per-record fingerprint
    (`AdapterResult.row_hashes`), carried through so row-level dedup has
    something to key on. The Phase-1 hashes finally have a consumer.
    """

    __tablename__ = "upload_records"

    org_id: str = Field(default=DEFAULT_ORG_ID, index=True)

    id: int | None = Field(default=None, primary_key=True)
    upload_id: str = Field(index=True)
    #: order | psp_txn | bank_line -- the same three values `records` uses.
    record_type: str = Field(index=True)
    record_id: str = Field(index=True)
    row_sha256: str = Field(index=True)
    payload: str


class UploadQuarantine(SQLModel, table=True):
    """One row an upload could not turn into a record, kept verbatim.

    **`raw` is the merchant's own data and this table is read only through an
    authenticated endpoint.** The no-secrets-in-errors rule extends to it: a
    quarantined row never appears in an error response, only in
    `GET /api/uploads/{id}/quarantine`, which is behind the same session every
    other financial read is behind.
    """

    __tablename__ = "upload_quarantine"

    org_id: str = Field(default=DEFAULT_ORG_ID, index=True)

    id: int | None = Field(default=None, primary_key=True)
    upload_id: str = Field(index=True)
    #: Physical line number, header included, counting from 1.
    row_number: int = Field(index=True)
    raw: str
    #: A `core.adapters.base.QuarantineReason` value. The review screen groups
    #: on it, so the strings are stable.
    reason: str = Field(index=True)
    detail: str
