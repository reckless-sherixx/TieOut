"""Task A.3 -- one test per injector, plus the cross-lane invariants.

Note on `test_cross_period_refund_lands_in_a_later_batch`: the plan's draft of
this test asserted `refund.order_id not in carrier.linkage.order_ids`. That
contradicts the frozen truth contract -- CSV_SCHEMAS 5.1 states that
`ORD-004472` appears in the linkage of *both* `setl_A1` (its payment) and
`setl_B2` (its refund), and `fixtures/tiny/truth.json` shows exactly that. The
comment above the assertion ("the refunded order belongs to an EARLIER
settlement than the one carrying it") describes the real intent, which is about
the carrier's *own* orders, so that is what is asserted here -- alongside the
contract the draft would have broken. Emitting truth the draft's way would score
a correct matcher as wrong.
"""

from datetime import date

import pytest

from core.generator.batches import (
    generate_clean_batch,
    payment_gross,
    true_net,
)
from core.generator.defects import (
    DEFAULT_DEFECT_MIX,
    DEFECT_REGISTRY,
    INJECTION_ORDER,
    RESERVED_CLEAN,
    InjectionResult,
    resolve_defect_mix,
)
from core.generator.rng import SeededRng

SPEC_DEFECTS = {
    "many_to_one_batch",
    "cross_period_refund",
    "fee_plus_gst",
    "garbled_narration",
    "duplicate_psp_txn",
    "rounding_break",
    "chargeback_hold",
    "split_settlement",
    "missing_order_ref",
    "ambiguous_unresolvable",
    "obfuscated_settlement_ref",
}


def _fresh(n: int, orders: int = 5) -> list:
    return [
        generate_clean_batch(
            SeededRng(42),
            f"setl_{i:04X}",
            orders,
            settled_at=date(2026, 2, 1 + i),
        )
        for i in range(n)
    ]


# --- registry ---------------------------------------------------------------


def test_registry_holds_exactly_the_eleven_spec_defects():
    assert set(DEFECT_REGISTRY) == SPEC_DEFECTS
    assert set(INJECTION_ORDER) == SPEC_DEFECTS
    assert len(INJECTION_ORDER) == 11


def test_every_result_labels_itself_with_its_own_defect_type():
    """`truth.json`'s `defect_type` comes from here, not from the caller.

    The emitter writes one `injected_defects` entry per `InjectionResult`. If
    the label travelled separately -- as the registry key the runner happened to
    be iterating -- a mis-wired runner would emit ten correctly-shaped entries
    carrying the wrong names, and every downstream metric would still compute.
    """
    for name, injector in DEFECT_REGISTRY.items():
        result = injector(SeededRng(7), _fresh(4))
        assert isinstance(result, InjectionResult), f"{name} produced nothing"
        assert result.defect_type == name
        assert result.affected_ids, f"{name} labelled nothing"
        assert all(isinstance(i, str) for i in result.affected_ids)
        assert isinstance(result.resolvable, bool)


def test_every_injector_is_reproducible_from_its_seed():
    """Same seed, same batches, same damage -- byte-identity starts here."""
    for name, injector in DEFECT_REGISTRY.items():
        first_batches, second_batches = _fresh(4), _fresh(4)
        first = injector(SeededRng(11), first_batches)
        second = injector(SeededRng(11), second_batches)
        assert first == second, f"{name} is not reproducible"
        for a, b in zip(first_batches, second_batches):
            assert [o.model_dump() for o in a.orders] == [
                o.model_dump() for o in b.orders
            ], name
            assert [t.model_dump() for t in a.psp_txns] == [
                t.model_dump() for t in b.psp_txns
            ], name
            assert [ln.model_dump() for ln in a.all_bank_lines] == [
                ln.model_dump() for ln in b.all_bank_lines
            ], name


def test_default_mix_names_every_defect_and_scales_with_the_dataset():
    assert set(DEFAULT_DEFECT_MIX) == SPEC_DEFECTS
    small = resolve_defect_mix(6)
    large = resolve_defect_mix(120)
    assert set(small) == SPEC_DEFECTS
    assert all(v >= 1 for v in small.values()), "every defect must always appear"
    assert sum(large.values()) > sum(small.values())


def test_an_override_replaces_only_the_named_entries():
    mix = resolve_defect_mix(60, {"rounding_break": 3})
    assert mix["rounding_break"] == 3
    assert mix["garbled_narration"] == resolve_defect_mix(60)["garbled_narration"]


def test_an_unknown_override_key_is_a_hard_error():
    with pytest.raises(ValueError, match="unknown defect"):
        resolve_defect_mix(60, {"not_a_defect": 1})


# --- the two the plan calls out ---------------------------------------------


def test_cross_period_refund_lands_in_a_later_batch():
    batches = _fresh(3)
    result = DEFECT_REGISTRY["cross_period_refund"](SeededRng(7), batches)
    refunds = [t for b in batches for t in b.psp_txns if t.txn_type == "refund"]
    assert len(refunds) == 1
    refund = refunds[0]
    carrier = next(b for b in batches if refund in b.psp_txns)

    # the refunded order belongs to an EARLIER settlement than the one carrying it
    assert refund.order_id not in {o.order_id for o in carrier.orders}
    source = next(b for b in batches if b.order_by_id(refund.order_id))
    assert batches.index(source) < batches.index(carrier)
    assert source.order_by_id(refund.order_id).status == "refunded"

    # ...and truth still records it in the carrier's TRUE order set
    # (CSV_SCHEMAS 5.1: ORD-004472 is in both setl_A1's and setl_B2's linkage).
    assert refund.order_id in carrier.linkage.order_ids
    assert refund.order_id in source.linkage.order_ids

    assert refund.amount < 0
    assert refund.settlement_id == carrier.settlement_id
    assert carrier.bank_line.credit == true_net(carrier)
    assert result.resolvable is True
    assert result.affected_ids == [refund.txn_id, refund.order_id]


def test_ambiguous_pair_is_marked_unresolvable():
    batches = _fresh(4)
    result = DEFECT_REGISTRY["ambiguous_unresolvable"](SeededRng(7), batches)
    ids = result.affected_ids
    lines = [b.bank_line for b in batches if b.bank_line.line_id in ids]
    assert len(lines) == 2
    assert lines[0].credit == lines[1].credit
    assert lines[0].txn_date == lines[1].txn_date
    assert lines[0].utr is None and lines[1].utr is None
    assert result.resolvable is False


def test_the_trap_strips_every_distinguishing_signal():
    """A trap that is merely difficult turns trap_capture_rate into noise."""
    batches = _fresh(4, orders=3)
    # give the batches genuinely different totals first
    for i, b in enumerate(batches):
        for o, t in zip(b.orders, b.payment_legs):
            o.gross_amount = (10_000 + i * 700) * 100
            t.amount = o.gross_amount
        from core.generator.batches import apply_credit, recompute_fee_tax

        recompute_fee_tax(b)
        apply_credit(b)

    result = DEFECT_REGISTRY["ambiguous_unresolvable"](SeededRng(7), batches)
    trapped = [b for b in batches if b.bank_line.line_id in result.affected_ids]
    a, b2 = trapped
    assert payment_gross(a) == payment_gross(b2)
    assert true_net(a) == true_net(b2)
    assert a.bank_line.credit == b2.bank_line.credit
    assert a.bank_line.txn_date == b2.bank_line.txn_date
    assert a.settled_at == b2.settled_at
    assert a.bank_line.narration == b2.bank_line.narration
    assert a.settlement_id not in a.bank_line.narration
    assert b2.settlement_id not in b2.bank_line.narration
    assert a.bank_line.utr is None and b2.bank_line.utr is None


# --- the other eight --------------------------------------------------------


def test_many_to_one_batch_labels_a_multi_payment_settlement():
    batches = _fresh(3)
    r = DEFECT_REGISTRY["many_to_one_batch"](SeededRng(7), batches)
    target = next(b for b in batches if b.bank_line.line_id in r.affected_ids)
    assert len(target.payment_legs) >= 2
    assert r.affected_ids == [target.bank_line.line_id, *target.true_order_ids]
    assert r.resolvable is True


def test_fee_plus_gst_labels_the_fee_and_tax_legs():
    batches = _fresh(3)
    r = DEFECT_REGISTRY["fee_plus_gst"](SeededRng(7), batches)
    legs = {t.txn_id: t for b in batches for t in b.psp_txns}
    assert len(r.affected_ids) == 2
    assert {legs[i].txn_type for i in r.affected_ids} == {"fee", "tax"}
    assert r.resolvable is True


def test_garbled_narration_removes_entity_and_reference():
    batches = _fresh(3)
    r = DEFECT_REGISTRY["garbled_narration"](SeededRng(7), batches)
    target = next(b for b in batches if b.bank_line.line_id in r.affected_ids)
    assert target.settlement_id not in target.bank_line.narration
    assert "  " in target.bank_line.narration, "doubled spaces are part of the data"
    assert target.bank_line.utr is None
    assert target.bank_line.credit == true_net(target), "narration only, no arithmetic"
    assert r.resolvable is True


def test_duplicate_is_order_bearing_and_shares_the_dedup_tuple():
    batches = _fresh(3)
    r = DEFECT_REGISTRY["duplicate_psp_txn"](SeededRng(7), batches)
    dup_id, canonical_id = r.affected_ids
    rows = {t.txn_id: t for b in batches for t in b.psp_txns}
    dup, canonical = rows[dup_id], rows[canonical_id]

    # CSV_SCHEMAS 3.2.1 -- the dedup key, and it is only meaningful with an order
    assert (dup.txn_type, dup.order_id, dup.captured_at, dup.amount) == (
        canonical.txn_type,
        canonical.order_id,
        canonical.captured_at,
        canonical.amount,
    )
    assert dup.order_id is not None
    assert dup.txn_type == "payment"
    assert dup.txn_id != canonical.txn_id

    target = next(b for b in batches if dup in b.psp_txns)
    assert dup_id in target.duplicate_txn_ids
    assert dup_id not in target.linkage.psp_txn_ids, "only the canonical row links"
    assert canonical_id in target.linkage.psp_txn_ids
    assert r.resolvable is True


def test_both_duplicate_variants_are_emitted():
    """The unsettled mirror AND the harsher in-settlement variant."""
    settled, unsettled = 0, 0
    for seed in range(40):
        batches = _fresh(2)
        r = DEFECT_REGISTRY["duplicate_psp_txn"](SeededRng(seed), batches)
        rows = {t.txn_id: t for b in batches for t in b.psp_txns}
        dup = rows[r.affected_ids[0]]
        if dup.settlement_id is None:
            unsettled += 1
            assert dup.settled_at is None
        else:
            settled += 1
            target = next(b for b in batches if dup in b.psp_txns)
            # the harsh variant: the naive CSV sum no longer equals the credit
            naive = sum(t.amount for t in target.settled_txns)
            assert naive == target.bank_line.credit + dup.amount
    assert settled > 0 and unsettled > 0


def test_rounding_break_leaves_the_credit_fifty_paise_short():
    batches = _fresh(3)
    r = DEFECT_REGISTRY["rounding_break"](SeededRng(7), batches)
    target = next(b for b in batches if b.bank_line.line_id in r.affected_ids)
    # residual is always net - credit, in that order (CSV_SCHEMAS 6)
    assert true_net(target) - target.bank_line.credit == 50
    assert abs(true_net(target) - target.bank_line.credit) <= 100, "must reach T3"
    assert r.resolvable is True


def test_chargeback_hold_references_an_order_absent_from_the_register():
    batches = _fresh(3)
    before = {b.settlement_id: true_net(b) for b in batches}
    r = DEFECT_REGISTRY["chargeback_hold"](SeededRng(7), batches)
    rows = {t.txn_id: t for b in batches for t in b.psp_txns}
    cb = rows[r.affected_ids[0]]
    assert cb.txn_type == "chargeback"
    assert cb.amount < 0

    target = next(b for b in batches if cb in b.psp_txns)
    every_order = {o.order_id for b in batches for o in b.orders}
    assert cb.order_id not in every_order, "the reference dangles on purpose"
    assert cb.order_id not in target.linkage.order_ids, "not a real order, not in truth"
    assert true_net(target) == before[target.settlement_id] + cb.amount
    assert target.bank_line.credit == true_net(target)
    assert payment_gross(target) == sum(
        o.gross_amount for o in target.orders
    ), "a hold must not shrink the MDR base"
    assert r.resolvable is True


def test_missing_order_ref_blanks_the_row_but_not_the_truth():
    batches = _fresh(3)
    r = DEFECT_REGISTRY["missing_order_ref"](SeededRng(7), batches)
    rows = {t.txn_id: t for b in batches for t in b.psp_txns}
    row = rows[r.affected_ids[0]]
    assert row.txn_type == "payment"
    assert row.order_id is None

    target = next(b for b in batches if row in b.psp_txns)
    recovered = next(
        o for o in target.orders if o.gross_amount == row.amount
    )
    assert recovered.order_id in target.linkage.order_ids, (
        "truth records the recovered order, not the damage (CSV_SCHEMAS 5.1)"
    )
    assert len(target.linkage.order_ids) == len(target.orders)
    assert r.resolvable is True


def test_split_settlement_credits_sum_to_the_net():
    batches = _fresh(3)
    r = DEFECT_REGISTRY["split_settlement"](SeededRng(7), batches)
    target = next(b for b in batches if b.split_pct is not None)
    lines = target.all_bank_lines
    assert len(lines) == 2
    assert sorted(r.affected_ids) == sorted(line.line_id for line in lines)
    assert sum(line.credit for line in lines) == true_net(target)
    assert lines[0].credit > 0 and lines[1].credit > 0
    assert lines[0].credit != true_net(target), "neither line matches on its own"
    assert abs((lines[1].txn_date - target.settled_at).days) <= 2
    assert r.resolvable is True


# --- eligibility ------------------------------------------------------------


def test_no_injector_will_touch_a_reserved_clean_batch():
    """The reserved batch is the only place tier T1 is ever exercised."""
    for name, injector in DEFECT_REGISTRY.items():
        batches = _fresh(4)
        reserved = batches[1]
        reserved.defect_tags.add(RESERVED_CLEAN)
        snapshot = (
            [o.model_dump() for o in reserved.orders],
            [t.model_dump() for t in reserved.psp_txns],
            reserved.bank_line.model_dump(),
            list(reserved.true_order_ids),
        )
        result = injector(SeededRng(7), batches)
        assert (
            [o.model_dump() for o in reserved.orders],
            [t.model_dump() for t in reserved.psp_txns],
            reserved.bank_line.model_dump(),
            list(reserved.true_order_ids),
        ) == snapshot, f"{name} touched the reserved clean batch"
        if result is not None:
            assert reserved.bank_line.line_id not in result.affected_ids
            assert all(
                o.order_id not in result.affected_ids for o in reserved.orders
            )


def test_an_injector_with_no_eligible_target_returns_none_rather_than_guessing():
    batches = _fresh(2)
    for b in batches:
        b.defect_tags.add(RESERVED_CLEAN)
    for injector in DEFECT_REGISTRY.values():
        assert injector(SeededRng(7), batches) is None


# --- obfuscated_settlement_ref: the capability the analyst layer exists for ----


def _obfuscated(seed: int = 7, batches: list | None = None):
    """Inject the defect and hand back `(result, damaged batch, all batches)`."""
    batches = _fresh(4) if batches is None else batches
    result = DEFECT_REGISTRY["obfuscated_settlement_ref"](SeededRng(seed), batches)
    assert result is not None
    target = next(b for b in batches if b.bank_line.line_id in result.affected_ids)
    return result, target, batches


def test_the_format_pool_holds_at_least_four_distinct_shapes():
    """Spec §5: four formats, not one format with four spellings.

    Distinctness is asserted on the *template*, not on a rendered sample --
    two templates that happen to render identically for one id would pass a
    value comparison and still be one format."""
    from core.generator.defects import OBFUSCATION_FORMATS

    assert len(OBFUSCATION_FORMATS) >= 4
    assert len(set(OBFUSCATION_FORMATS)) == len(OBFUSCATION_FORMATS)


def test_the_canonicaliser_recovers_none_of_the_obfuscated_formats():
    """**This test is the honesty of the whole capability.**

    `SETTLEMENT_RE` matches `setl_[A-Za-z0-9]+`. If it recovered even one of
    these, that instance would be deterministic work misfiled as LLM work: T0
    applies no date window, so a recovered reference plus a closing sum is a
    match at confidence 1.00 and the analyst never sees the subject at all.

    It is asserted over every format and a spread of ids rather than one
    rendered sample, because a format that happens to hide the reference for
    `setl_00046` and expose it for `setl_00460` is not a format that hides it.
    """
    from core.canonicalize.narration import canonicalize
    from core.generator.defects import OBFUSCATION_FORMATS, render_obfuscated

    for settlement_id in ("setl_00001", "setl_00046", "setl_04710", "setl_99999"):
        for template in OBFUSCATION_FORMATS:
            narration = render_obfuscated(
                template, settlement_id, settled_at=date(2026, 8, 14), salt=3
            )
            recovered = canonicalize(narration).settlement_id
            assert recovered is None, (
                f"{template!r} rendered {narration!r}, from which the "
                f"canonicaliser recovered {recovered!r}"
            )
            assert settlement_id not in narration


def test_every_format_still_carries_the_reference_a_human_could_read():
    """The other half of honesty. A narration that has *lost* the reference is
    `garbled_narration`, which is already a defect and is resolvable on amount
    alone. This one has to remain readable, or the model is being asked to
    invent rather than to read."""
    import re as _re

    from core.generator.defects import OBFUSCATION_FORMATS, render_obfuscated

    for settlement_id in ("setl_00046", "setl_04710"):
        bare = settlement_id.split("_", 1)[1].lstrip("0") or "0"
        for template in OBFUSCATION_FORMATS:
            narration = render_obfuscated(
                template, settlement_id, settled_at=date(2026, 8, 14), salt=3
            )
            squashed = _re.sub(r"[^0-9A-Za-z]", "", narration)
            assert bare in squashed, f"{narration!r} does not name {settlement_id}"


def test_the_pool_is_drawn_from_per_instance_and_not_fixed():
    """"Chosen per-instance from the seeded pool" -- so different seeds must
    produce different shapes, or there is one format wearing four hats."""
    from core.generator.defects import OBFUSCATION_FORMATS

    shapes = set()
    for seed in range(40):
        _, target, _ = _obfuscated(seed)
        shapes.add(target.bank_line.narration)
    assert len(shapes) >= min(4, len(OBFUSCATION_FORMATS))


def test_the_credit_still_equals_the_reconstruction_exactly():
    """The model recovers a reference; the verifier does every rupee. An
    arithmetic break here would make the capability untestable -- every
    recovery would be rejected on `arithmetic` for a reason that has nothing to
    do with reading."""
    _, target, _ = _obfuscated()
    assert target.bank_line.credit == true_net(target)


def test_the_bank_line_posts_outside_the_deterministic_window():
    """Why the subject reaches the analyst at all.

    An obfuscated narration on its own is not enough: the credit still equals
    the reconstruction, so T1/T2/T3 would match it on amount and the analyst
    would never see it. What holds the subject back is the two-day window --
    the ONE thing the deterministic tiers check and the verifier does not.
    `_causality` still has to hold, so the money must post AFTER it settled.
    """
    from core.matcher.tiers import WINDOW_DAYS

    for seed in range(20):
        _, target, _ = _obfuscated(seed)
        delay = (target.bank_line.txn_date - target.settled_at).days
        assert delay > WINDOW_DAYS, "a window-visible line is matched deterministically"
        assert delay > 0, "money cannot post before the settlement that produced it"


def test_the_utr_is_kept_and_is_not_a_settlement_reference():
    """`referenced_settlement` reads the `utr` column as well as the narration.
    A UTR that canonicalised to a settlement id would hand T0 the answer."""
    from core.canonicalize.narration import canonicalize

    _, target, _ = _obfuscated()
    utr = target.bank_line.utr
    assert utr, "a real NEFT credit carries a UTR; removing it is a different defect"
    assert canonicalize(utr).settlement_id is None
    assert target.settlement_id not in utr


def test_truth_keeps_the_answer_and_records_it_resolvable():
    """`resolvable: true` with the real linkage: the reference IS in the data,
    so a correct recovery must raise `assisted_match_rate`, and an incorrect one
    must be gradeable as a false match."""
    result, target, _ = _obfuscated()
    assert result.resolvable is True
    assert result.affected_ids == [target.bank_line.line_id]
    assert target.linkage.bank_line_id == target.bank_line.line_id
    assert target.linkage.settlement_id == target.settlement_id
    assert target.linkage.psp_txn_ids == [t.txn_id for t in target.psp_txns]
