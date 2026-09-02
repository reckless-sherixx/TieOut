"""Dataset assembly (Task A.4): batches, then defects, then disambiguation.

`build_dataset(seed, count)` is the whole generator in one call. It is pure --
no clock, no filesystem, no global RNG -- so `emit_dataset(*build_dataset(...))`
is byte-identical on every machine for a given seed.

Three things happen here that no single injector could do for itself.

**The T1 guarantee.** The first settlement is built with exactly one payment leg
and stamped `RESERVED_CLEAN`, which makes it ineligible for every injector. Spec
7 separates matcher tier T1 from T2 by payment-leg cardinality, and
`fixtures/tiny/` structurally cannot supply a clean single-payment settlement --
both of the two it has are spoken for. Without this reservation a whole tier can
go untested at every scale and nothing in the project would notice.

**Distinct settlement grosses.** Two settlements that reconstruct to the same
net on the same day are an ambiguity trap. One of those is deliberate and
labelled; a second, accidental one would be scored as a matcher failure it had
no way to avoid. Grosses are kept distinct as batches are built, which makes an
accidental collision nearly impossible...

**...and `_disambiguate_bank_lines` closes the rest of that gap** after
injection, when refunds and holds have moved the nets around. It shifts a
colliding bank line's `txn_date` within its own two-day settlement window --
touching no amount and no PSP row -- and it exempts the labelled trap pair,
whose collision is the entire point.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta

from .batches import (
    START_DATE,
    Batch,
    IdAllocator,
    apply_credit,
    generate_clean_batch,
    payment_gross,
    recompute_fee_tax,
)
from .defects import (
    RESERVED_CLEAN,
    InjectionResult,
    resolve_defect_mix,
    run_injections,
)
from .rng import SeededRng

#: Orders per settlement, drawn uniformly from this tuple -- so the weights are
#: the repeats. Mean 3.1. Single-order settlements are 20% of the run: they are
#: legal, they are present in `fixtures/tiny/`, and they are the only shape that
#: exercises tier T1 (spec 10 #1).
SETTLEMENT_SIZES: tuple[int, ...] = (1, 1, 2, 2, 3, 3, 4, 4, 5, 6)

#: Days between one settlement cycle and the next.
_CYCLE_GAP = (1, 3)


def build_dataset(
    seed: int,
    count: int,
    defect_mix: Mapping[str, int] | None = None,
) -> tuple[list[Batch], list[InjectionResult]]:
    """Build `count` orders' worth of settlements and inject the defect mix.

    `count` is the number of rows in `orders.csv` -- the record spine the scale
    runs (50 / 500 / 5,000) count, and what `truth.json`'s `record_count` holds.

    `defect_mix` overrides individual entries of `DEFAULT_DEFECT_MIX`; **omitted
    or `None` means use the default**, which is what makes `--seed 42 --count
    500` the same adversarial dataset for the demo, the API and every scale run.
    """
    if count < 1:
        raise ValueError("count must be at least 1")

    rng = SeededRng(seed)
    ids = IdAllocator()
    batches: list[Batch] = []
    index = 0

    def next_settlement_id() -> str:
        nonlocal index
        index += 1
        return f"setl_{index:05d}"

    # The reserved T1 case: one payment leg, no defect, ever.
    settle: date = START_DATE
    reserved = generate_clean_batch(
        rng, next_settlement_id(), 1, ids=ids, settled_at=settle
    )
    reserved.defect_tags.add(RESERVED_CLEAN)
    batches.append(reserved)

    used_gross = {payment_gross(reserved)}
    remaining = count - 1
    while remaining > 0:
        settle = settle + timedelta(days=rng.randint(*_CYCLE_GAP))
        size = min(rng.choice(SETTLEMENT_SIZES), remaining)
        batch = generate_clean_batch(
            rng, next_settlement_id(), size, ids=ids, settled_at=settle
        )
        _make_gross_distinct(batch, used_gross)
        used_gross.add(payment_gross(batch))
        batches.append(batch)
        remaining -= size

    injections = run_injections(rng, batches, resolve_defect_mix(count, defect_mix))
    _disambiguate_bank_lines(batches, injections)
    return batches, injections


def _make_gross_distinct(batch: Batch, used_gross: set[int]) -> None:
    """Nudge the last order by whole rupees until this batch's gross is unique.

    Runs before any injection, so nothing depends on the amount yet -- no
    duplicate row to keep in step, no refund sized against the net.
    """
    if payment_gross(batch) not in used_gross:
        return
    order = batch.orders[-1]
    leg = next(t for t in batch.payment_legs if t.order_id == order.order_id)
    while payment_gross(batch) in used_gross:
        order.gross_amount += 100
        leg.amount = order.gross_amount
        recompute_fee_tax(batch)
    apply_credit(batch)


def _disambiguate_bank_lines(
    batches: list[Batch], injections: list[InjectionResult]
) -> None:
    """Ensure no two bank lines share a `(txn_date, credit)` except the trap.

    A collision is an ambiguity trap whether or not anyone meant it. The
    labelled one is recorded in `unresolvable_ids` and the scorer expects the
    matcher to decline it; an unlabelled one looks identical to the matcher and
    is scored as a failure it could not have avoided, which quietly turns
    `false_match_rate` into noise.

    The repair moves a `txn_date` inside the settlement's own two-day window.
    That is the only lever here that changes nothing else: every amount, every
    PSP row and every linkage is left exactly as the injectors left it.

    That window is anchored on `settled_at + bank_delay_days`, not on
    `settled_at`. For every settlement but one the delay is zero and the two
    are the same date. For `obfuscated_settlement_ref` the delay is the defect:
    the credit posts days after the cycle, which is what keeps the subject out
    of the matcher's reach. Repairing a collision on that line by re-deriving
    its date from `settled_at` would pull it back onto its own cycle, hand it
    to a reconstruction tier, and delete the defect -- silently, and with every
    test still green, because the line would simply be matched.
    """
    trap_ids = {
        line_id
        for result in injections
        if not result.resolvable
        for line_id in result.affected_ids
    }
    seen: set[tuple[date, int | None]] = set()

    # The trap's own key is reserved first, so nothing else can land on it and
    # turn a two-way ambiguity into a three-way one.
    for batch in batches:
        for line in batch.all_bank_lines:
            if line.line_id in trap_ids:
                seen.add((line.txn_date, line.credit))

    for batch in batches:
        for line in batch.all_bank_lines:
            if line.line_id in trap_ids:
                continue
            if (line.txn_date, line.credit) not in seen:
                seen.add((line.txn_date, line.credit))
                continue
            posted = batch.settled_at + timedelta(days=batch.bank_delay_days)
            for delta in (1, 2, 0):
                candidate = posted + timedelta(days=delta)
                if (candidate, line.credit) not in seen:
                    line.txn_date = candidate
                    seen.add((candidate, line.credit))
                    break
            else:
                raise RuntimeError(
                    f"{line.line_id}: cannot separate this line from another with "
                    f"credit {line.credit} without moving it outside the two-day "
                    f"window of {batch.settlement_id}'s posting date {posted}"
                )
