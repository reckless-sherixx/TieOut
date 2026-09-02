"""The ten labelled defect injectors (Task A.3, spec 10).

An injector takes the seeded rng and the whole list of batches, damages exactly
one settlement (or two, for the defects that need a pair), and returns an
`InjectionResult` naming what it did. `truth.json`'s `injected_defects` is a
straight transcription of those results, so **the label travels with the
damage** -- an injector names its own `defect_type` rather than trusting the
caller to remember which registry key it was iterating.

Four rules the whole file obeys:

**An injector never guesses.** With no eligible target it returns `None`. The
alternative -- damaging a batch that another injector has already claimed --
produces a settlement carrying two defects whose truth entries each describe
only half of it.

**Eligibility is "no tags yet".** `Batch.defect_tags` is the claim ticket.
Blanking the `order_id` of a duplicate's canonical twin would destroy the dedup
tuple (CSV_SCHEMAS 3.2.1); giving the rounding-break settlement a second bank
line would make the split's "credits sum to the net" untrue for a reason
unrelated to the split. One tag per batch keeps every defect independently
gradeable.

**`RESERVED_CLEAN` is a tag, not a defect.** The pipeline stamps it on the one
settlement that must stay pristine -- the single-payment-leg batch that is the
only place matcher tier T1 is ever exercised (`fixtures/tiny/` structurally
cannot carry it). Because it is a tag, the eligibility rule above already
protects it; no injector needs to know it exists.

**Truth records the answer, not the damage** (CSV_SCHEMAS 5.1). `missing_order_ref`
blanks a PSP row's `order_id` and leaves `Batch.true_order_ids` alone;
`cross_period_refund` *adds* the refunded order to the carrier's true set even
though the order itself lives in an earlier settlement. Emitting truth by
scraping `order_id` off the emitted rows would penalise a correct matcher.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from core.models import BankLine, PSPTransaction

from .batches import (
    OPENING_BALANCE,
    Batch,
    apply_credit,
    clean_narration,
    payment_gross,
    recompute_fee_tax,
    true_net,
)
from .rng import SeededRng

#: Stamped on the settlement the pipeline holds back as a guaranteed-clean T1
#: case. It lives in `defect_tags` so the ordinary eligibility rule excludes it.
RESERVED_CLEAN = "reserved_clean"

#: The trap's narration: no entity, no settlement reference, nothing to parse.
#: Byte-identical on both lines of the pair, exactly as `fixtures/tiny/`
#: BL-0005 and BL-0006 are.
TRAP_NARRATION = "NEFT CR   PAYOUT"

#: Bank narrations that name no settlement. Doubled and tripled spaces are part
#: of the data (CSV_SCHEMAS 1.4) and must survive to the CSV unstripped.
GARBLED_NARRATIONS = (
    "RZPX*ACME  RET PL",
    "NEFT  CR  RZPX SETTLEMEN",
    "IMPS  CR   RZP*MERCHANT PAYOU",
    "NEFT CR  RZPX*  PVT LT",
    "UPI  CR   RAZORPAY*COLLECT TRUNCAT",
)


@dataclass(frozen=True)
class InjectionResult:
    """One `injected_defects` entry of `truth.json` (CSV_SCHEMAS 5)."""

    defect_type: str
    affected_ids: list[str]
    resolvable: bool


Injector = Callable[[SeededRng, list[Batch]], InjectionResult | None]


# --- target selection -------------------------------------------------------


def _pool(batches: list[Batch]) -> list[Batch]:
    """Unclaimed batches, in dataset order so selection is reproducible."""
    return [b for b in batches if not b.defect_tags]


def _pick(
    rng: SeededRng, batches: list[Batch], *, min_payment_legs: int = 1
) -> Batch | None:
    pool = [b for b in _pool(batches) if len(b.payment_legs) >= min_payment_legs]
    if not pool:
        return None
    return rng.choice(pool)


def _positions(batches: list[Batch]) -> dict[int, int]:
    """Identity-keyed index map. `list.index` would compare batches by value."""
    return {id(b): i for i, b in enumerate(batches)}


def _stamp(rng: SeededRng, day: date, *, lo: int = 7, hi: int = 20) -> datetime:
    return datetime.combine(day, time()) + timedelta(
        hours=rng.randint(lo, hi),
        minutes=rng.randint(0, 59),
        seconds=rng.randint(0, 59),
    )


# --- 1. many_to_one_batch ---------------------------------------------------


def inject_many_to_one_batch(
    rng: SeededRng, batches: list[Batch]
) -> InjectionResult | None:
    """Label a settlement that batches several orders into one bank line.

    Nothing is damaged -- this is the dominant *shape* rather than a fault, and
    it is labelled so the scorer can report core competence separately from
    error handling. It still claims the batch, because a settlement that is
    also carrying, say, a rounding break is no longer a clean many-to-one case.
    """
    batch = _pick(rng, batches, min_payment_legs=2)
    if batch is None:
        return None
    batch.defect_tags.add("many_to_one_batch")
    return InjectionResult(
        defect_type="many_to_one_batch",
        affected_ids=[batch.bank_line.line_id, *batch.true_order_ids],
        resolvable=True,
    )


# --- 2. cross_period_refund -------------------------------------------------


def inject_cross_period_refund(
    rng: SeededRng, batches: list[Batch]
) -> InjectionResult | None:
    """A refund for an order settled in cycle N-1, netted into cycle N.

    The refund reduces the carrier's net but **not** its fee base -- MDR is not
    returned when a payment is refunded (CSV_SCHEMAS 3.3) -- so `recompute_fee_tax`
    is deliberately not called. The refunded order joins the carrier's *true*
    order set, which is how a scorer detects a matcher that understood the
    netting: `ORD-004472` is in the linkage of both `setl_A1` and `setl_B2`.
    """
    pool = _pool(batches)
    if len(pool) < 2:
        return None
    pos = _positions(batches)

    carrier = rng.choice(pool[1:])  # never the first, so an earlier batch exists
    earlier = [b for b in pool if pos[id(b)] < pos[id(carrier)]]
    source = rng.choice(earlier)

    net = true_net(carrier)
    affordable = [o for o in source.orders if o.gross_amount * 2 <= net]
    if not affordable:
        smallest = min(source.orders, key=lambda o: o.gross_amount)
        if smallest.gross_amount >= net:
            return None  # any refund would drive the credit non-positive
        affordable = [smallest]
    order = rng.choice(affordable)

    refund = PSPTransaction(
        txn_id=carrier.ids.next_txn_id("rfnd"),
        txn_type="refund",
        order_id=order.order_id,
        captured_at=_stamp(rng, carrier.settled_at - timedelta(days=1)),
        amount=-order.gross_amount,
        settlement_id=carrier.settlement_id,
        settled_at=carrier.settled_at,
    )
    carrier.psp_txns.append(refund)
    carrier.true_order_ids.append(order.order_id)
    order.status = "refunded"
    apply_credit(carrier)

    source.defect_tags.add("cross_period_refund")
    carrier.defect_tags.add("cross_period_refund")
    return InjectionResult(
        defect_type="cross_period_refund",
        affected_ids=[refund.txn_id, order.order_id],
        resolvable=True,
    )


# --- 3. fee_plus_gst --------------------------------------------------------


def inject_fee_plus_gst(
    rng: SeededRng, batches: list[Batch]
) -> InjectionResult | None:
    """Label a settlement's MDR and GST legs.

    `generate_clean_batch` already computes them correctly, so there is nothing
    to break here -- the entry exists so the scorer can report, per settlement,
    whether the matcher reproduced `pct_of(gross, 236)` and `pct_of(fee, 1800)`
    rather than the classic `pct_of(gross, 1800)`.
    """
    batch = _pick(rng, batches)
    if batch is None:
        return None
    fee = next(t for t in batch.psp_txns if t.txn_type == "fee")
    tax = next(t for t in batch.psp_txns if t.txn_type == "tax")
    batch.defect_tags.add("fee_plus_gst")
    return InjectionResult(
        defect_type="fee_plus_gst",
        affected_ids=[fee.txn_id, tax.txn_id],
        resolvable=True,
    )


# --- 4. garbled_narration ---------------------------------------------------


def inject_garbled_narration(
    rng: SeededRng, batches: list[Batch]
) -> InjectionResult | None:
    """Strip the entity and the settlement reference from a bank narration.

    Arithmetic is untouched: the credit still equals the reconstructed net, so
    the settlement remains resolvable on amount alone. Only the cheap textual
    route to the answer is removed.
    """
    batch = _pick(rng, batches)
    if batch is None:
        return None
    batch.bank_line.narration = rng.choice(GARBLED_NARRATIONS)
    batch.bank_line.utr = None
    batch.defect_tags.add("garbled_narration")
    return InjectionResult(
        defect_type="garbled_narration",
        affected_ids=[batch.bank_line.line_id],
        resolvable=True,
    )


# --- 5. duplicate_psp_txn ---------------------------------------------------

#: Share of duplicates emitted as the unsettled mirror rather than the harsher
#: in-settlement form. The mirror is the realistic PSP-report glitch and is what
#: `fixtures/tiny/` carries (`pay_1105`); the in-settlement variant is the one
#: that actually bites, so it is the majority.
_UNSETTLED_MIRROR_PCT = 30


def inject_duplicate_psp_txn(
    rng: SeededRng, batches: list[Batch]
) -> InjectionResult | None:
    """The same economic event twice, in one of two shapes.

    * **unsettled mirror** -- the copy carries an empty `settlement_id`, so it
      sits outside every settlement and the Sigma-equals-credit identity still
      holds. This is `fixtures/tiny/`'s shape, chosen there because the fixture
      integrity test asserts that identity on all six settlements.
    * **in-settlement** -- the copy carries the *same* `settlement_id`, so a
      naive sum over the settlement's rows overshoots the bank credit by exactly
      the duplicated amount. The matcher must detect and discount the duplicate
      before it can close the settlement at all.

    Both are order-bearing `payment` rows: a "duplicate" fee leg is not a
    duplicate under CSV_SCHEMAS 3.2.1 and would cost a defect class. Both agree
    with the canonical row on `(txn_type, order_id, captured_at, amount)` and
    differ only in `txn_id` and the settlement fields, which is the dedup key
    verbatim.
    """
    batch = _pick(rng, batches)
    if batch is None:
        return None
    canonical = rng.choice(batch.payment_legs)
    unsettled = rng.chance(_UNSETTLED_MIRROR_PCT)

    duplicate = PSPTransaction(
        txn_id=batch.ids.next_txn_id("pay"),
        txn_type=canonical.txn_type,
        order_id=canonical.order_id,
        captured_at=canonical.captured_at,
        amount=canonical.amount,
        settlement_id=None if unsettled else batch.settlement_id,
        settled_at=None if unsettled else batch.settled_at,
    )
    batch.psp_txns.append(duplicate)
    batch.duplicate_txn_ids.append(duplicate.txn_id)
    batch.touched_txn_ids.add(canonical.txn_id)
    batch.defect_tags.add("duplicate_psp_txn")
    apply_credit(batch)  # true_net excludes the copy; the credit does not move

    # CSV_SCHEMAS 5.1: [duplicate, canonical], and only the canonical links.
    return InjectionResult(
        defect_type="duplicate_psp_txn",
        affected_ids=[duplicate.txn_id, canonical.txn_id],
        resolvable=True,
    )


# --- 6. rounding_break ------------------------------------------------------

#: The bank credited 50 paise less than the reconstruction. Residual is always
#: `net - credit`, in that order (CSV_SCHEMAS 6), so the delta on the credit is
#: negative and the residual is +50.
_ROUNDING_BREAK_PAISE = -50


def inject_rounding_break(
    rng: SeededRng, batches: list[Batch]
) -> InjectionResult | None:
    """A half-rupee gap between the reconstruction and the credit.

    Small enough to be a tolerance question rather than a missing leg, which is
    exactly the T3-versus-exception boundary the matcher has to place.
    """
    batch = _pick(rng, batches)
    if batch is None:
        return None
    batch.credit_delta = _ROUNDING_BREAK_PAISE
    apply_credit(batch)
    batch.defect_tags.add("rounding_break")
    return InjectionResult(
        defect_type="rounding_break",
        affected_ids=[batch.bank_line.line_id],
        resolvable=True,
    )


# --- 7. chargeback_hold -----------------------------------------------------


def inject_chargeback_hold(
    rng: SeededRng, batches: list[Batch]
) -> InjectionResult | None:
    """A deduction referencing an order that predates the sales register.

    The reference dangles on purpose (CSV_SCHEMAS 3.2) -- the disputed order is
    outside the window -- so the id is drawn from the allocator's reserved
    below-the-register range and is **not** added to `true_order_ids`: it is not
    a real order, so it is not in the true set (CSV_SCHEMAS 5.1).

    The hold reduces the net and leaves the MDR base alone, so `recompute_fee_tax`
    is deliberately not called.
    """
    batch = _pick(rng, batches)
    if batch is None:
        return None
    hold = min(rng.randint(200, 900) * 100, true_net(batch) // 4)
    hold = (hold // 100) * 100  # whole rupees
    if hold < 100:
        return None  # this settlement is too small to absorb a hold

    chargeback = PSPTransaction(
        txn_id=batch.ids.next_txn_id("cb"),
        txn_type="chargeback",
        order_id=batch.ids.next_dangling_order_id(),
        captured_at=_stamp(rng, batch.settled_at - timedelta(days=rng.randint(1, 5))),
        amount=-hold,
        settlement_id=batch.settlement_id,
        settled_at=batch.settled_at,
    )
    batch.psp_txns.append(chargeback)
    apply_credit(batch)
    batch.defect_tags.add("chargeback_hold")
    return InjectionResult(
        defect_type="chargeback_hold",
        affected_ids=[chargeback.txn_id],
        resolvable=True,
    )


# --- 8. split_settlement ----------------------------------------------------


def inject_split_settlement(
    rng: SeededRng, batches: list[Batch]
) -> InjectionResult | None:
    """One settlement paid across two bank lines.

    The defect that breaks the identity everything else leans on. Every other
    settlement satisfies `Sigma legs == its bank line's credit`; this one
    satisfies `Sigma legs == credit_1 + credit_2` and matches **neither** line on
    its own. A matcher that has only seen the clean identity scores both lines
    as exceptions and says nothing about why.

    `fixtures/tiny/` cannot carry it -- six bank lines, two already spent on the
    ambiguity trap -- so generated datasets are the only place it is exercised.
    It stays `resolvable: true`: both narrations name the settlement, so the
    answer is derivable from the CSVs alone.
    """
    batch = _pick(rng, batches)
    if batch is None:
        return None

    settlement_id = batch.settlement_id
    second_date = batch.settled_at + timedelta(days=rng.randint(0, 2))
    batch.bank_line.narration = f"{clean_narration(settlement_id)} PART 1 OF 2"
    batch.extra_bank_lines.append(
        BankLine(
            line_id=batch.ids.next_line_id(),
            txn_date=second_date,
            narration=f"{clean_narration(settlement_id)} PART 2 OF 2",
            credit=0,  # set by apply_credit below
            debit=None,
            balance=OPENING_BALANCE,  # chained by the emitter in statement order
            utr=f"HDFCN{second_date:%y%m%d}{rng.randint(0, 99_999):05d}",
        )
    )
    batch.split_pct = rng.randint(35, 65)
    apply_credit(batch)
    batch.defect_tags.add("split_settlement")
    return InjectionResult(
        defect_type="split_settlement",
        affected_ids=[line.line_id for line in batch.all_bank_lines],
        resolvable=True,
    )


# --- 9. missing_order_ref ---------------------------------------------------


def inject_missing_order_ref(
    rng: SeededRng, batches: list[Batch]
) -> InjectionResult | None:
    """Blank the `order_id` of a settled `payment` row.

    An empty `order_id` is legitimate on a `fee`, `tax` or `reserve` leg, so the
    defect is only a defect on an order-bearing row (CSV_SCHEMAS 3.2).

    `true_order_ids` is untouched: recovering the order *is* what solving this
    defect means, so truth keeps the answer. Emitting truth by scraping
    `order_id` off the rows this function has just corrupted would score a
    correct matcher as wrong.
    """
    batch = _pick(rng, batches)
    if batch is None:
        return None
    leg = rng.choice(batch.payment_legs)
    leg.order_id = None
    batch.touched_txn_ids.add(leg.txn_id)
    batch.defect_tags.add("missing_order_ref")
    return InjectionResult(
        defect_type="missing_order_ref",
        affected_ids=[leg.txn_id],
        resolvable=True,
    )


# --- 10. ambiguous_unresolvable ---------------------------------------------


def inject_ambiguous_unresolvable(
    rng: SeededRng, batches: list[Batch]
) -> InjectionResult | None:
    """The trap: two settlements no evidence can tell apart.

    A trap that is merely *difficult* is worse than no trap -- it turns
    `trap_capture_rate` from a measurement into noise. So every signal goes:

    | Signal | How it is removed |
    |---|---|
    | amount | the second batch's payment legs are rewritten to the first's gross, so both reconstruct to the identical net |
    | date | one settled date, one bank `txn_date`, for both |
    | narration entity | both lines carry `TRAP_NARRATION` -- byte-identical, naming nobody |
    | settlement reference | absent from that narration |
    | UTR | `None` on both lines |
    | statement order | the two bank lines are **swapped** between the batches, so "first line, first settlement" is wrong rather than merely unjustified |

    The fee and tax legs of the two settlements end up byte-identical on the
    dedup tuple. That is correct and deliberate: settlement-level legs are
    exempt from duplicate detection (CSV_SCHEMAS 3.2.1), and giving them
    distinguishable timestamps would reintroduce a PSP-side ordering the trap
    depends on being absent.

    Recorded `resolvable: false`; both line ids land in `unresolvable_ids`. A
    matcher that resolves these has guessed, and the metric says so.
    """
    pool = _pool(batches)
    if len(pool) < 2:
        return None
    pos = _positions(batches)
    first, second = sorted(rng.sample(pool, 2), key=lambda b: pos[id(b)])

    # -- amount: rewrite `second`'s payment legs to `first`'s gross ----------
    target = payment_gross(first)
    legs = second.payment_legs
    per = ((target // len(legs)) // 100) * 100  # whole rupees
    if per < 100:
        return None
    amounts = [per] * (len(legs) - 1) + [target - per * (len(legs) - 1)]
    for leg, amount in zip(legs, amounts):
        leg.amount = amount
        order = second.order_by_id(leg.order_id)
        if order is not None:
            order.gross_amount = amount
    recompute_fee_tax(second)

    # -- date: move `second` onto `first`'s cycle ---------------------------
    shift = (first.settled_at - second.settled_at).days
    if shift:
        for order in second.orders:
            order.order_date = order.order_date + timedelta(days=shift)
    for txn in second.psp_txns:
        if txn.txn_type in ("fee", "tax"):
            txn.captured_at = datetime.combine(first.settled_at, time())
        elif shift:
            txn.captured_at = txn.captured_at + timedelta(days=shift)
        if txn.settled_at is not None:
            txn.settled_at = first.settled_at
    second.settled_at = first.settled_at

    # -- narration, UTR, bank date ------------------------------------------
    for line in (first.bank_line, second.bank_line):
        line.txn_date = first.settled_at
        line.narration = TRAP_NARRATION
        line.utr = None

    # -- statement order: cross the pairing ---------------------------------
    first.bank_line, second.bank_line = second.bank_line, first.bank_line

    apply_credit(first)
    apply_credit(second)
    first.defect_tags.add("ambiguous_unresolvable")
    second.defect_tags.add("ambiguous_unresolvable")
    return InjectionResult(
        defect_type="ambiguous_unresolvable",
        affected_ids=sorted(
            [first.bank_line.line_id, second.bank_line.line_id]
        ),
        resolvable=False,
    )


# --- 11. obfuscated_settlement_ref ------------------------------------------

#: Bank narrations that DO name the settlement -- in a form no regex in this
#: repository recovers. Rendered by `render_obfuscated` and drawn per instance,
#: so one dataset carries several shapes rather than one shape several times.
#:
#: The pool exists to make a specific claim testable: that recovering the
#: reference is a READING task rather than a pattern-matching one. Four
#: independent things vary, and they vary together --
#:
#: | | keyword | separator | padding | distractor numbers |
#: |---|---|---|---|---|
#: | 1 | `SETL` | space | as issued | none |
#: | 2 | `SETL` | hyphen | as issued | the year |
#: | 3 | `SETTLEMENT NO` | space | **stripped** | none |
#: | 4 | none | **fused to the token** | stripped | three |
#: | 5 | `SETL NO` | **split across a space** | as issued | the value date |
#: | 6 | none (`REF`) | space | **over-padded** | the year |
#:
#: So the numeral a reader has to lift out is not in a fixed relationship to
#: the canonical id in any two rows: it is the id, the id without its zeros,
#: the id with one more zero, or two fragments of the id either side of a
#: space -- and in row 4 it sits among three other numbers with better claim to
#: being "the number in this narration".
#:
#: **What this is NOT.** It is not proof that no deterministic pass could
#: recover these. A generate-and-test recovery -- lift every numeral, try it
#: zero-padded to each plausible width, keep the one that names a settlement
#: the PSP report actually contains -- would recover most of them, and row 4 is
#: the only one that would give it real trouble. What the pool does establish
#: is that no single expression over the narration ALONE yields the canonical
#: id: the four variables above are independent, so a pattern is needed per
#: row, plus a normalisation step, plus a membership test against data the
#: canonicaliser does not have. `SETTLEMENT_RE` recovers zero of them, and
#: `tests/generator/test_defects.py` asserts exactly that and nothing stronger.
OBFUSCATION_FORMATS: tuple[str, ...] = (
    "NEFT CR RZPX*ACME RET PL REF SETL {padded} BATCH {mon}",
    "NEFT/RZP/SETL-{padded}/{mon}{yy}",
    "RAZORPAY SETTLEMENT NO {bare} CR",
    "IMPS CR RZP BATCH {batch} OF {total} STLMNT{bare} CYCLE {mon}{yy}",
    "NEFT CR RZPX*ACME RET PL SETL NO {head} {tail} VAL {dd}{mon}{yy}",
    "UPI CR RAZORPAY*COLLECT REF {wide} SETTLED {mon}{yy}",
)

#: How many days after `settled_at` the bank posts the credit.
#:
#: Strictly greater than the matcher's two-day candidate window, which is the
#: whole mechanism: an obfuscated narration ALONE changes nothing, because the
#: credit still equals the reconstruction and T1/T2/T3 would match it on the
#: amount without ever reading the prose. The late posting is what holds the
#: subject back, and it is the one thing the deterministic tiers check that the
#: verifier does not -- `_causality` asks only that the money did not arrive
#: before it settled, which a LATE posting satisfies by construction.
#:
#: Named here rather than imported from `core.matcher.tiers`: the generator does
#: not depend on the matcher, and a test that imports both is where the
#: relationship between the two numbers belongs.
_POSTING_DELAY_DAYS = (4, 9)

_MONTHS = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)


def render_obfuscated(
    template: str, settlement_id: str, *, settled_at: date, salt: int
) -> str:
    """One narration that names `settlement_id` without spelling it.

    `salt` supplies the distractor numerals deterministically, so the caller
    keeps its RNG and this function stays a pure formatter a test can call.
    """
    suffix = settlement_id.split("_", 1)[-1]
    bare = suffix.lstrip("0") or "0"
    cut = max(1, len(suffix) // 2)
    batch = salt % 20 + 1
    return template.format(
        padded=suffix,
        bare=bare,
        wide=f"0{suffix}",
        head=suffix[:cut],
        tail=suffix[cut:],
        mon=_MONTHS[settled_at.month - 1],
        yy=f"{settled_at.year % 100:02d}",
        dd=f"{settled_at.day:02d}",
        batch=batch,
        total=batch + 7 + salt % 5,
    )


def inject_obfuscated_settlement_ref(
    rng: SeededRng, batches: list[Batch]
) -> InjectionResult | None:
    """The settlement reference is in the narration, but not in canonical form.

    This is the one defect written FOR the analyst layer, and the division of
    labour it encodes is the point of it:

    * the **model** recovers only the reference -- a reading task;
    * the **verifier** does every rupee: existence, exclusivity, causality,
      arithmetic, coherence, uniqueness, plus the pipeline's subject tie.

    So the arithmetic is left untouched. The credit still equals the
    reconstruction exactly, which is what lets a correct recovery be ACCEPTED
    rather than rejected for a reason that has nothing to do with reading. A
    recovery naming the wrong settlement fails `arithmetic` like any other bad
    hypothesis; the model cannot buy an acceptance with confidence.

    Two changes, and both are needed:

    1. **The narration is rewritten** into one of `OBFUSCATION_FORMATS`, so T0
       finds no reference. On its own this achieves nothing -- `garbled_narration`
       already removes the reference and its subject is still matched on amount.
    2. **The bank posts late**, `_POSTING_DELAY_DAYS` after the settlement, so
       no reconstruction tier will even look at the settlement. That is the only
       asymmetry between the tiers and the verifier, and it is the same one
       `tests/llm/test_pipeline.py`'s fixtures have always used.

    Truth records it `resolvable: true` with the real linkage: the answer IS in
    the data, so a correct recovery must raise `assisted_match_rate`, an
    incorrect one that survives verification must be gradeable as a false match,
    and a rejected one must stay an exception.
    """
    batch = _pick(rng, batches)
    if batch is None:
        return None

    template = rng.choice(OBFUSCATION_FORMATS)
    salt = rng.randint(0, 99)
    batch.bank_delay_days = rng.randint(*_POSTING_DELAY_DAYS)

    line = batch.bank_line
    line.txn_date = batch.settled_at + timedelta(days=batch.bank_delay_days)
    line.narration = render_obfuscated(
        template, batch.settlement_id, settled_at=batch.settled_at, salt=salt
    )
    # The UTR is KEPT. Stripping it would be `garbled_narration`'s damage, and
    # a real NEFT credit carries one; it is re-stamped for the posting date so
    # the row stays internally consistent. `HDFCN...` canonicalises to no
    # settlement id, so `referenced_settlement` still finds nothing in the
    # `utr` column -- which is the other place T0 looks.
    line.utr = f"HDFCN{line.txn_date:%y%m%d}{rng.randint(0, 99_999):05d}"

    batch.defect_tags.add("obfuscated_settlement_ref")
    return InjectionResult(
        defect_type="obfuscated_settlement_ref",
        affected_ids=[line.line_id],
        resolvable=True,
    )


# --- registry ---------------------------------------------------------------

#: Injection order, and it is load-bearing. Every injector consumes an unclaimed
#: batch, so whichever runs last is the one that starves. The defects with **no
#: coverage anywhere else in the project** go first -- `fixtures/tiny/` is frozen
#: and structurally cannot carry any of the three -- followed by the ones that
#: need a pair of batches, then the single-batch ones, then the two that only
#: attach a label.
#:
#: `obfuscated_settlement_ref` is third for that reason and one more: starving it
#: does not merely drop a row from a table, it takes `assisted_match_rate`'s
#: whole denominator to zero and makes the analyst layer unmeasurable again --
#: which is the exact failure this defect was written to end.
INJECTION_ORDER: tuple[str, ...] = (
    "ambiguous_unresolvable",
    "split_settlement",
    "obfuscated_settlement_ref",
    "cross_period_refund",
    "duplicate_psp_txn",
    "missing_order_ref",
    "chargeback_hold",
    "rounding_break",
    "garbled_narration",
    "many_to_one_batch",
    "fee_plus_gst",
)

DEFECT_REGISTRY: dict[str, Injector] = {
    "many_to_one_batch": inject_many_to_one_batch,
    "cross_period_refund": inject_cross_period_refund,
    "fee_plus_gst": inject_fee_plus_gst,
    "garbled_narration": inject_garbled_narration,
    "duplicate_psp_txn": inject_duplicate_psp_txn,
    "rounding_break": inject_rounding_break,
    "chargeback_hold": inject_chargeback_hold,
    "split_settlement": inject_split_settlement,
    "missing_order_ref": inject_missing_order_ref,
    "ambiguous_unresolvable": inject_ambiguous_unresolvable,
    "obfuscated_settlement_ref": inject_obfuscated_settlement_ref,
}

#: Instances of each defect per **100 rows of `orders.csv`** -- the record spine
#: the scale runs (50 / 500 / 5,000) count. This is the only copy of these
#: numbers in the repository. `api/openapi.yaml` makes `defect_mix` optional and
#: Lane D passes `None` straight through precisely so a second copy cannot drift
#: away from this one.
#:
#: `fixtures/tiny/` pins nine of the ten: 12 orders, 6 settlements, one instance
#: of each of nine defect types. `split_settlement` is the tenth and the fixture
#: has no room for it -- six bank lines, two already spent on the ambiguity trap,
#: and a split needs two of its own -- so it is given the same
#: one-per-settlement-cycle rate as the other single-instance defects.
#:
#: | Defect | Per 100 records | Batches consumed | Why this rate |
#: |---|---:|---:|---|
#: | `many_to_one_batch` | 2 | 2 | the dominant shape; labelled often |
#: | `cross_period_refund` | 1 | 2 | needs a source cycle and a carrier cycle |
#: | `fee_plus_gst` | 1 | 1 | one settlement's MDR/GST pair per cycle |
#: | `garbled_narration` | 2 | 2 | the commonest real-world bank defect |
#: | `duplicate_psp_txn` | 2 | 2 | split across both variants, see above |
#: | `rounding_break` | 1 | 1 | single-instance, as in the fixture |
#: | `chargeback_hold` | 1 | 1 | single-instance, as in the fixture |
#: | `split_settlement` | 1 | 1 | the tenth class; fixture cannot carry it |
#: | `missing_order_ref` | 1 | 1 | single-instance, as in the fixture |
#: | `ambiguous_unresolvable` | 1 | 2 | the trap; one pair per cycle |
#: | `obfuscated_settlement_ref` | 2 | 2 | the analyst layer's only fuel, see below |
#: | **total** | **15** | **17** | ~65% of a 100-record run's settlements |
#:
#: `obfuscated_settlement_ref` is set at 2 rather than 1 deliberately. It is the
#: only defect class whose subjects the deterministic engine cannot resolve *and*
#: the verifier can, so it is the entire denominator of `assisted_match_rate`; at
#: 1 per 100 a 500-record run measures the analyst on five subjects, which is not
#: a rate, it is an anecdote. At 2 it matches the population of the split halves,
#: the trap pairs and the duplicate legs, and every one of those numbers is
#: reported per class in `METRICS.md`.
#:
#: The batch column is the constraint that matters: mean settlement size is
#: about 3.1 orders, so 100 records is roughly 26 settlements and 15 of them are
#: claimed. The rest stay clean -- including the reserved single-payment-leg T1
#: case, which no injector can touch.
DEFAULT_DEFECT_MIX: dict[str, int] = {
    "many_to_one_batch": 2,
    "cross_period_refund": 1,
    "fee_plus_gst": 1,
    "garbled_narration": 2,
    "duplicate_psp_txn": 2,
    "rounding_break": 1,
    "chargeback_hold": 1,
    "split_settlement": 1,
    "missing_order_ref": 1,
    "ambiguous_unresolvable": 1,
    "obfuscated_settlement_ref": 2,
}


def resolve_defect_mix(
    record_count: int, override: Mapping[str, int] | None = None
) -> dict[str, int]:
    """Scale `DEFAULT_DEFECT_MIX` to `record_count`, then apply `override`.

    `override` is the CLI's `--defect-mix` and the API's optional `defect_mix`;
    **omitted or `None` means use the default**, which is what makes
    `--seed 42 --count 500` the same adversarial dataset everywhere.

    Every defect gets at least one instance regardless of how small the run is.
    A dataset silently missing a defect class removes a row from `METRICS.md`
    and there is no honest way to report that.
    """
    if record_count < 1:
        raise ValueError("record_count must be at least 1")

    mix = {
        name: max(1, per_100 * record_count // 100)
        for name, per_100 in DEFAULT_DEFECT_MIX.items()
    }
    if override:
        unknown = sorted(set(override) - set(DEFECT_REGISTRY))
        if unknown:
            raise ValueError(
                f"unknown defect type(s): {', '.join(unknown)}. "
                f"Known types: {', '.join(sorted(DEFECT_REGISTRY))}"
            )
        for name, count in override.items():
            if int(count) < 0:
                raise ValueError(f"{name}: a defect count cannot be negative")
            mix[name] = int(count)
    return {name: mix[name] for name in INJECTION_ORDER}


def run_injections(
    rng: SeededRng, batches: list[Batch], mix: Mapping[str, int]
) -> list[InjectionResult]:
    """Apply `mix` to `batches`, one instance of every class before any seconds.

    The first pass walks `INJECTION_ORDER` taking a single instance of each, so
    a run whose batch pool is only just big enough still produces **all ten**
    classes rather than a surplus of whichever injector happened to be first.
    Later passes round-robin the remainder and stop as soon as a full round
    damages nothing, which is how a starved pool ends: quietly short, never
    by double-claiming a batch.
    """
    remaining = {name: int(mix.get(name, 0)) for name in INJECTION_ORDER}
    results: list[InjectionResult] = []

    while any(count > 0 for count in remaining.values()):
        progressed = False
        for name in INJECTION_ORDER:
            if remaining[name] <= 0:
                continue
            remaining[name] -= 1
            result = DEFECT_REGISTRY[name](rng, batches)
            if result is not None:
                results.append(result)
                progressed = True
        if not progressed:
            break
    return results
