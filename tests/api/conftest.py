"""Fixtures for the store and route tests.

Deliberately **no `tests/api/__init__.py`**. `tests/` lands on `sys.path` for the
package-shaped test directories beside this one, so a regular `tests/api`
package would resolve `import api.main` to *this* directory and shadow the real
top-level `api/` package -- the same failure a previous lane hit with
`tests/scorer/__init__.py`. Nothing here collides with a frozen top-level test
basename, so the `__init__.py` is not needed and must not be added.

The sample data is the **real** committed dataset, not an invented one: the
store has to page over the reason codes and subject shapes the engine actually
emits, including the ambiguity trap.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.ingest.reader import read_bank, read_orders, read_psp
from core.matcher.engine import MatchResult, run_match
from core.models import (
    BankLine,
    MatchGroup,
    Order,
    PSPTransaction,
    ReasonCode,
    ReconException,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED42_50 = REPO_ROOT / "fixtures" / "seed42-50"
SEED42_500 = REPO_ROOT / "fixtures" / "seed42-500"

#: The scale-test size the brief pins: pagination must stay stable at 5,000
#: exceptions, because that is what a reviewer pages through on camera.
LARGE_EXCEPTION_COUNT = 5_000

#: The same scale, counted in *input records* rather than exceptions: the
#: settlements, records and matches listings page over the run's inputs, and the
#: brief pins their stability test at 5,000 records too.
LARGE_RECORD_COUNT = 5_000


def read_dataset(directory: Path):
    """The three input record lists, read fresh so no test mutates another's."""
    return (
        read_orders(directory / "orders.csv"),
        read_psp(directory / "psp.csv"),
        read_bank(directory / "bank.csv"),
    )


@pytest.fixture
def sample_records():
    return read_dataset(SEED42_50)


@pytest.fixture
def sample_result(sample_records) -> MatchResult:
    return run_match(*sample_records, run_id="run-sample")


@pytest.fixture
def stamped_at() -> datetime:
    """A fixed boundary timestamp. Tests stamp it; `core/` never reads a clock."""
    return datetime(2026, 8, 28, 9, 30, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def large_result() -> tuple[list[BankLine], MatchResult]:
    """5,000 exceptions over 5,000 bank lines, with the subjects they name.

    Returned as `(bank_lines, result)`: the store resolves each exception's
    subject out of the `records` table, so a pagination test that persisted only
    the exceptions would be testing half the query.

    Synthetic on purpose: this is a pagination stress test, not an engine test,
    and no real dataset produces 5,000 exceptions. `exception_id` is minted the
    way the engine mints it (`exc-<subject_id>`), and the ids are deliberately
    NOT in insertion order -- a page that is stable only because rows happen to
    come back in rowid order is not stable.
    """
    lines: list[BankLine] = []
    exceptions: list[ReconException] = []
    codes = list(ReasonCode)
    for index in range(LARGE_EXCEPTION_COUNT):
        # Shuffled-by-construction ids: the numeric suffix is spread by a stride
        # coprime with the count, so insertion order != exception_id order.
        scrambled = (index * 2_311) % LARGE_EXCEPTION_COUNT
        line_id = f"BL-{scrambled:06d}"
        lines.append(
            BankLine(
                line_id=line_id,
                txn_date=date(2026, 8, 1) + timedelta(days=index % 28),
                narration=f"NEFT RAZORPAY MISC CREDIT {scrambled:06d}",
                credit=100_000 + scrambled,
                debit=None,
                balance=10_000_000,
                utr=None,
            )
        )
        exceptions.append(
            ReconException(
                exception_id=f"exc-{line_id}",
                subject_type="bank_line",
                subject_id=line_id,
                reason_code=codes[index % len(codes)],
                amount=100_000 + scrambled,
                llm_hypothesis=None,
                verifier_verdict="not_attempted",
                verifier_reason=None,
                failed_check=None,
            )
        )
    result = MatchResult(run_id="run-large", exceptions=exceptions, record_count=5_000)
    return lines, result


#: 1,000 settlements x (1 order + 3 PSP legs + 1 bank line) = 5,000 records.
LARGE_SETTLEMENT_COUNT = 1_000

#: How many of them the "engine" closed. The remaining 100 are unmatched
#: settlements, which is the branch of the settlements listing that has no
#: MatchGroup to pass through and must reconstruct from the legs instead.
LARGE_MATCHED_COUNT = 900


@pytest.fixture(scope="session")
def large_run() -> tuple[list[Order], list[PSPTransaction], list[BankLine], MatchResult]:
    """5,000 input records over 1,000 settlements, 900 of them matched.

    Returned as `(orders, psp_txns, bank_lines, result)` so a test can persist
    the whole run and page over any of the three listings.

    Synthetic on purpose: this is a paging stress test, not an engine test, and
    the committed fixtures are 500 records. Two properties are deliberate and
    load-bearing:

    * **Ids are not in insertion order.** The numeric suffix is spread by a
      stride coprime with the count, so a page that is stable only because
      SQLite happened to return rowid order is not stable and this test says so.
    * **100 settlements are left unmatched.** A settlements listing that only
      ever reads the `matches` table would page over 900 rows and silently lose
      the 100 batches that did not close -- which are the ones a reviewer most
      wants to see.
    """
    orders: list[Order] = []
    psp_txns: list[PSPTransaction] = []
    bank_lines: list[BankLine] = []
    matches: list[MatchGroup] = []

    for index in range(LARGE_SETTLEMENT_COUNT):
        scrambled = (index * 311) % LARGE_SETTLEMENT_COUNT
        settlement_id = f"setl_{scrambled:05d}"
        order_id = f"ORD-{scrambled:06d}"
        line_id = f"BL-{scrambled:06d}"
        captured = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
            days=scrambled % 28
        )
        settled = captured.date() + timedelta(days=1)

        gross = 100_000 + scrambled * 100
        fee = gross * 236 // 10_000
        tax = fee * 1_800 // 10_000
        net = gross - fee - tax

        orders.append(
            Order(
                order_id=order_id,
                order_date=captured.date(),
                customer_ref=f"CUST-{scrambled:06d}",
                gross_amount=gross,
                currency="INR",
                status="paid",
            )
        )
        legs = [
            PSPTransaction(
                txn_id=f"pay_{scrambled:06d}",
                txn_type="payment",
                order_id=order_id,
                captured_at=captured,
                amount=gross,
                settlement_id=settlement_id,
                settled_at=settled,
            ),
            PSPTransaction(
                txn_id=f"fee_{scrambled:06d}",
                txn_type="fee",
                order_id=None,
                captured_at=captured,
                amount=-fee,
                settlement_id=settlement_id,
                settled_at=settled,
            ),
            PSPTransaction(
                txn_id=f"tax_{scrambled:06d}",
                txn_type="tax",
                order_id=None,
                captured_at=captured,
                amount=-tax,
                settlement_id=settlement_id,
                settled_at=settled,
            ),
        ]
        psp_txns.extend(legs)
        bank_lines.append(
            BankLine(
                line_id=line_id,
                txn_date=settled,
                narration=f"NEFT RAZORPAY SETTLEMENT {settlement_id}",
                credit=net,
                debit=None,
                balance=50_000_000,
                utr=None,
            )
        )
        if scrambled < LARGE_MATCHED_COUNT:
            matches.append(
                MatchGroup(
                    match_id=f"match-{line_id}",
                    bank_line_id=line_id,
                    settlement_id=settlement_id,
                    psp_txn_ids=[leg.txn_id for leg in legs],
                    order_ids=[order_id],
                    gross=gross,
                    fees=fee,
                    tax=tax,
                    refunds=0,
                    holds=0,
                    net=net,
                    tier=("T0", "T1", "T2", "T3")[scrambled % 4],
                    confidence=1.0,
                    evidence=[f"settlement reference {settlement_id} in narration"],
                )
            )

    assert (
        len(orders) + len(psp_txns) + len(bank_lines) == LARGE_RECORD_COUNT
    ), "the brief pins the scale test at 5,000 records"
    result = MatchResult(
        run_id="run-large-listings",
        matches=matches,
        exceptions=[],
        record_count=LARGE_RECORD_COUNT,
    )
    return orders, psp_txns, bank_lines, result
