"""Task D.1 -- SQLite persistence for a run.

The store's job is to round-trip what the engine produced without editorialising
it, and to page over the result deterministically. Both halves are load-bearing:
the first is what makes the API's numbers the engine's numbers, and the second
is what stops a reviewer paging through 5,000 exceptions from seeing a row
twice.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from core.matcher.batch import payment_leg_count, reconstruct
from core.matcher.tiers import TOLERANCE_PAISE
from core.models import ReasonCode
from core.store.repo import Repo, SubjectNotFound
from core.store.schema import DEFAULT_ORG_ID

#: The scale the brief pins for the listings' stability test, and the settlement
#: count `large_run` builds to reach it. The fixture asserts its own totals, so a
#: drift between these and the fixture fails loudly rather than silently
#: shrinking the test.
LARGE_RECORD_COUNT = 5_000
LARGE_SETTLEMENT_COUNT = 1_000


def _repo(tmp_path) -> Repo:
    return Repo(tmp_path / "t.db")


# --- round trip ---------------------------------------------------------------


def test_round_trips_a_result(tmp_path, sample_records, sample_result, stamped_at):
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=50, created_at=stamped_at)
    repo.save_records(run_id, *sample_records)
    repo.save_result(run_id, sample_result)

    summary = repo.summary(run_id)
    assert summary.exception_count == len(sample_result.exceptions)
    assert summary.match_count == len(sample_result.matches)
    assert summary.state == "completed"


def test_created_at_is_persisted_verbatim_and_present_before_the_run_finishes(
    tmp_path, stamped_at
):
    """`created_at` is stamped at the API boundary and only stored here.

    The run-history table renders it for a pending run too, so it must survive
    the round trip on a run that has no result yet -- and it must come back as
    the instant it was handed, not as a value the store invented.
    """
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=50, created_at=stamped_at)

    summary = repo.summary(run_id)
    assert summary.state == "pending"
    assert summary.created_at == stamped_at
    assert summary.metrics is None


def test_the_store_never_reads_a_clock(tmp_path):
    """No wall-clock inside `core/` -- `core/store/` included.

    `tests/test_boundaries.py` polices web imports out of `core/`; this polices
    the other global constraint over the one package in `core/` that is most
    tempted to break it. The store may persist a timestamp it is handed and
    nothing more.
    """
    forbidden = {"now", "utcnow", "today", "time", "monotonic", "perf_counter"}
    for path in (pathlib.Path("core") / "store").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden, (
                    f"{path}:{node.lineno} calls .{node.func.attr}() -- timestamps "
                    "are stamped at the API boundary, never inside core/"
                )


# --- the subject join ---------------------------------------------------------


def test_every_exception_row_carries_its_subject_record(
    tmp_path, sample_records, sample_result, stamped_at
):
    """`PaginatedReconExceptions.items` is exception + subject, never an id.

    `web/` may not read `core/models.py`, so the record has to arrive on the
    wire (spec 13 #3).
    """
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=50, created_at=stamped_at)
    repo.save_records(run_id, *sample_records)
    repo.save_result(run_id, sample_result)

    page = repo.exceptions_page(run_id, page=1, size=100)
    assert page.items
    for item in page.items:
        assert item.subject is not None
        identifier = {
            "order": "order_id",
            "psp_txn": "txn_id",
            "bank_line": "line_id",
        }[item.subject_type]
        assert getattr(item.subject, identifier) == item.subject_id


def test_an_unresolvable_subject_is_raised_not_serialised_as_null(
    tmp_path, sample_result, stamped_at
):
    """An exception whose subject cannot be joined is a bug to surface."""
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=50, created_at=stamped_at)
    repo.save_result(run_id, sample_result)  # records deliberately not saved

    with pytest.raises(SubjectNotFound):
        repo.exceptions_page(run_id, page=1, size=10)


# --- filtering and paging -----------------------------------------------------


def test_exceptions_page_filters_by_reason_code(
    tmp_path, sample_records, sample_result, stamped_at
):
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=50, created_at=stamped_at)
    repo.save_records(run_id, *sample_records)
    repo.save_result(run_id, sample_result)

    page = repo.exceptions_page(
        run_id, page=1, size=10, reason_code="AMBIGUOUS_MULTI_CANDIDATE"
    )
    assert page.items, "the seed-42 dataset carries the ambiguity trap"
    assert all(e.reason_code == "AMBIGUOUS_MULTI_CANDIDATE" for e in page.items)
    # `total` counts the filtered set, not the run -- the pager renders it.
    assert page.total == sum(
        1
        for e in sample_result.exceptions
        if e.reason_code is ReasonCode.AMBIGUOUS_MULTI_CANDIDATE
    )


def test_pagination_is_stable_across_pages(tmp_path, large_result, stamped_at):
    """5,000 exceptions, paged. Page 2 may never repeat a row from page 1.

    Insertion order and `exception_id` order deliberately disagree in the
    fixture, so a query with no ORDER BY passes only by luck of the rowid.
    """
    bank_lines, result = large_result
    total = len(result.exceptions)
    assert total == 5_000, "the brief pins the scale test at 5,000 exceptions"

    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=5_000, created_at=stamped_at)
    repo.save_records(run_id, [], [], bank_lines)
    repo.save_result(run_id, result)

    p1 = repo.exceptions_page(run_id, 1, 50, None)
    p2 = repo.exceptions_page(run_id, 2, 50, None)

    assert len(p1.items) == 50 and len(p2.items) == 50
    assert p1.total == total and p2.total == total
    assert {e.exception_id for e in p1.items} & {e.exception_id for e in p2.items} == set()

    # Stability is stronger than "these two pages are disjoint": walking every
    # page must visit every row exactly once, and re-reading a page must return
    # the same rows in the same order.
    seen: list[str] = []
    for number in range(1, total // 50 + 1):
        seen.extend(
            e.exception_id for e in repo.exceptions_page(run_id, number, 50).items
        )
    assert len(seen) == total
    assert len(set(seen)) == total
    assert seen == sorted(seen), "ORDER BY exception_id is the stability guarantee"
    assert [e.exception_id for e in repo.exceptions_page(run_id, 2, 50).items] == [
        e.exception_id for e in p2.items
    ]


# --- run history --------------------------------------------------------------


def test_list_runs_is_most_recent_first_and_deterministic(tmp_path, stamped_at):
    repo = _repo(tmp_path)
    ids = [
        repo.create_run(
            seed=42, record_count=50, created_at=stamped_at + timedelta(minutes=n)
        )
        for n in range(3)
    ]
    assert [r.run_id for r in repo.list_runs()] == list(reversed(ids))


def test_unknown_run_has_no_summary_and_no_status(tmp_path):
    repo = _repo(tmp_path)
    assert repo.summary("does-not-exist") is None
    assert repo.status("does-not-exist") is None
    assert repo.run_exists("does-not-exist") is False


# --- the settlements listing --------------------------------------------------


def test_settlements_page_lists_every_settlement_the_run_saw(
    tmp_path, sample_records, sample_result, stamped_at
):
    """One row per `settlement_id` on the run's PSP legs -- matched or not.

    A listing built off the `matches` table alone would show only the batches
    that closed, which is the opposite of what a reviewer opens it for.
    """
    orders, psp_txns, bank_lines = sample_records
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=50, created_at=stamped_at)
    repo.save_records(run_id, orders, psp_txns, bank_lines)
    repo.save_result(run_id, sample_result)

    expected = sorted({t.settlement_id for t in psp_txns if t.settlement_id})
    page = repo.settlements_page(run_id, page=1, size=1_000)

    assert page.total == len(expected)
    assert [s.settlement_id for s in page.items] == expected


def test_a_matched_settlement_passes_the_match_group_through_untouched(
    tmp_path, sample_records, sample_result, stamped_at
):
    """Money on a matched row is the engine's, field for field.

    Re-deriving `net` here would be indistinguishable from an engine bug on the
    screen, so the row carries the `MatchGroup`'s own numbers and the tier that
    produced them.
    """
    orders, psp_txns, bank_lines = sample_records
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=50, created_at=stamped_at)
    repo.save_records(run_id, orders, psp_txns, bank_lines)
    repo.save_result(run_id, sample_result)

    rows = {s.settlement_id: s for s in repo.settlements_page(run_id, 1, 1_000).items}
    assert sample_result.matches, "the seed-42 dataset matches something"
    for match in sample_result.matches:
        if match.settlement_id is None:
            continue
        row = rows[match.settlement_id]
        assert row.matched is True
        assert row.bank_line_id == match.bank_line_id
        assert row.match_id == match.match_id
        assert row.tier == match.tier
        for field in ("gross", "fees", "tax", "refunds", "holds", "net"):
            assert getattr(row, field) == getattr(match, field), (
                f"{match.settlement_id}.{field} was re-derived, not passed through"
            )


def test_an_unmatched_settlement_is_reconstructed_from_its_legs(
    tmp_path, sample_records, sample_result, stamped_at
):
    """A batch that never closed still carries a breakdown and a leg count.

    It has no `MatchGroup` to pass through, so the row is
    `core.matcher.batch.reconstruct` over the settlement's legs -- the matcher's
    own function, never a second sum written here.
    """
    orders, psp_txns, bank_lines = sample_records
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=50, created_at=stamped_at)
    repo.save_records(run_id, orders, psp_txns, bank_lines)
    repo.save_result(run_id, sample_result)

    matched = {m.settlement_id for m in sample_result.matches}
    unmatched = [
        s for s in repo.settlements_page(run_id, 1, 1_000).items if not s.matched
    ]
    assert unmatched, "the seed-42 dataset leaves batches unclosed"

    legs: dict[str, list] = {}
    for txn in psp_txns:
        if txn.settlement_id:
            legs.setdefault(txn.settlement_id, []).append(txn)

    for row in unmatched:
        assert row.settlement_id not in matched
        assert row.bank_line_id is None and row.match_id is None and row.tier is None
        totals = reconstruct(legs[row.settlement_id])
        assert (row.gross, row.fees, row.tax, row.refunds, row.holds, row.net) == (
            totals.gross,
            totals.fees,
            totals.tax,
            totals.refunds,
            totals.holds,
            totals.net,
        )
        assert row.payment_leg_count == payment_leg_count(legs[row.settlement_id])


def test_the_two_derivations_of_a_settlement_breakdown_agree(
    tmp_path, sample_records, sample_result, stamped_at
):
    """The reconciliation guard.

    A matched row's money comes from the `MatchGroup`; an unmatched row's comes
    from `reconstruct` over the settlement's *active* legs. Those are two
    derivations in one list, so they must be shown to agree wherever both exist
    -- otherwise the list is two different things depending on the row, and the
    netting diagram it links to disagrees with the row that opened it.

    "Active" is the engine's own leg set: this test runs over
    `fixtures/seed42-50`, which suppresses a duplicate payment leg inside 1 of
    its matched settlements (`fixtures/seed42-500` does so inside 7), and
    reconstructing those from the raw PSP rows lands the gross millions of paise
    high.

    The one place they legitimately differ is `net` on a **T3** match, and it is
    not a defect: `MatchGroup.net` is defined as the bank credit
    (`core/matcher/tiers.py:_build`), so on a residual break it sits up to
    `TOLERANCE_PAISE` away from the reconstruction and the difference lives in
    the evidence. The breakdown fields still agree exactly, which is what makes
    the discrepancy legible rather than mysterious.
    """
    orders, psp_txns, bank_lines = sample_records
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=50, created_at=stamped_at)
    repo.save_records(run_id, orders, psp_txns, bank_lines)
    repo.save_result(run_id, sample_result)

    rows = {s.settlement_id: s for s in repo.settlements_page(run_id, 1, 1_000).items}
    assert sample_result.matches, "the seed-42 dataset matches something"
    residuals = 0
    for match in sample_result.matches:
        if match.settlement_id is None:
            continue
        active = repo.settlement_legs(run_id, match.settlement_id)
        totals = reconstruct(active)

        for field in ("gross", "fees", "tax", "refunds", "holds"):
            assert getattr(totals, field) == getattr(match, field), (
                f"{match.settlement_id}.{field}: reconstruct over the active "
                f"legs disagrees with the MatchGroup the engine emitted"
            )
        if totals.net != match.net:
            residuals += 1
            assert match.tier == "T3", (
                f"{match.settlement_id}: only a T3 residual break may put "
                f"MatchGroup.net away from the reconstruction, not {match.tier}"
            )
            assert abs(totals.net - match.net) <= TOLERANCE_PAISE
        assert rows[match.settlement_id].payment_leg_count == payment_leg_count(active)

    assert residuals, "the seed-42 dataset carries at least one T3 residual break"


def test_settlements_pagination_is_stable_at_5000_records(
    tmp_path, large_run, stamped_at
):
    """5,000 records, 1,000 settlements, paged. Page 2 may never repeat page 1."""
    orders, psp_txns, bank_lines, result = large_run
    assert len(orders) + len(psp_txns) + len(bank_lines) == LARGE_RECORD_COUNT, (
        "the brief pins the stability test at 5,000 records"
    )
    assert len(orders) == LARGE_SETTLEMENT_COUNT

    repo = _repo(tmp_path)
    run_id = repo.create_run(
        seed=42, record_count=LARGE_RECORD_COUNT, created_at=stamped_at
    )
    repo.save_records(run_id, orders, psp_txns, bank_lines)
    repo.save_result(run_id, result)

    first = repo.settlements_page(run_id, 1, 100)
    second = repo.settlements_page(run_id, 2, 100)
    assert first.total == second.total == LARGE_SETTLEMENT_COUNT
    assert len(first.items) == len(second.items) == 100
    assert {s.settlement_id for s in first.items} & {
        s.settlement_id for s in second.items
    } == set()

    seen: list[str] = []
    for number in range(1, LARGE_SETTLEMENT_COUNT // 100 + 1):
        seen += [s.settlement_id for s in repo.settlements_page(run_id, number, 100).items]
    assert len(seen) == LARGE_SETTLEMENT_COUNT
    assert len(set(seen)) == LARGE_SETTLEMENT_COUNT
    assert seen == sorted(seen), "ORDER BY settlement_id is the stability guarantee"
    assert [s.settlement_id for s in repo.settlements_page(run_id, 2, 100).items] == [
        s.settlement_id for s in second.items
    ]
    assert sum(1 for s in repo.settlements_page(run_id, 1, 5_000).items if s.matched) == len(
        result.matches
    )


# --- the records listing ------------------------------------------------------


def test_records_page_returns_one_source_at_a_time(
    tmp_path, sample_records, sample_result, stamped_at
):
    """`source` selects the shape. A page mixing three shapes with no
    discriminator would be unusable -- the contract forbids sniffing fields."""
    orders, psp_txns, bank_lines = sample_records
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=50, created_at=stamped_at)
    repo.save_records(run_id, orders, psp_txns, bank_lines)
    repo.save_result(run_id, sample_result)

    for source, expected, identifier in (
        ("order", orders, "order_id"),
        ("psp_txn", psp_txns, "txn_id"),
        ("bank_line", bank_lines, "line_id"),
    ):
        page = repo.records_page(run_id, source, page=1, size=5_000)
        assert page.source == source
        assert page.total == len(expected)
        assert [getattr(item, identifier) for item in page.items] == sorted(
            getattr(item, identifier) for item in expected
        )


def test_a_record_round_trips_through_the_listing_unchanged(
    tmp_path, sample_records, sample_result, stamped_at
):
    """The listing is the ingested row, not a projection of it.

    Nullability is the part that matters: `PSPTransaction.order_id` absent is
    the missing_order_ref defect and `BankLine.credit` null is a debit line.
    Both have to survive to the wire as they were read.
    """
    orders, psp_txns, bank_lines = sample_records
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=50, created_at=stamped_at)
    repo.save_records(run_id, orders, psp_txns, bank_lines)
    repo.save_result(run_id, sample_result)

    listed = {
        item.txn_id: item
        for item in repo.records_page(run_id, "psp_txn", 1, 5_000).items
    }
    assert any(t.order_id is None for t in psp_txns), (
        "the seed-42 dataset carries the missing_order_ref defect"
    )
    for txn in psp_txns:
        assert listed[txn.txn_id] == txn


def test_records_pagination_is_stable_at_5000_records(tmp_path, large_run, stamped_at):
    """The scale the brief pins. Page 2 may never repeat a row from page 1.

    The fixture's ids are spread by a stride coprime with the count, so
    insertion order and id order disagree and a query with no ORDER BY passes
    only by luck of the rowid.
    """
    orders, psp_txns, bank_lines, result = large_run
    assert len(orders) + len(psp_txns) + len(bank_lines) == LARGE_RECORD_COUNT

    repo = _repo(tmp_path)
    run_id = repo.create_run(
        seed=42, record_count=LARGE_RECORD_COUNT, created_at=stamped_at
    )
    repo.save_records(run_id, orders, psp_txns, bank_lines)
    repo.save_result(run_id, result)

    walked = 0
    for source, identifier, expected in (
        ("order", "order_id", len(orders)),
        ("psp_txn", "txn_id", len(psp_txns)),
        ("bank_line", "line_id", len(bank_lines)),
    ):
        first = repo.records_page(run_id, source, 1, 100)
        second = repo.records_page(run_id, source, 2, 100)
        assert first.total == second.total == expected
        assert {getattr(i, identifier) for i in first.items} & {
            getattr(i, identifier) for i in second.items
        } == set()

        seen: list[str] = []
        for number in range(1, expected // 100 + 1):
            seen += [
                getattr(item, identifier)
                for item in repo.records_page(run_id, source, number, 100).items
            ]
        assert len(seen) == expected
        assert len(set(seen)) == expected, f"{source}: a row was served twice"
        assert seen == sorted(seen), "ORDER BY record_id is the stability guarantee"
        assert [
            getattr(i, identifier) for i in repo.records_page(run_id, source, 2, 100).items
        ] == [getattr(i, identifier) for i in second.items]
        walked += len(seen)

    assert walked == LARGE_RECORD_COUNT, "every record was reachable exactly once"


def test_an_unknown_source_is_rejected_rather_than_returned_empty(
    tmp_path, sample_records, sample_result, stamped_at
):
    """An empty page for a typo'd source reads as "this run has no orders"."""
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=50, created_at=stamped_at)
    repo.save_records(run_id, *sample_records)
    repo.save_result(run_id, sample_result)

    with pytest.raises(ValueError):
        repo.records_page(run_id, "orders", 1, 10)


# --- the matches listing ------------------------------------------------------


def test_matches_page_lists_the_accepted_matches(
    tmp_path, sample_records, sample_result, stamped_at
):
    """The engine's `MatchGroup`s, ordered by `match_id`, with nothing added.

    A reviewer who can only see what failed cannot check what succeeded, which
    is the half of the run the match rate is actually made of.
    """
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=50, created_at=stamped_at)
    repo.save_records(run_id, *sample_records)
    repo.save_result(run_id, sample_result)

    page = repo.matches_page(run_id, page=1, size=5_000)
    assert page.total == len(sample_result.matches)
    assert page.items == sorted(sample_result.matches, key=lambda m: m.match_id)
    assert all(item.tier and item.evidence for item in page.items), (
        "the tier and the evidence lines are the point of the listing"
    )


def test_matches_pagination_is_stable_at_5000_records(tmp_path, large_run, stamped_at):
    orders, psp_txns, bank_lines, result = large_run
    assert len(orders) + len(psp_txns) + len(bank_lines) == LARGE_RECORD_COUNT

    repo = _repo(tmp_path)
    run_id = repo.create_run(
        seed=42, record_count=LARGE_RECORD_COUNT, created_at=stamped_at
    )
    repo.save_records(run_id, orders, psp_txns, bank_lines)
    repo.save_result(run_id, result)

    total = len(result.matches)
    first = repo.matches_page(run_id, 1, 100)
    second = repo.matches_page(run_id, 2, 100)
    assert first.total == second.total == total
    assert {m.match_id for m in first.items} & {m.match_id for m in second.items} == set()

    seen: list[str] = []
    for number in range(1, total // 100 + 1):
        seen += [m.match_id for m in repo.matches_page(run_id, number, 100).items]
    assert len(seen) == total
    assert len(set(seen)) == total
    assert seen == sorted(seen), "ORDER BY match_id is the stability guarantee"
    assert [m.match_id for m in repo.matches_page(run_id, 2, 100).items] == [
        m.match_id for m in second.items
    ]


# --- the org_id migration -----------------------------------------------------
#
# The database this has to work on is somebody's existing `out/recon.db` with
# real runs in it. "Delete your database" is the kind of migration note that
# turns a security feature into a reason not to upgrade, so the column is added
# additively and the rows already there are filed under DEFAULT_ORG_ID -- which
# is their correct owner, not a placeholder: they were written by a deployment
# that had exactly one operator.


def _downgrade(path: pathlib.Path) -> None:
    """Strip `org_id` back off a database, producing a genuine pre-migration file.

    Built by removing the column rather than by hand-writing the old `CREATE
    TABLE` statements. A transcribed copy of the previous schema would be a
    second definition of it living in a test file, free to drift from what the
    branch point actually wrote; removing the column from what this branch
    writes cannot drift, because there is only ever one schema in the repo.

    SQLite refuses `DROP COLUMN` on an indexed column, so the index goes first
    -- which is also a check that the migration created one.
    """
    import sqlite3

    with sqlite3.connect(path) as connection:
        for table in Repo._TENANT_TABLES:
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            assert "org_id" in columns, f"{table} has no org_id to remove"
            connection.execute(f"DROP INDEX IF EXISTS ix_{table}_org_id")
            connection.execute(f"ALTER TABLE {table} DROP COLUMN org_id")
        connection.commit()


def _columns(path: pathlib.Path, table: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(path) as connection:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_a_database_written_before_org_id_existed_is_upgraded_in_place(
    tmp_path, sample_records, sample_result, stamped_at
):
    """The migration, end to end, on a file that has real rows in it.

    Three claims, and the third is the one that matters:

    * the column appears on every tenanted table;
    * every row that was already there reads back under `DEFAULT_ORG_ID`;
    * **nothing is lost** -- the run, its records, its matches, its exceptions
      and its audit trail all come back through the ordinary read path, with
      the same counts they went in with.
    """
    database = tmp_path / "pre-migration.db"

    before = Repo(database)
    run_id = before.create_run(seed=42, record_count=50, created_at=stamped_at)
    before.save_records(run_id, *sample_records)
    before.save_result(run_id, sample_result)
    expected = {
        "matches": before.matches_page(run_id, size=500).total,
        "exceptions": before.exceptions_page(run_id, size=500).total,
        "orders": before.records_page(run_id, "order", size=500).total,
        "trail": len(
            before.exception_detail(
                before.exceptions_page(run_id, size=1).items[0].exception_id
            ).audit_trail
        ),
    }
    assert all(expected.values()), f"the fixture must produce rows: {expected}"
    before._engine.dispose()

    _downgrade(database)
    for table in Repo._TENANT_TABLES:
        assert "org_id" not in _columns(database, table), table

    after = Repo(database)

    for table in Repo._TENANT_TABLES:
        assert "org_id" in _columns(database, table), f"{table} was not migrated"

    summary = after.summary(run_id)
    assert summary is not None, "the pre-existing run must survive the upgrade"
    assert summary.created_at == stamped_at
    assert after.matches_page(run_id, size=500).total == expected["matches"]
    assert after.exceptions_page(run_id, size=500).total == expected["exceptions"]
    assert after.records_page(run_id, "order", size=500).total == expected["orders"]
    first = after.exceptions_page(run_id, size=1).items[0].exception_id
    assert len(after.exception_detail(first).audit_trail) == expected["trail"]


def test_migrated_rows_belong_to_the_default_org_and_to_no_other(
    tmp_path, sample_records, sample_result, stamped_at
):
    """Which org the pre-existing rows land in, asserted through the filter.

    Not by reading the column -- by asking a *different* org for the same run
    and getting nothing. That is the property the migration has to deliver:
    the rows are owned, not merely stamped.
    """
    database = tmp_path / "pre-migration.db"
    before = Repo(database)
    run_id = before.create_run(seed=42, record_count=50, created_at=stamped_at)
    before.save_records(run_id, *sample_records)
    before.save_result(run_id, sample_result)
    before._engine.dispose()
    _downgrade(database)

    after = Repo(database)
    assert after.org_id == DEFAULT_ORG_ID
    assert after.summary(run_id) is not None
    assert [s.run_id for s in after.list_runs()] == [run_id]

    somebody_else = after.scoped("org-somebody-else")
    assert somebody_else.summary(run_id) is None
    assert somebody_else.list_runs() == []
    assert somebody_else.run_exists(run_id) is False


def test_the_migration_is_idempotent(tmp_path, stamped_at):
    """Opening a migrated database again must be a no-op, not a second ALTER.

    `Repo` is constructed on every process start and `api/deps.py` caches one
    per file, so this runs on every boot of the API against a database that has
    already been through it.
    """
    database = tmp_path / "twice.db"
    first = Repo(database)
    run_id = first.create_run(seed=1, record_count=10, created_at=stamped_at)
    first._engine.dispose()

    for _ in range(3):
        again = Repo(database)
        assert again.summary(run_id) is not None
        again._engine.dispose()


def test_a_scoped_repo_shares_the_engine_it_was_scoped_from(tmp_path):
    """`scoped()` is a view, not a second database handle.

    `api/deps.py` caches one `Repo` per file precisely because the engine owns
    a connection pool; a per-request org that opened its own engine would open
    a SQLite connection per request and the 500 ms status poll would pay for
    it. Asserted rather than trusted, because the cost is invisible until it
    is not.
    """
    root = Repo(tmp_path / "shared.db")
    scoped = root.scoped("org-other")
    assert scoped is not root
    assert scoped._engine is root._engine
    assert scoped.org_id == "org-other"
    # Memoised, and reflexive: scoping back finds the original.
    assert root.scoped("org-other") is scoped
    assert scoped.scoped(DEFAULT_ORG_ID) is root
    assert root.scoped(DEFAULT_ORG_ID) is root


def test_two_orgs_writing_the_same_run_id_do_not_collide_in_a_listing(
    tmp_path, stamped_at
):
    """`run_id` is minted per run, so this is a synthetic collision -- and it is
    exactly the shape of the bug worth ruling out: a query that filtered by id
    and forgot the org would return the other tenant's row for a guessed id."""
    root = Repo(tmp_path / "collide.db")
    other = root.scoped("org-other")
    root.create_run(seed=1, record_count=10, created_at=stamped_at, run_id="run-x")
    other.create_run(seed=2, record_count=20, created_at=stamped_at, run_id="run-y")

    assert root.summary("run-x").seed == 1
    assert root.summary("run-y") is None
    assert other.summary("run-y").seed == 2
    assert other.summary("run-x") is None
    assert [s.run_id for s in root.list_runs()] == ["run-x"]
    assert [s.run_id for s in other.list_runs()] == ["run-y"]
