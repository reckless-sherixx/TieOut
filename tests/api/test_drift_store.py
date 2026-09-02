"""The two reads drift needs from the store, and the one performance claim
`core/drift/` cannot make for itself.

`Repo.reason_code_census` is the source of the reason-code distributions §7
compares. It must group **in SQL**: a 5,000-record run has hundreds of
exceptions, drift is meant to be cheap enough to call on every run, and the
obvious Python implementation would reconstitute every `ReconException` from its
JSON payload to count eight buckets.

That claim is not something an assertion on the returned dict can check -- a
Python-side `Counter` returns exactly the same dict. So the SQL this method
emits is captured off the engine and checked directly. This is the same reason
`tests/api/test_store.py` checks the store's `ORDER BY` by walking every page
rather than by trusting the docstring.

`Repo.previous_completed_run` is the default baseline: absent `?against=`, the
immediately previous **completed** run on the same dataset.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import event

from core.models import ReasonCode
from core.store.repo import Repo


def _repo(tmp_path) -> Repo:
    return Repo(tmp_path / "t.db")


def _completed(repo: Repo, *, dataset_id, created_at, result=None, records=None):
    run_id = repo.create_run(
        seed=42, record_count=50, created_at=created_at, dataset_id=dataset_id
    )
    if records is not None:
        repo.save_records(run_id, *records)
    if result is not None:
        repo.save_result(run_id, result)
    else:
        repo.set_progress(run_id, state="completed", progress=1.0, stage="complete")
    return run_id


class _CapturedSql:
    """Every statement the engine executes while this is attached."""

    def __init__(self, repo: Repo) -> None:
        self._engine = repo._engine
        self.statements: list[str] = []

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        event.remove(self._engine, "before_cursor_execute", self._record)
        return False

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(" ".join(statement.split()))


# --- the census ---------------------------------------------------------------


def test_the_census_counts_every_reason_code_the_run_recorded(
    tmp_path, sample_records, sample_result, stamped_at
):
    repo = _repo(tmp_path)
    run_id = _completed(
        repo,
        dataset_id="ds-a",
        created_at=stamped_at,
        result=sample_result,
        records=sample_records,
    )

    census = repo.reason_code_census(run_id)

    expected: dict[str, int] = {}
    for exception in sample_result.exceptions:
        expected[exception.reason_code.value] = (
            expected.get(exception.reason_code.value, 0) + 1
        )
    assert census == expected
    assert sum(census.values()) == len(sample_result.exceptions)
    assert census, "the seed-42 dataset produces exceptions"


def test_a_code_that_never_fired_is_absent_rather_than_zero(
    tmp_path, sample_records, sample_result, stamped_at
):
    """The census is what the run recorded, not a template. `compare()` reads a
    missing key as 0, which is what makes `appeared` mean "absent before"."""
    repo = _repo(tmp_path)
    run_id = _completed(
        repo,
        dataset_id="ds-a",
        created_at=stamped_at,
        result=sample_result,
        records=sample_records,
    )
    census = repo.reason_code_census(run_id)
    assert set(census) < {code.value for code in ReasonCode}
    assert all(count > 0 for count in census.values())


def test_the_census_of_an_unknown_run_is_empty(tmp_path):
    assert _repo(tmp_path).reason_code_census("does-not-exist") == {}


def test_the_census_is_scoped_to_one_run(
    tmp_path, sample_records, sample_result, stamped_at
):
    repo = _repo(tmp_path)
    first = _completed(
        repo,
        dataset_id="ds-a",
        created_at=stamped_at,
        result=sample_result,
        records=sample_records,
    )
    second = _completed(repo, dataset_id="ds-a", created_at=stamped_at + timedelta(1))
    assert repo.reason_code_census(second) == {}
    assert repo.reason_code_census(first) != {}


def test_the_census_is_grouped_in_sql_and_never_loads_a_row(
    tmp_path, large_result, stamped_at
):
    """5,000 exceptions, counted without reconstituting one of them.

    Three separate claims, because "it returned the right dict" proves none of
    them: one statement, a GROUP BY inside it, and the `payload` column -- the
    JSON every `ReconException` would have to be parsed out of -- never
    selected.
    """
    bank_lines, result = large_result
    repo = _repo(tmp_path)
    run_id = repo.create_run(seed=42, record_count=5_000, created_at=stamped_at)
    repo.save_records(run_id, [], [], bank_lines)
    repo.save_result(run_id, result)

    with _CapturedSql(repo) as captured:
        census = repo.reason_code_census(run_id)

    assert sum(census.values()) == 5_000
    assert len(captured.statements) == 1, (
        "one query. A census that costs a statement per reason code is a census "
        f"drift cannot afford on every run: {captured.statements}"
    )
    sql = captured.statements[0].lower()
    assert "group by" in sql, f"the counting must happen in SQL: {sql}"
    assert "count(" in sql, f"the counting must happen in SQL: {sql}"
    assert "payload" not in sql, (
        "the census selected the payload column, so every exception's JSON was "
        f"read to count eight buckets: {sql}"
    )


def test_the_census_returns_plain_ints_a_drift_report_can_take(
    tmp_path, sample_records, sample_result, stamped_at
):
    repo = _repo(tmp_path)
    run_id = _completed(
        repo,
        dataset_id="ds-a",
        created_at=stamped_at,
        result=sample_result,
        records=sample_records,
    )
    census = repo.reason_code_census(run_id)
    assert all(isinstance(k, str) for k in census)
    assert all(type(v) is int for v in census.values())


# --- the default baseline -----------------------------------------------------


def test_the_previous_completed_run_on_the_same_dataset_is_the_baseline(
    tmp_path, stamped_at
):
    repo = _repo(tmp_path)
    older = _completed(repo, dataset_id="ds-a", created_at=stamped_at)
    newer = _completed(repo, dataset_id="ds-a", created_at=stamped_at + timedelta(1))
    latest = _completed(repo, dataset_id="ds-a", created_at=stamped_at + timedelta(2))

    assert repo.previous_completed_run(latest).run_id == newer
    assert repo.previous_completed_run(newer).run_id == older
    assert repo.previous_completed_run(older) is None


def test_a_run_on_another_dataset_is_not_the_baseline(tmp_path, stamped_at):
    repo = _repo(tmp_path)
    _completed(repo, dataset_id="ds-b", created_at=stamped_at)
    current = _completed(repo, dataset_id="ds-a", created_at=stamped_at + timedelta(1))
    assert repo.previous_completed_run(current) is None


def test_a_run_that_did_not_complete_is_not_the_baseline(tmp_path, stamped_at):
    """"the immediately previous **completed** run". A failed run has no
    metrics and a running one has not finished producing them."""
    repo = _repo(tmp_path)
    good = _completed(repo, dataset_id="ds-a", created_at=stamped_at)
    for offset, state in ((1, "failed"), (2, "running"), (3, "pending")):
        run_id = repo.create_run(
            seed=42,
            record_count=50,
            created_at=stamped_at + timedelta(offset),
            dataset_id="ds-a",
        )
        repo.set_progress(run_id, state=state)

    current = _completed(repo, dataset_id="ds-a", created_at=stamped_at + timedelta(4))
    assert repo.previous_completed_run(current).run_id == good


def test_the_baseline_is_strictly_earlier_and_never_the_run_itself(
    tmp_path, stamped_at
):
    repo = _repo(tmp_path)
    only = _completed(repo, dataset_id="ds-a", created_at=stamped_at)
    assert repo.previous_completed_run(only) is None


def test_two_runs_stamped_in_the_same_instant_break_the_tie_deterministically(
    tmp_path, stamped_at
):
    """`list_runs` orders on `created_at` then `run_id` for exactly this case;
    the baseline lookup must agree with it or the drift report and the run
    history would disagree about which run came before which."""
    repo = _repo(tmp_path)
    a = _completed(repo, dataset_id="ds-a", created_at=stamped_at)
    b = repo.create_run(
        seed=42, record_count=50, created_at=stamped_at, dataset_id="ds-a"
    )
    repo.set_progress(b, state="completed")
    earlier, later = sorted([a, b])

    assert repo.previous_completed_run(later).run_id == earlier
    assert repo.previous_completed_run(earlier) is None


def test_the_baseline_of_an_unknown_run_is_refused_not_nulled(tmp_path):
    """`None` already means "no earlier run on this dataset". An unknown run is
    a different fact and gets a different answer, so the route can render one as
    404 and the other as its own message."""
    from core.store.repo import UnknownRun

    with pytest.raises(UnknownRun):
        _repo(tmp_path).previous_completed_run("does-not-exist")


def test_the_baseline_lookup_is_one_query(tmp_path, stamped_at):
    repo = _repo(tmp_path)
    _completed(repo, dataset_id="ds-a", created_at=stamped_at)
    current = _completed(repo, dataset_id="ds-a", created_at=stamped_at + timedelta(1))

    with _CapturedSql(repo) as captured:
        repo.previous_completed_run(current)

    assert len(captured.statements) <= 2, (
        "the current run's own row plus the baseline lookup; a scan of the run "
        f"history in Python would be neither: {captured.statements}"
    )
    assert any("limit" in s.lower() for s in captured.statements), (
        "the baseline is one row, selected in SQL, not the head of a list "
        f"filtered in Python: {captured.statements}"
    )
