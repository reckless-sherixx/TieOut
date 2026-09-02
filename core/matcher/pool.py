"""The candidate pool: every derived view the tiers need, computed once.

The pool is the matcher's whole world. It knows the three input tables, the
settlements those tables imply, and which of them are still unclaimed. It knows
nothing about ground truth and never will -- see tests/test_boundaries.py.

Three derived views are built up front, because each of them is a place where a
plausible-looking local rule is wrong:

* **Duplicate suppression** (CSV_SCHEMAS.md 3.2.1). Two rows are the same
  economic event when they agree on `(txn_type, order_id, captured_at, amount)`
  -- but that test is only meaningful for legs that name an order. `fee`, `tax`
  and `reserve` legs carry an empty `order_id` by design, so two settlements
  charging the same amount on the same day collide on that tuple without being
  duplicates at all. Restricting the check to order-bearing legs with a
  non-empty `order_id` is what makes it a sound rule rather than a lucky one.

  The tuple identifies the event; it does not decide the remedy. Suppression is
  a **within-settlement** repair and must never reach across a settlement
  boundary. Two rows in two different settlements are two payouts, and
  suppressing one deletes a leg from a batch a real bank line closes against --
  when it is that batch's only payment leg, the settlement disappears from
  `settlement_ids` and its bank line becomes an unexplained orphan. So the
  three shapes are handled separately (see `_detect_duplicates`): in-settlement
  copies are suppressed, an unsettled mirror is suppressed in favour of its
  settled twin, and cross-settlement twins are recorded in
  `cross_settlement_twins` and left alone.

  Putting `settlement_id` into the key wholesale would not work either: it
  makes an unsettled mirror look distinct from its settled twin -- the rows
  differ on the key by construction -- and misses `fixtures/tiny/`'s defect
  entirely. The distinction has to be drawn on the remedy, not the key.

* **Order recovery.** A settlement's true order set is every order whose
  economic event is in the batch, not the set of ids the PSP rows happen to
  spell. A `payment` leg with an empty `order_id` is the `missing_order_ref`
  defect, and recovering the order behind it is what solving that defect means.
  A matcher that scrapes `order_id` off the rows omits the recovered order and
  is marked wrong for behaving correctly.

  Recovery is by unique remainder, in three steps:

  1. **Candidates.** Orders in the register whose `gross_amount` equals the
     leg's amount and which no leg names outright.
  2. **Narrowing, only if step 1 is ambiguous.** When more than one order
     survives, the set is narrowed to orders dated on or before the leg's
     capture date and within `ORDER_RECOVERY_WINDOW_DAYS` of it. The window
     only ever narrows an already-ambiguous set; it never widens one, and it is
     never skipped because the set happens to have shrunk to one.
  3. **Accept only an uncontested singleton.** If more than one order still
     survives, recovery declines. If one survives but another leg's set is the
     same singleton, both decline -- the same rule the tiers obey, and its
     mirror image (tiers.py, rule 1).

  Both steps run over ALL legs before any recovery is accepted. Mutating the
  claimed set inside the loop would let each recovery shrink the pool for the
  next leg, and the leg read first would win: statement order as tie-breaker,
  which the brief forbids outright.

* **Dangling references are dropped, not recovered.** An order id named by a
  leg but absent from the register (the chargeback hold's dangling ref) is not
  a real order, so it is not in the settlement's order set. That is recorded in
  the audit trail, not raised as an exception: the settlement's arithmetic
  still closes and the bank line still matches.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from core.canonicalize.narration import Narration, canonicalize
from core.canonicalize.txn_types import ORDER_BEARING_TYPES, PAYMENT_TYPE
from core.matcher.batch import BatchTotals, reconstruct
from core.models import BankLine, Order, PSPTransaction

#: How far an order's own date may sit from a payment leg's capture time before
#: it stops being a plausible recovery target. Only ever used to narrow an
#: already-ambiguous set; never to widen one.
ORDER_RECOVERY_WINDOW_DAYS = 7


def _count(n: int) -> str:
    """`3 unclaimed orders` / `1 unclaimed order`, so an audit line reads."""
    return f"{n} unclaimed order{'' if n == 1 else 's'}"


@dataclass(frozen=True)
class DuplicateFinding:
    """One suppressed row and the row it duplicates."""

    duplicate_id: str
    canonical_id: str
    evidence: str


@dataclass(frozen=True)
class TwinObservation:
    """Two settlements carrying a row with the same economic tuple.

    Not a duplicate and not an exception -- neither row is wrong. Recorded so
    that leaving both alone is a stated decision in the audit trail rather than
    a silence, because the alternative (suppressing one) used to delete a whole
    settlement without saying so.
    """

    txn_ids: tuple[str, ...]
    settlement_ids: tuple[str, ...]
    evidence: str


@dataclass(frozen=True)
class OrderRecovery:
    """One attempt to put an order behind a leg that does not name one.

    Recorded whether or not it succeeded: the attempt is part of the audit
    argument, and a declined recovery is what licenses `MISSING_ORDER_REF`.
    """

    txn_id: str
    settlement_id: str | None
    order_id: str | None
    evidence: str

    @property
    def recovered(self) -> bool:
        return self.order_id is not None


class CandidatePool:
    """Mutable matching state: what exists, and what is still unclaimed."""

    def __init__(
        self,
        *,
        orders: list[Order],
        psp_txns: list[PSPTransaction],
        bank_lines: list[BankLine],
    ) -> None:
        self.orders = list(orders)
        self.psp_txns = list(psp_txns)
        self.bank_lines = list(bank_lines)

        self.orders_by_id: dict[str, Order] = {o.order_id: o for o in self.orders}
        self.txns_by_id: dict[str, PSPTransaction] = {
            t.txn_id: t for t in self.psp_txns
        }

        self.duplicates: list[DuplicateFinding] = []
        self.cross_settlement_twins: list[TwinObservation] = []
        self.suppressed_txn_ids: set[str] = set()
        self._detect_duplicates()

        self._legs: dict[str, list[PSPTransaction]] = defaultdict(list)
        for txn in self.active_txns():
            if txn.settlement_id:
                self._legs[txn.settlement_id].append(txn)

        self.order_recoveries: list[OrderRecovery] = []
        self._recovered_order_by_txn: dict[str, str] = {}
        self._recover_order_refs()

        self.dangling_order_refs: dict[str, str] = {
            t.txn_id: t.order_id
            for t in self.active_txns()
            if t.txn_type in ORDER_BEARING_TYPES
            and t.order_id
            and t.order_id not in self.orders_by_id
        }

        self._totals: dict[str, BatchTotals] = {
            sid: reconstruct(legs) for sid, legs in self._legs.items()
        }
        self._narrations: dict[str, Narration] = {
            line.line_id: canonicalize(line.narration) for line in self.bank_lines
        }

        self._claimed: set[str] = set()
        self._matched_lines: set[str] = set()
        self._ambiguity: dict[str, tuple[str, ...]] = {}
        self._contests: dict[str, tuple[str, ...]] = {}

        self._build_indexes()

    # --- indexes ---------------------------------------------------------------

    def _build_indexes(self) -> None:
        """Every lookup the tiers make, keyed once instead of scanned per line.

        The tiers used to answer "which unclaimed settlement closes this credit"
        by walking the whole pool, once per bank line, once per tier -- three
        full scans per subject, and `unclaimed_settlements()` re-sorted the pool
        on every one of them. That is O(lines x settlements) with a sort inside
        it, and at 5,000 records it was roughly 3 x 1,677 x 1,627 comparisons.

        Four indexes replace four linear scans. Each one is keyed on the thing
        the scan compared and on **nothing else**, which is the property that
        matters far more than the speed:

        * `_by_net` maps a reconstructed net to every settlement carrying it.
          The key is the net alone. It is emphatically NOT keyed by payment-leg
          count, and `_sorted_nets` is not partitioned by it either: candidacy
          is cardinality-blind (see the module docstring in tiers.py, rule 1),
          and an index that split the pool by cardinality would turn the T1/T2
          label into a tie-breaker. Two settlements that close the same credit
          at different cardinalities would each look unique inside their own
          partition, both subjects would match, and every individual rule would
          still read as correct. The value is a tuple of ids, never a single id,
          for the same reason: equal nets are an ambiguity and the tier needs
          all of them to see that.
        * `_sorted_nets` carries the same keys in order, so the +/-100 paise
          tolerance window is two `bisect` calls rather than a scan.
        * `_by_settled_date` maps the date a settlement completes to the
          settlements completing on it, so the +/- 2 day window is a walk of
          five keys.
        * `_settled_on` memoises the date itself, which was recomputed as a
          `max()` over the legs on every candidacy test.

        `_recoveries_by_settlement` and `_dangling_by_settlement` group two
        lists the match builder used to filter linearly for every match it
        built. Both preserve the order of the list they group, because that
        order is the order evidence lines appear in the audit trail.

        Nothing here reads the claimed set: an index built once per run cannot
        depend on state that changes during the run. Claimed settlements are
        filtered out at lookup time instead, which costs a set membership test
        per hit and keeps the index immutable.
        """
        self._settled_on: dict[str, date | None] = {
            sid: self._compute_settled_on(sid) for sid in self._legs
        }

        by_net: dict[int, list[str]] = defaultdict(list)
        for sid in sorted(self._legs):
            by_net[self._totals[sid].net].append(sid)
        self._by_net: dict[int, tuple[str, ...]] = {
            net: tuple(sids) for net, sids in by_net.items()
        }
        self._sorted_nets: list[int] = sorted(self._by_net)

        by_date: dict[date, list[str]] = defaultdict(list)
        for sid in sorted(self._legs):
            settled = self._settled_on[sid]
            if settled is not None:
                by_date[settled].append(sid)
        self._by_settled_date: dict[date, tuple[str, ...]] = {
            day: tuple(sids) for day, sids in by_date.items()
        }

        recoveries: dict[str, list[OrderRecovery]] = defaultdict(list)
        for recovery in self.order_recoveries:
            if recovery.settlement_id is not None:
                recoveries[recovery.settlement_id].append(recovery)
        self._recoveries_by_settlement: dict[str, tuple[OrderRecovery, ...]] = {
            sid: tuple(found) for sid, found in recoveries.items()
        }

        dangling: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for txn_id, order_id in self.dangling_order_refs.items():
            settlement_id = self.txns_by_id[txn_id].settlement_id
            if settlement_id:
                dangling[settlement_id].append((txn_id, order_id))
        self._dangling_by_settlement: dict[str, tuple[tuple[str, str], ...]] = {
            sid: tuple(found) for sid, found in dangling.items()
        }

        #: The sorted unclaimed set, recomputed only when a claim invalidates
        #: it rather than on every call. `None` means "not computed yet".
        self._unclaimed: tuple[str, ...] | None = None

    # --- construction helpers -------------------------------------------------

    def _detect_duplicates(self) -> None:
        """Suppress a repeated economic event -- but only where it is safe to.

        The economic tuple identifies the event. What to DO about a repeat
        depends on where the rows sit, and the three shapes are not the same
        problem:

        * **In one settlement.** The copy inflates that batch's reconstruction
          so nothing can close until it is discounted. Suppress it. Both rows
          are in the same batch, so the settlement survives either way.
        * **One settled, one not.** The unsettled row belongs to no batch, so
          removing it cannot take a leg away from anything. Keep the settled
          twin. This is `fixtures/tiny/`'s `pay_1105` shape.
        * **In two different settlements.** NOT a duplicate. These are two
          distinct payouts with their own `settled_at` and their own bank
          credit. Suppressing one deletes a leg from a batch a real bank line
          closes against -- and when it is that batch's only payment leg the
          settlement drops out of `settlement_ids` entirely, so the bank line
          that would have closed against it becomes an unexplained orphan. The
          remedy is worse than the disease, and it is silent. They are recorded
          in `cross_settlement_twins` and left alone.

        Within a settlement the survivor is the lowest `txn_id`. That is not an
        arbitrary tie-break for reproducibility's sake: `txn_id` is the PSP's
        own issuance sequence, and a re-post of an event is always issued after
        the event. The first row is the transaction; a later row repeating it is
        the glitch. It does matter which one survives -- the survivor is what
        lands in `MatchGroup.psp_txn_ids`, which the scorer grades -- so the
        choice needs a reason, not just a seed.
        """
        groups: dict[tuple[str, str, str, int], list[PSPTransaction]] = defaultdict(list)
        for txn in self.psp_txns:
            if txn.txn_type in ORDER_BEARING_TYPES and txn.order_id:
                key = (
                    txn.txn_type,
                    txn.order_id,
                    txn.captured_at.isoformat(),
                    txn.amount,
                )
                groups[key].append(txn)

        for rows in groups.values():
            if len(rows) < 2:
                continue

            settled: dict[str, list[PSPTransaction]] = defaultdict(list)
            unsettled: list[PSPTransaction] = []
            for txn in rows:
                if txn.settlement_id:
                    settled[txn.settlement_id].append(txn)
                else:
                    unsettled.append(txn)

            for settlement_id, batch_rows in sorted(settled.items()):
                if len(batch_rows) < 2:
                    continue
                ordered = sorted(batch_rows, key=lambda t: t.txn_id)
                self._suppress(
                    ordered[0],
                    ordered[1:],
                    f"both carry settlement_id {settlement_id!r}, so the copy "
                    f"inflates that batch",
                )

            if len(settled) > 1:
                self.cross_settlement_twins.append(
                    TwinObservation(
                        txn_ids=tuple(
                            sorted(t.txn_id for group in settled.values() for t in group)
                        ),
                        settlement_ids=tuple(sorted(settled)),
                        evidence=(
                            f"{sorted(settled)} each carry a row matching the economic "
                            f"tuple (txn_type {rows[0].txn_type!r}, order_id "
                            f"{rows[0].order_id!r}, captured_at "
                            f"{rows[0].captured_at.isoformat()}, amount "
                            f"{rows[0].amount} paise). Two settlements are two "
                            f"payouts, not one event written twice: neither row is "
                            f"suppressed, because suppressing either would delete a "
                            f"leg from a batch a bank line closes against"
                        ),
                    )
                )

            if unsettled:
                if settled:
                    survivors = sorted(t.txn_id for g in settled.values() for t in g)
                    canonical = self.txns_by_id[survivors[0]]
                    spurious = unsettled
                    why = (
                        "the copy carries no settlement_id, so it sits in no batch "
                        "and removing it changes no arithmetic"
                    )
                else:
                    ordered = sorted(unsettled, key=lambda t: t.txn_id)
                    canonical, spurious = ordered[0], ordered[1:]
                    why = "no row in the group is settled, so none is in a batch"
                if spurious:
                    self._suppress(canonical, spurious, why)

    def _suppress(
        self,
        canonical: PSPTransaction,
        spurious: list[PSPTransaction],
        why: str,
    ) -> None:
        for duplicate in spurious:
            self.suppressed_txn_ids.add(duplicate.txn_id)
            self.duplicates.append(
                DuplicateFinding(
                    duplicate_id=duplicate.txn_id,
                    canonical_id=canonical.txn_id,
                    evidence=(
                        f"{duplicate.txn_id} repeats the economic event of "
                        f"{canonical.txn_id}: same txn_type {duplicate.txn_type!r}, "
                        f"order_id {duplicate.order_id!r}, captured_at "
                        f"{duplicate.captured_at.isoformat()} and amount "
                        f"{duplicate.amount} paise -- {why}; suppressed from the batch"
                    ),
                )
            )

    def _recover_order_refs(self) -> None:
        """Put an order behind every `payment` leg that does not name one.

        Two passes, and the split is the whole point. A single pass that adds
        each recovered order to the claimed set as it goes lets every recovery
        shrink the pool for the leg after it, so the *first* leg read gets the
        defensible order and the second gets whatever is left -- including an
        order the 7-day window has already rejected, because a leftover set of
        one is never re-narrowed. Statement order becomes the tie-breaker, which
        is the one thing LANE-B-matcher.md 6 forbids.

        Pass 1 computes every leg's candidate set against the same unmutated
        pool. Pass 2 accepts a leg only when that set is a singleton *and* no
        other leg's singleton names the same order -- the contest rule the tiers
        already apply, seen from the order side.
        """
        # An order a leg names outright is spoken for and can never be the
        # answer to a leg that names nothing.
        named_orders = {
            t.order_id
            for t in self.psp_txns
            if t.order_id and t.order_id in self.orders_by_id
        }
        anonymous = [
            t
            for t in self.active_txns()
            if t.txn_type == PAYMENT_TYPE and not t.order_id
        ]

        # An order's gross is the only thing step 1 compares, so the register is
        # keyed by it once here rather than walked once per anonymous leg. That
        # walk is the quadratic term the tier indexes do not touch -- both the
        # number of anonymous legs and the size of the register grow with the
        # dataset. Register order is preserved inside each bucket because a
        # surviving singleton is reported as `found[0]`.
        orders_by_gross: dict[int, list[Order]] = defaultdict(list)
        for order in self.orders:
            orders_by_gross[order.gross_amount].append(order)

        # --- pass 1: candidate sets, all against the same pool ---------------
        candidates: dict[str, list[Order]] = {}
        population: dict[str, int] = {}
        for txn in anonymous:
            at_gross = [
                o
                for o in orders_by_gross.get(txn.amount, ())
                if o.order_id not in named_orders
            ]
            population[txn.txn_id] = len(at_gross)
            narrowed = at_gross
            if len(narrowed) > 1:
                # The window only ever narrows an already-ambiguous set. It is
                # never used to widen one, and never skipped just because the
                # set happens to have one element left.
                captured = txn.captured_at.date()
                narrowed = [
                    o
                    for o in narrowed
                    if o.order_date <= captured
                    and (captured - o.order_date).days <= ORDER_RECOVERY_WINDOW_DAYS
                ]
            candidates[txn.txn_id] = narrowed

        # --- pass 2: accept only uncontested singletons ----------------------
        wanted: dict[str, list[str]] = defaultdict(list)
        for txn_id, found in candidates.items():
            if len(found) == 1:
                wanted[found[0].order_id].append(txn_id)

        window = ORDER_RECOVERY_WINDOW_DAYS
        for txn in anonymous:
            found = candidates[txn.txn_id]
            total = population[txn.txn_id]
            narrowing = f" ({_count(len(found))} within the {window}-day window)"

            if len(found) == 1 and len(wanted[found[0].order_id]) == 1:
                recovered = found[0]
                self._recovered_order_by_txn[txn.txn_id] = recovered.order_id
                note = narrowing if total > 1 else ""
                self.order_recoveries.append(
                    OrderRecovery(
                        txn_id=txn.txn_id,
                        settlement_id=txn.settlement_id,
                        order_id=recovered.order_id,
                        evidence=(
                            f"{txn.txn_id} names no order; {recovered.order_id} is the "
                            f"only unclaimed order in the register with gross "
                            f"{txn.amount} paise{note} -- recovered"
                        ),
                    )
                )
                continue

            # Every declined branch states the population it actually saw. An
            # audit line that reports a count from after the narrowing while
            # describing the set from before it is a false statement, and the
            # declined-recovery line is the one a maintainer reads to find out
            # why an order went missing.
            seen = (
                f"{_count(total)} in the register "
                f"{'carries' if total == 1 else 'carry'} gross {txn.amount} paise"
            )
            if total == 0:
                why = f"{seen} -- nothing to recover"
            elif not found:
                why = f"{seen}, none within the {window}-day order-date window"
            elif len(found) > 1:
                why = (
                    f"{seen}{narrowing} -- more than one candidate survives, so "
                    f"recovery declines rather than guess"
                )
            else:
                rivals = sorted(set(wanted[found[0].order_id]) - {txn.txn_id})
                why = (
                    f"{seen}{narrowing}; {found[0].order_id} is its only candidate "
                    f"but {rivals} name the same only candidate -- matching either "
                    f"would make row order the tie-breaker"
                )
            self.order_recoveries.append(
                OrderRecovery(
                    txn_id=txn.txn_id,
                    settlement_id=txn.settlement_id,
                    order_id=None,
                    evidence=f"{txn.txn_id} names no order and recovery declined: {why}",
                )
            )

    # --- read-only views ------------------------------------------------------

    def active_txns(self) -> list[PSPTransaction]:
        """Every PSP row except the suppressed duplicates."""
        return [t for t in self.psp_txns if t.txn_id not in self.suppressed_txn_ids]

    @property
    def settlement_ids(self) -> frozenset[str]:
        return frozenset(self._legs)

    def legs(self, settlement_id: str) -> list[PSPTransaction]:
        return list(self._legs.get(settlement_id, ()))

    def totals(self, settlement_id: str) -> BatchTotals:
        return self._totals[settlement_id]

    def payment_legs(self, settlement_id: str) -> int:
        """T1/T2 cardinality: `payment` legs alone. This LABELS a match that is
        already unique -- it must never filter a candidate pool."""
        return sum(
            1 for t in self._legs.get(settlement_id, ()) if t.txn_type == PAYMENT_TYPE
        )

    def _compute_settled_on(self, settlement_id: str) -> date | None:
        """The date the settlement completes: the latest `settled_at` on its
        legs, falling back to the latest capture date when no leg carries one.

        Called once per settlement, at index-build time. It is a pure function
        of legs that never change after construction, and it used to be
        recomputed -- a `max()` over the whole batch -- on every candidacy test
        of every tier.
        """
        legs = self._legs.get(settlement_id, ())
        settled = [t.settled_at for t in legs if t.settled_at is not None]
        if settled:
            return max(settled)
        captured = [t.captured_at.date() for t in legs]
        return max(captured) if captured else None

    def settled_on(self, settlement_id: str) -> date | None:
        return self._settled_on.get(settlement_id)

    def within_window(self, settlement_id: str, txn_date: date, days: int) -> bool:
        settled = self._settled_on.get(settlement_id)
        if settled is None:
            return False
        return abs((settled - txn_date).days) <= days

    def order_ids(self, settlement_id: str) -> list[str]:
        """The settlement's true order set, in leg order and de-duplicated.

        Includes an order recovered from a leg that names none, and excludes a
        reference that does not resolve to a real order in the register.
        """
        found: list[str] = []
        for txn in self._legs.get(settlement_id, ()):
            if txn.txn_type not in ORDER_BEARING_TYPES:
                continue
            candidate = txn.order_id or self._recovered_order_by_txn.get(txn.txn_id)
            if candidate and candidate in self.orders_by_id and candidate not in found:
                found.append(candidate)
        return found

    def recovered_order(self, txn_id: str) -> str | None:
        return self._recovered_order_by_txn.get(txn_id)

    def narration(self, line: BankLine) -> Narration:
        return self._narrations[line.line_id]

    def referenced_settlement(self, line: BankLine) -> str | None:
        """A settlement reference carried by the bank line and present in the
        PSP report -- from the narration, or from the `utr` column.

        A reference proves *identity*. It says nothing about whether the
        arithmetic closes, which is a separate clause of T0.
        """
        from_narration = self._narrations[line.line_id].settlement_id
        if from_narration and from_narration in self._legs:
            return from_narration
        if line.utr:
            if line.utr in self._legs:
                return line.utr
            match = canonicalize(line.utr).settlement_id
            if match and match in self._legs:
                return match
        return None

    # --- mutable matching state ----------------------------------------------

    def unclaimed_settlements(self) -> list[str]:
        """Every settlement no bank line has closed yet, in id order.

        Sorted once per invalidation rather than once per call. It was called
        three times per bank line -- T1, T2 and T3 each asked -- and re-sorted
        the whole pool every time; that sort alone was a fifth of a 5,000-record
        run. A fresh list is still handed back, because callers have always been
        free to do what they like with it and a cache that leaked its own
        storage would be corrupted by the first one that did.

        **No hot path calls this any more.** The indexes answer the two
        questions it used to be asked -- which settlements are near a net, and
        which settle near a date -- without materialising the unclaimed set at
        all. It stays because it is the pool's plainest statement of what is
        still open, because it is what `tests/matcher/test_index.py` runs the
        indexes against as an oracle, and because the next caller should find a
        cheap method rather than the expensive one that was here.
        """
        if self._unclaimed is None:
            self._unclaimed = tuple(
                sorted(sid for sid in self._legs if sid not in self._claimed)
            )
        return list(self._unclaimed)

    def unclaimed_settlements_near_net(self, credit: int, tolerance: int) -> list[str]:
        """Every unclaimed settlement whose reconstructed net is within
        `tolerance` paise of `credit`, in id order.

        This is the candidate search, and it answers **exactly** what the linear
        scan it replaced answered -- `abs(net - credit) <= tolerance`, inclusive
        at both edges, over the same unclaimed set
        (`tests/matcher/test_index.py` runs the two against each other).

        Two things it deliberately does not do:

        * **It does not consider payment-leg count.** Candidacy is
          cardinality-blind; the T1/T2 label is applied afterwards, to a set of
          exactly one. An index that narrowed by cardinality here would recreate
          the forbidden tie-breaker silently.
        * **It does not stop at the first hit.** Every settlement at a matching
          net comes back, because more than one candidate means the subject
          matches nothing, and a search that returned one of two equal nets
          would turn an ambiguity into a false match.

        Money is integer paise, so an exact-net tier (tolerance 0) is a single
        dictionary lookup and no arithmetic at all. A tolerance tier bisects the
        sorted nets for the window's two edges and takes every bucket between
        them.
        """
        if tolerance == 0:
            found = self._by_net.get(credit, ())
            return [sid for sid in found if sid not in self._claimed]

        nets = self._sorted_nets
        low = bisect_left(nets, credit - tolerance)
        high = bisect_right(nets, credit + tolerance)
        found = [
            sid
            for net in nets[low:high]
            for sid in self._by_net[net]
            if sid not in self._claimed
        ]
        # The buckets arrive in net order, not id order. The scan this replaces
        # walked `unclaimed_settlements()`, which is sorted by id, and the
        # ambiguity audit line reports the candidates it was handed -- so the
        # order is part of the output, not an implementation detail.
        found.sort()
        return found

    def unclaimed_settlements_in_window(self, txn_date: date, days: int) -> list[str]:
        """Every unclaimed settlement completing within `days` of `txn_date`.

        The same predicate as `within_window`, asked of the whole pool at once:
        `abs((settled_on - txn_date).days) <= days`. Dates are whole days, so
        the window is exactly `2 * days + 1` keys and walking them is cheaper
        than testing every settlement -- which is what the exception classifier
        did, for every unmatched bank line.

        A settlement with no date at all is not in the index and is not within
        any window, exactly as `within_window` returns False for it.
        """
        found = [
            sid
            for offset in range(-days, days + 1)
            for sid in self._by_settled_date.get(txn_date + timedelta(days=offset), ())
            if sid not in self._claimed
        ]
        found.sort()
        return found

    def recoveries_for(self, settlement_id: str) -> tuple[OrderRecovery, ...]:
        """This settlement's order-recovery attempts, in `order_recoveries`
        order -- which is the order their evidence lines appear in a match."""
        return self._recoveries_by_settlement.get(settlement_id, ())

    def dangling_refs_for(self, settlement_id: str) -> tuple[tuple[str, str], ...]:
        """`(txn_id, unresolvable order_id)` for this settlement's legs, in
        `dangling_order_refs` order."""
        return self._dangling_by_settlement.get(settlement_id, ())

    def open_bank_lines(self) -> list[BankLine]:
        return [line for line in self.bank_lines if line.line_id not in self._matched_lines]

    def is_claimed(self, settlement_id: str) -> bool:
        return settlement_id in self._claimed

    def is_matched(self, line_id: str) -> bool:
        return line_id in self._matched_lines

    def claim(self, line_id: str, settlement_id: str) -> None:
        self._matched_lines.add(line_id)
        self._claimed.add(settlement_id)
        # The only thing that invalidates the cached unclaimed set. Every other
        # index is a function of the input tables and never goes stale.
        self._unclaimed = None

    def note_ambiguity(self, line_id: str, settlement_ids: list[str]) -> None:
        """Record that a subject had more than one valid candidate.

        Kept so the engine can give the subject the right reason code without
        re-deriving the candidate set, and so the widest ambiguity any tier saw
        is the one reported.
        """
        previous = self._ambiguity.get(line_id, ())
        if len(settlement_ids) > len(previous):
            self._ambiguity[line_id] = tuple(settlement_ids)

    def note_contest(self, line_id: str, rival_line_ids: list[str]) -> None:
        """Record that a subject's only candidate was also the only candidate
        of another subject. The mirror image of ambiguity: matching either one
        would make iteration order the tie-breaker."""
        self._contests[line_id] = tuple(rival_line_ids)

    def ambiguous_candidates(self, line_id: str) -> tuple[str, ...]:
        return self._ambiguity.get(line_id, ())

    def contested_with(self, line_id: str) -> tuple[str, ...]:
        return self._contests.get(line_id, ())

    def was_undecidable(self, line_id: str) -> bool:
        """True when the data offered this subject no single answer -- either
        several candidates, or one candidate several subjects wanted."""
        return bool(self._ambiguity.get(line_id) or self._contests.get(line_id))
