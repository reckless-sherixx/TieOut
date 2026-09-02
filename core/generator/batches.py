"""Clean settlement batches (Task A.2).

A `Batch` is one settlement: its orders, its PSP legs, the bank line(s) that
paid it, and the truth linkage that records what the answer *is*. Defect
injectors (Task A.3) mutate batches; the emitter (Task A.4) reads them.

Three rules live here, and every one of them is a cross-lane contract:

**Money is `int` paise, floored, everywhere.** Percentages go through
`core.money.pct_of`, which is `(amount * bps) // 10_000`. No float, no Decimal.

**The MDR base is the settlement's own `payment` legs only** (CSV_SCHEMAS 3.3).
Refunds, chargebacks, reserves and adjustments netted into a settlement reduce
the net; they do not shrink the fee base. `payment_gross()` is the single place
that question is answered, so it is the single place it can be got wrong.

**GST is charged on the fee, never on the gross.** `tax = pct_of(fee, 1800)`.

The truth linkage is a *property*, derived from the batch on every read, so it
cannot drift out of step with an injector that forgot to update it. Two things
it deliberately does not do:

* it never scrapes `order_id` off the PSP rows -- `true_order_ids` is
  maintained separately, so blanking a row's `order_id` (the
  `missing_order_ref` defect) damages the CSV without damaging the truth. That
  is CSV_SCHEMAS 5.1: truth records the answer, not the damage.
* it omits rows listed in `duplicate_txn_ids`. A duplicate is not a second
  economic event, so the canonical row appears in the linkage and its duplicate
  does not, whichever settlement the duplicate happens to carry.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from core.models import BankLine, Order, PSPTransaction
from core.money import Money, pct_of

from .rng import SeededRng

MDR_BPS = 236  # 2.36% merchant discount rate
GST_BPS = 1800  # 18% GST, charged on the MDR

#: Opening balance of the generated bank statement, in paise. The `balance`
#: column is a chain from here; the emitter recomputes it in statement order.
OPENING_BALANCE: Money = 10_000_000

#: First calendar day of a generated dataset.
START_DATE = date(2026, 1, 5)

_ORDER_SEQ_START = 4_471
_TXN_SEQ_START = 1_001
_LINE_SEQ_START = 1

#: Chargeback legs may reference an order that predates the register window, so
#: the reference dangles on purpose (CSV_SCHEMAS 3.2). Those ids are drawn from
#: below `_ORDER_SEQ_START` so they can never collide with a real order.
_DANGLING_SEQ_START = 1_000


# --- ids --------------------------------------------------------------------


@dataclass
class IdAllocator:
    """Hands out globally unique ids for one dataset.

    The pipeline threads a single allocator through every batch, which is what
    makes ids unique across the whole dataset. `for_settlement()` builds a
    stand-alone allocator whose ranges are derived from the settlement id, so a
    unit test that generates a handful of batches independently still gets
    distinct ids without needing to wire one up.
    """

    order_seq: int = _ORDER_SEQ_START
    txn_seq: int = _TXN_SEQ_START
    line_seq: int = _LINE_SEQ_START
    dangling_seq: int = _DANGLING_SEQ_START

    @classmethod
    def for_settlement(cls, settlement_id: str) -> IdAllocator:
        # crc32 rather than hash(): hash() of a str is salted per process.
        block = zlib.crc32(settlement_id.encode("utf-8")) % 900
        return cls(
            order_seq=_ORDER_SEQ_START + block * 1_000,
            txn_seq=_TXN_SEQ_START + block * 1_000,
            line_seq=_LINE_SEQ_START + block,
        )

    def next_order_id(self) -> str:
        self.order_seq += 1
        return f"ORD-{self.order_seq:06d}"

    def next_dangling_order_id(self) -> str:
        """An `ORD-` reference that is deliberately absent from orders.csv."""
        self.dangling_seq += 1
        if self.dangling_seq >= _ORDER_SEQ_START:
            raise RuntimeError("dangling order ids have run into the real range")
        return f"ORD-{self.dangling_seq:06d}"

    def next_txn_id(self, prefix: str) -> str:
        self.txn_seq += 1
        return f"{prefix}_{self.txn_seq:06d}"

    def next_line_id(self) -> str:
        self.line_seq += 1
        return f"BL-{self.line_seq:04d}"


# --- truth ------------------------------------------------------------------


@dataclass(frozen=True)
class Linkage:
    """One entry of `truth.json`'s `linkages` (CSV_SCHEMAS 5)."""

    bank_line_id: str
    settlement_id: str
    psp_txn_ids: list[str]
    order_ids: list[str]


# --- batch ------------------------------------------------------------------


@dataclass
class Batch:
    settlement_id: str
    settled_at: date
    orders: list[Order]
    psp_txns: list[PSPTransaction]
    bank_line: BankLine
    ids: IdAllocator
    #: Additional bank lines for a `split_settlement`. Empty for every other
    #: settlement -- one bank line is one settlement batch (CSV_SCHEMAS 4).
    extra_bank_lines: list[BankLine] = field(default_factory=list)
    #: The TRUE order set, maintained independently of what the PSP rows spell.
    true_order_ids: list[str] = field(default_factory=list)
    #: Rows that are a second copy of an economic event already counted.
    duplicate_txn_ids: list[str] = field(default_factory=list)
    #: Names of the defects injected into this batch. Membership tests only --
    #: never iterated to make a random draw.
    defect_tags: set[str] = field(default_factory=set)
    #: PSP rows an injector has already claimed, so two injectors do not fight
    #: over the same row (blanking the order_id of a duplicate's canonical twin
    #: would destroy the dedup tuple).
    touched_txn_ids: set[str] = field(default_factory=set)
    #: Deliberate divergence between the reconstruction and the bank credit.
    #: `rounding_break` sets this to -50, so `net - credit == +50`.
    credit_delta: int = 0
    #: Percentage of the net paid on the first line of a `split_settlement`.
    split_pct: int | None = None
    #: Days between `settled_at` and the day the bank actually posted the
    #: credit, when an injector has moved it. Zero for every settlement whose
    #: line posts on its own cycle, which is all of them but
    #: `obfuscated_settlement_ref`.
    #:
    #: It is recorded rather than left implicit in `bank_line.txn_date` because
    #: `_disambiguate_bank_lines` re-derives a colliding line's date from
    #: `settled_at`, and a repair that silently pulled a deliberately late
    #: posting back onto its cycle would hand the subject to a deterministic
    #: tier and delete the defect without any test going red.
    bank_delay_days: int = 0

    # -- derived views ------------------------------------------------------

    @property
    def all_bank_lines(self) -> list[BankLine]:
        return [self.bank_line, *self.extra_bank_lines]

    @property
    def payment_legs(self) -> list[PSPTransaction]:
        return [
            t
            for t in self.psp_txns
            if t.txn_type == "payment"
            and t.settlement_id == self.settlement_id
            and t.txn_id not in self.duplicate_txn_ids
        ]

    @property
    def settled_txns(self) -> list[PSPTransaction]:
        """Every row carrying this settlement id, duplicates included."""
        return [t for t in self.psp_txns if t.settlement_id == self.settlement_id]

    @property
    def linkage(self) -> Linkage:
        return Linkage(
            bank_line_id=self.bank_line.line_id,
            settlement_id=self.settlement_id,
            psp_txn_ids=[
                t.txn_id
                for t in self.settled_txns
                if t.txn_id not in self.duplicate_txn_ids
            ],
            order_ids=list(self.true_order_ids),
        )

    def order_by_id(self, order_id: str) -> Order | None:
        for o in self.orders:
            if o.order_id == order_id:
                return o
        return None


# --- arithmetic -------------------------------------------------------------


def payment_gross(batch: Batch) -> Money:
    """The MDR base: this settlement's own `payment` legs, and nothing else."""
    return sum(t.amount for t in batch.payment_legs)


def true_net(batch: Batch) -> Money:
    """Reconstructed net, counting each economic event exactly once.

    Duplicated rows are excluded -- they are the defect, not the arithmetic. A
    naive sum over the emitted CSV rows of a settlement carrying an
    in-settlement duplicate is therefore *larger* than this, by exactly the
    duplicated amount, and that gap is what the matcher has to notice.
    """
    return sum(
        t.amount
        for t in batch.settled_txns
        if t.txn_id not in batch.duplicate_txn_ids
    )


def recompute_fee_tax(batch: Batch) -> None:
    """Rewrite the fee and tax legs from the current payment legs."""
    gross = payment_gross(batch)
    fee = pct_of(gross, MDR_BPS)
    tax = pct_of(fee, GST_BPS)
    for t in batch.psp_txns:
        if t.txn_type == "fee":
            t.amount = -fee
        elif t.txn_type == "tax":
            t.amount = -tax


def apply_credit(batch: Batch) -> None:
    """Re-derive the bank credit(s) from the batch's current legs.

    Every injector that changes a leg calls this, so injection order does not
    matter: the credit is always a function of the batch's present state plus
    two explicitly named, explicitly labelled deviations --

    * `credit_delta` (the `rounding_break`), and
    * `split_pct` (the `split_settlement`, whose two credits sum to the net).

    They are mutually exclusive; a settlement carrying both would make the
    split's "credits sum to the net" assertion untrue for a reason unrelated to
    the split.
    """
    net = true_net(batch)
    if batch.split_pct is None:
        if batch.extra_bank_lines:
            raise RuntimeError(
                f"{batch.settlement_id}: extra bank lines without a split_pct"
            )
        batch.bank_line.credit = net + batch.credit_delta
        return

    if batch.credit_delta:
        raise RuntimeError(
            f"{batch.settlement_id}: a split settlement cannot also carry a "
            "rounding break -- the two credits would no longer sum to the net"
        )
    if len(batch.extra_bank_lines) != 1:
        raise RuntimeError(f"{batch.settlement_id}: a split needs exactly two lines")

    first = net * batch.split_pct // 100
    batch.bank_line.credit = first
    batch.extra_bank_lines[0].credit = net - first


# --- narration --------------------------------------------------------------


def clean_narration(settlement_id: str) -> str:
    return f"NEFT CR RAZORPAY SOFTWARE PVT LTD SETL {settlement_id}"


def _utr(rng: SeededRng, txn_date: date) -> str:
    return f"HDFCN{txn_date:%y%m%d}{rng.randint(0, 99_999):05d}"


# --- generation -------------------------------------------------------------


def generate_clean_batch(
    rng: SeededRng,
    settlement_id: str,
    order_count: int,
    *,
    ids: IdAllocator | None = None,
    settled_at: date | None = None,
) -> Batch:
    """One settlement with no defects at all.

    `order_count` orders, one `payment` leg each, then the batch's `fee` and
    `tax` legs, then the bank line at the reconstructed net. Every amount is
    integer paise; order values are whole rupees, as the tiny fixture's are, so
    the only non-round figures in the dataset come from the fee flooring.
    """
    if order_count < 1:
        raise ValueError("order_count must be at least 1")

    allocator = ids if ids is not None else IdAllocator.for_settlement(settlement_id)
    settle_date = settled_at if settled_at is not None else START_DATE

    orders: list[Order] = []
    psp: list[PSPTransaction] = []

    for _ in range(order_count):
        order_id = allocator.next_order_id()
        gross = rng.randint(500, 50_000) * 100  # whole rupees, in paise
        days_before = rng.randint(1, 4)
        captured = datetime.combine(
            settle_date - timedelta(days=days_before),
            datetime.min.time(),
        ) + timedelta(
            hours=rng.randint(6, 21),
            minutes=rng.randint(0, 59),
            seconds=rng.randint(0, 59),
        )
        orders.append(
            Order(
                order_id=order_id,
                order_date=captured.date(),
                customer_ref=f"CUST-{31_000 + rng.randint(1, 8_999)}",
                gross_amount=gross,
                currency="INR",
                status="paid",
            )
        )
        psp.append(
            PSPTransaction(
                txn_id=allocator.next_txn_id("pay"),
                txn_type="payment",
                order_id=order_id,
                captured_at=captured,
                amount=gross,
                settlement_id=settlement_id,
                settled_at=settle_date,
            )
        )

    gross_total = sum(o.gross_amount for o in orders)
    fee = pct_of(gross_total, MDR_BPS)
    tax = pct_of(fee, GST_BPS)
    stamped = datetime.combine(settle_date, datetime.min.time())

    # Settlement-level legs: order_id stays empty by design (CSV_SCHEMAS 3.2).
    psp.append(
        PSPTransaction(
            txn_id=allocator.next_txn_id("fee"),
            txn_type="fee",
            order_id=None,
            captured_at=stamped,
            amount=-fee,
            settlement_id=settlement_id,
            settled_at=settle_date,
        )
    )
    psp.append(
        PSPTransaction(
            txn_id=allocator.next_txn_id("tax"),
            txn_type="tax",
            order_id=None,
            captured_at=stamped,
            amount=-tax,
            settlement_id=settlement_id,
            settled_at=settle_date,
        )
    )

    txn_date = settle_date + timedelta(days=rng.randint(0, 1))
    bank_line = BankLine(
        line_id=allocator.next_line_id(),
        txn_date=txn_date,
        narration=clean_narration(settlement_id),
        credit=0,  # set by apply_credit below
        debit=None,
        balance=OPENING_BALANCE,  # chained by the emitter in statement order
        utr=_utr(rng, txn_date),
    )

    batch = Batch(
        settlement_id=settlement_id,
        settled_at=settle_date,
        orders=orders,
        psp_txns=psp,
        bank_line=bank_line,
        ids=allocator,
        true_order_ids=[o.order_id for o in orders],
    )
    apply_credit(batch)
    return batch
