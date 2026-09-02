"""Bank narrations are deliberately messy (spec 10 #4).

The canonicaliser extracts three signals -- settlement reference, UTR, entity --
and is the ONLY place that normalises whitespace and case. Note what it is
*for*: the settlement reference and the UTR are identity evidence a tier may
act on; `entity` is recorded in evidence and never gates a match.
"""

from pathlib import Path

from core.canonicalize.narration import canonicalize
from core.canonicalize.txn_types import (
    ORDER_BEARING_TYPES,
    PAYMENT_TYPE,
    SETTLEMENT_LEVEL_TYPES,
)
from core.ingest.reader import read_bank

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "tiny"


def test_extracts_settlement_id():
    n = canonicalize("NEFT RAZORPAY setl_A1B2C3 CREDIT")
    assert n.settlement_id == "setl_A1B2C3"


def test_settlement_id_keeps_its_original_case():
    """`setl_` ids are matched against the PSP report verbatim, so the search
    runs on the raw text -- squashing uppercases and would break the join."""
    assert canonicalize("SETL setl_D4").settlement_id == "setl_D4"


def test_extracts_utr():
    n = canonicalize("NEFT UTR: SBIN226534120987 RAZORPAY")
    assert n.utr == "SBIN226534120987"


def test_squashes_whitespace_and_uppercases():
    assert canonicalize("RZPX*ACME   RET  PL").squashed == "RZPX*ACME RET PL"


def test_raw_is_preserved_untouched():
    raw = "RZPX*ACME  RET PL"
    assert canonicalize(raw).raw == raw


def test_extracts_entity_from_garbled_narration():
    assert canonicalize("RZPX*ACME RET PL").entity == "ACME RET"


def test_returns_none_entity_when_unparseable():
    n = canonicalize("MISC CREDIT 00000")
    assert n.entity is None
    assert n.settlement_id is None


def test_empty_narration_yields_no_signal():
    n = canonicalize("")
    assert (n.settlement_id, n.utr, n.entity) == (None, None, None)
    assert n.is_unparseable is True


def test_is_unparseable_is_false_when_any_signal_is_present():
    assert canonicalize("NEFT CR SETL setl_A1").is_unparseable is False
    assert canonicalize("RZPX*ACME RET PL").is_unparseable is False


# --- what the fixture's six narrations actually yield -----------------------
# Pinned so a regex tweak cannot silently change the engine's inputs.


def _fixture_narrations() -> dict[str, str]:
    return {b.line_id: b.narration for b in read_bank(FIX / "bank.csv")}


def test_fixture_reference_lines_yield_their_settlement_id():
    narrations = _fixture_narrations()
    assert canonicalize(narrations["BL-0001"]).settlement_id == "setl_A1"
    assert canonicalize(narrations["BL-0002"]).settlement_id == "setl_B2"
    assert canonicalize(narrations["BL-0004"]).settlement_id == "setl_D4"


def test_the_garbled_line_yields_an_entity_but_no_reference():
    """BL-0003 is the garbled_narration defect: no UTR, no settlement token.
    It must be matched on arithmetic alone, so T0 cannot fire on it."""
    n = canonicalize(_fixture_narrations()["BL-0003"])
    assert n.settlement_id is None
    assert n.utr is None
    assert n.entity == "ACME RET"


def test_the_trap_lines_carry_no_signal_whatsoever():
    """BL-0005/BL-0006 are byte-identical and name nothing. `entity is None`
    must never, on its own, change whether a tier fires."""
    narrations = _fixture_narrations()
    for line_id in ("BL-0005", "BL-0006"):
        n = canonicalize(narrations[line_id])
        assert (n.settlement_id, n.utr, n.entity) == (None, None, None)
    assert narrations["BL-0005"] == narrations["BL-0006"]


def test_no_narration_carries_an_inline_utr():
    """The fixture's UTRs live in the `utr` column, not in the narration, so a
    tier that only read the narration would miss them entirely."""
    for narration in _fixture_narrations().values():
        assert canonicalize(narration).utr is None


# --- the promoted order-bearing type constant (brief 7) ---------------------


def test_order_bearing_types_is_the_documented_set():
    """One named constant, imported everywhere. A dedup key or exception rule
    that re-spells this literal is the drift the constant exists to prevent
    (CSV_SCHEMAS.md 3.2.1)."""
    assert set(ORDER_BEARING_TYPES) == {"payment", "refund", "chargeback"}


def test_settlement_level_types_are_disjoint_from_order_bearing_types():
    assert set(SETTLEMENT_LEVEL_TYPES) == {"fee", "tax", "reserve"}
    assert not (set(SETTLEMENT_LEVEL_TYPES) & set(ORDER_BEARING_TYPES))


def test_the_cardinality_type_is_narrower_than_the_order_bearing_set():
    """T1/T2 cardinality counts `payment` legs ALONE. A refund leg does not
    make a settlement a batch (spec 7.1)."""
    assert PAYMENT_TYPE == "payment"
    assert PAYMENT_TYPE in ORDER_BEARING_TYPES
    assert set(ORDER_BEARING_TYPES) - {PAYMENT_TYPE} == {"refund", "chargeback"}
