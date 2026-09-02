import pytest
from pydantic import ValidationError
from core.models import Order, PSPTransaction, BankLine, ReasonCode

def test_order_rejects_float_amount():
    with pytest.raises(ValidationError):
        Order(order_id="ORD-1", order_date="2026-08-01", customer_ref="C1",
              gross_amount=100.50, currency="INR", status="paid")

def test_psp_transaction_allows_missing_order_ref():
    t = PSPTransaction(txn_id="pay_1", txn_type="payment", order_id=None,
                       captured_at="2026-08-01T10:00:00", amount=100_000,
                       settlement_id="setl_A", settled_at="2026-08-03")
    assert t.order_id is None

def test_bank_line_narration_is_preserved_verbatim():
    b = BankLine(line_id="BL-1", txn_date="2026-08-03",
                 narration="RZPX*ACME  RET PL", credit=4_655_654,
                 debit=None, balance=9_999_999, utr=None)
    assert b.narration == "RZPX*ACME  RET PL"

def test_reason_codes_are_exhaustive():
    assert {c.value for c in ReasonCode} == {
        "NO_SETTLEMENT_REF", "AMOUNT_MISMATCH", "ORPHAN_BANK_LINE",
        "ORPHAN_PSP_TXN", "DUPLICATE_PSP_TXN", "AMBIGUOUS_MULTI_CANDIDATE",
        "UNPARSEABLE_NARRATION", "MISSING_ORDER_REF",
    }


def test_metrics_carries_every_spec_section_9_field():
    from core.models import Metrics
    expected = {
        "auto_match_rate", "assisted_match_rate", "exception_rate",
        "false_match_rate", "precision", "recall_on_resolvable",
        "trap_capture_rate", "llm_rejection_rate",
        "throughput_records_per_sec", "llm_cost_usd_per_100",
        "llm_tokens_per_100", "tier_counts",
        # Spec section 6. Rupee figures, not rates -- integer paise, and the
        # only fields here that are neither a rate nor a count.
        "itc_substantiated_paise", "itc_at_risk_paise", "itc_variance_paise",
    }
    assert set(Metrics.model_fields) == expected


# --- Metrics.tier_counts ----------------------------------------------------
#
# The run-detail screen owes the viewer a match-rate breakdown by tier (spec
# §13 #2). It is not decoration: the honest shape of a real walk is lopsided --
# one tier carries almost everything and the others score in single digits --
# and being able to SHOW the small ones, zeros included, is the claim. A tier
# absent from the dict and a tier at zero are different claims, so the contract
# fixes the key set rather than leaving it to whatever the engine happened to
# score.

ZERO_TIERS = {"T0": 0, "T1": 0, "T2": 0, "T3": 0, "LLM": 0}


def _metrics(**overrides):
    from core.models import Metrics
    kwargs = dict(
        auto_match_rate=0.0, assisted_match_rate=0.0, exception_rate=0.0,
        false_match_rate=0.0, precision=1.0, recall_on_resolvable=0.0,
        trap_capture_rate=1.0, llm_rejection_rate=0.0,
        throughput_records_per_sec=0.0, llm_cost_usd_per_100=0.0,
        llm_tokens_per_100=0, tier_counts=dict(ZERO_TIERS),
        itc_substantiated_paise=0, itc_at_risk_paise=0, itc_variance_paise=0,
    )
    kwargs.update(overrides)
    return Metrics(**kwargs)


# --- Metrics ITC fields (spec section 6) ------------------------------------
#
# The three fields that turn a match rate into a rupee figure. They are money,
# so they are int paise and never float -- `llm_cost_usd_per_100` sitting two
# fields above them is the exception that proves the rule, and it is a dollar
# cost, not paise.


@pytest.mark.parametrize(
    "field",
    ["itc_substantiated_paise", "itc_at_risk_paise", "itc_variance_paise"],
)
def test_metrics_rejects_a_float_itc_figure(field):
    with pytest.raises(ValidationError):
        _metrics(**{field: 4_242.5})


def test_metrics_itc_variance_may_be_negative():
    """Signed, unlike the other two. A month invoiced above what the run could
    substantiate is a real position and the sign is the finding."""
    assert _metrics(itc_variance_paise=-9_440_88).itc_variance_paise == -944_088


def test_metrics_itc_figures_survive_a_round_trip_as_integers():
    from core.models import Metrics
    original = _metrics(
        itc_substantiated_paise=3_235_401,
        itc_at_risk_paise=2_185_340,
        itc_variance_paise=-944_088,
    )
    restored = Metrics.model_validate_json(original.model_dump_json())
    assert restored.itc_substantiated_paise == 3_235_401
    assert type(restored.itc_at_risk_paise) is int
    assert restored.itc_variance_paise == -944_088


def test_metrics_tier_counts_keys_match_the_match_group_tier_labels():
    """One set of tier labels, not two. `MatchGroup.tier` and the keys of
    `tier_counts` are the same fact counted twice; a sixth tier that appeared in
    one and not the other would be drift with no test to catch it."""
    import typing
    from core.models import MatchGroup, TIER_KEYS
    tier_literal = set(typing.get_args(MatchGroup.model_fields["tier"].annotation))
    assert set(TIER_KEYS) == tier_literal == {"T0", "T1", "T2", "T3", "LLM"}


def test_metrics_rejects_tier_counts_missing_a_key():
    """A missing key is not "zero implied" -- it is an unanswered question."""
    partial = {k: v for k, v in ZERO_TIERS.items() if k != "T1"}
    with pytest.raises(ValidationError):
        _metrics(tier_counts=partial)


def test_metrics_rejects_tier_counts_carrying_an_unknown_tier():
    with pytest.raises(ValidationError):
        _metrics(tier_counts={**ZERO_TIERS, "T4": 0})


def test_metrics_rejects_an_empty_tier_counts():
    with pytest.raises(ValidationError):
        _metrics(tier_counts={})


def test_metrics_tier_counts_survives_a_round_trip_with_zeros_present():
    """Zeros must reach the wire. A serialiser that dropped them would leave the
    UI unable to distinguish "T1 matched nothing" from "T1 was not reported"."""
    from core.models import Metrics
    original = _metrics(tier_counts={"T0": 136, "T1": 0, "T2": 8, "T3": 5, "LLM": 0})

    restored = Metrics.model_validate_json(original.model_dump_json())

    assert restored.tier_counts == {"T0": 136, "T1": 0, "T2": 8, "T3": 5, "LLM": 0}
    assert set(restored.tier_counts) == {"T0", "T1", "T2", "T3", "LLM"}
    assert "T1" in original.model_dump_json(), "a zero tier must be serialised, not omitted"


def _exception(**overrides):
    from core.models import ReconException
    kwargs = dict(
        exception_id="EX-1", subject_type="bank_line", subject_id="BL-0005",
        reason_code=ReasonCode.AMBIGUOUS_MULTI_CANDIDATE, amount=2_430_380,
        llm_hypothesis=None, verifier_verdict="not_attempted",
        verifier_reason=None, failed_check=None,
    )
    kwargs.update(overrides)
    return ReconException(**kwargs)


def test_recon_exception_failed_check_accepts_none():
    assert _exception(failed_check=None).failed_check is None


@pytest.mark.parametrize(
    "check",
    ["existence", "exclusivity", "causality", "arithmetic", "uniqueness"],
)
def test_recon_exception_failed_check_accepts_each_verifier_check(check):
    e = _exception(verifier_verdict="rejected", failed_check=check)
    assert e.failed_check == check


def test_recon_exception_rejects_an_unknown_failed_check():
    with pytest.raises(ValidationError):
        _exception(failed_check="vibes")


def _run_summary(**overrides):
    from core.models import RunSummary
    kwargs = dict(
        run_id="r1", seed=42, record_count=50, state="running",
        created_at="2026-08-28T09:15:00", match_count=0, exception_count=0,
        metrics=None,
    )
    kwargs.update(overrides)
    return RunSummary(**kwargs)


def test_run_summary_allows_metrics_none_while_running():
    """A run still executing has no metrics yet, and `null` is the honest
    rendering. Adding `created_at` must not have made metrics mandatory: the
    two fields answer different questions, and a pending run can answer only
    one of them."""
    s = _run_summary(state="running", metrics=None)
    assert s.metrics is None
    assert s.created_at is not None


def test_run_summary_requires_created_at():
    """No default and no default_factory, deliberately. A default would have to
    come from a clock, and `core/` may not read one."""
    from core.models import RunSummary
    with pytest.raises(ValidationError):
        RunSummary(run_id="r1", seed=42, record_count=50, state="running",
                   match_count=0, exception_count=0, metrics=None)


def test_run_summary_created_at_has_no_clock_default():
    """The field must not manufacture its own value. `core/` is forbidden
    wall-clock; this timestamp is data stamped at the API boundary and handed
    in. A later lane adding `default_factory=datetime.now` would move the clock
    back into `core/` and this test is the tripwire."""
    from core.models import RunSummary
    field = RunSummary.model_fields["created_at"]
    assert field.is_required()
    assert field.default_factory is None


def test_run_summary_created_at_round_trips_as_a_datetime():
    from datetime import datetime
    from core.models import RunSummary
    s = _run_summary(created_at="2026-08-28T09:15:00")
    assert s.created_at == datetime(2026, 8, 28, 9, 15)
    assert RunSummary.model_validate_json(s.model_dump_json()).created_at == s.created_at


def test_no_module_under_core_reads_a_wall_clock():
    """The global constraint, asserted rather than trusted.

    `created_at` and `Metrics.throughput_records_per_sec` are both time-shaped
    and both stamped at the API boundary; the temptation to "helpfully" default
    them from `datetime.now()` lands squarely in `core/`. Audit ordering uses a
    monotonic `sequence` int for exactly this reason (see `AuditEntry`).

    `tests/test_boundaries.py` owns the import-level separation proofs and is
    frozen; this is the narrower value-level check for the one constraint this
    field introduces."""
    import ast
    import pathlib

    core_dir = pathlib.Path(__file__).resolve().parent.parent / "core"
    paths = list(core_dir.rglob("*.py"))
    assert paths, "no .py files under core/; the check would pass vacuously"

    banned = {("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"),
              ("date", "today"), ("time", "time"), ("time", "monotonic")}
    offenders = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if (func.value.id, func.attr) in banned:
                    offenders.append(f"{path.name}:{node.lineno} {func.value.id}.{func.attr}()")
    assert not offenders, (
        "wall-clock inside core/ -- timestamps are stamped at the API boundary: "
        f"{offenders}"
    )


# --- api/openapi.yaml must mirror core/models.py ----------------------------
#
# The API contract restates every frozen model field-for-field, because web/ may
# not read core/models.py. Two hand-maintained copies of one fact is exactly the
# drift this project keeps paying for, so the agreement is checked, not eyeballed:
# field set, required set, and the JSON type (including nullability) of every
# field, for every model the contract mirrors.

MIRRORED_MODELS = [
    "Order", "PSPTransaction", "BankLine", "MatchGroup", "Settlement",
    "ReconException", "AuditEntry", "Metrics", "RunSummary",
    "MetricMove", "ReasonCodeMove", "DriftReport",
]


def _openapi_schemas():
    yaml = pytest.importorskip("yaml")
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    spec = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    return spec["components"]["schemas"]


def _resolve(node, schemas):
    while "$ref" in node:
        node = schemas[node["$ref"].rsplit("/", 1)[-1]]
    return node


def _json_types(node, schemas) -> set[str]:
    """The JSON types a schema node admits, following $ref and anyOf/oneOf.
    `anyOf: [$ref Metrics, "null"]` is `{"object", "null"}` -- the nullability
    the pydantic annotation has to match."""
    node = _resolve(node, schemas)
    for key in ("anyOf", "oneOf"):
        if key in node:
            found: set[str] = set()
            for sub in node[key]:
                found |= _json_types(sub, schemas)
            return found
    declared = node.get("type")
    assert declared is not None, f"schema node has no type: {node}"
    return set(declared) if isinstance(declared, list) else {declared}


def _py_types(annotation) -> set[str]:
    """The JSON types a pydantic annotation admits. `Literal[...]` follows the
    type of its values; an Enum is its value type; a `| None` adds "null"."""
    import datetime as dt
    import enum
    import types as pytypes
    import typing

    primitives = {
        float: "number", int: "integer", str: "string", bool: "boolean",
        dt.datetime: "string", dt.date: "string",
    }
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, pytypes.UnionType):
        found: set[str] = set()
        for arg in typing.get_args(annotation):
            found |= _py_types(arg)
        return found
    if annotation is type(None):
        return {"null"}
    if origin is typing.Literal:
        return {primitives[type(v)] for v in typing.get_args(annotation)}
    if origin in (list, set, frozenset, tuple):
        return {"array"}
    if origin is dict:
        return {"object"}
    assert isinstance(annotation, type), f"unhandled annotation {annotation!r}"
    if issubclass(annotation, enum.Enum):
        return {"string"}
    if annotation in primitives:
        return {primitives[annotation]}
    return {"object"}


@pytest.mark.parametrize("name", MIRRORED_MODELS)
def test_openapi_mirrors_the_pydantic_model(name):
    import core.models

    schemas = _openapi_schemas()
    model = getattr(core.models, name)
    schema = schemas[name]
    properties = schema.get("properties", {})

    assert set(model.model_fields) == set(properties), (
        f"{name}: core/models.py and api/openapi.yaml declare different fields; "
        f"symmetric difference {sorted(set(model.model_fields) ^ set(properties))}"
    )
    assert set(schema.get("required", [])) == set(properties), (
        f"{name}: every field of the pydantic model is required (nullable is not "
        "optional -- the key is always present), so the OpenAPI `required` list "
        "must name every property. Missing: "
        f"{sorted(set(properties) - set(schema.get('required', [])))}"
    )
    for field_name, field in model.model_fields.items():
        assert _py_types(field.annotation) == _json_types(properties[field_name], schemas), (
            f"{name}.{field_name}: model admits "
            f"{sorted(_py_types(field.annotation))}, OpenAPI admits "
            f"{sorted(_json_types(properties[field_name], schemas))}"
        )


def test_openapi_tier_counts_pins_all_five_keys_as_required_integers():
    """The pydantic validator and the OpenAPI object must reject the same dicts.
    A type-level `object` with no required keys would let a serialiser omit a
    zero and still validate."""
    schemas = _openapi_schemas()
    node = schemas["Metrics"]["properties"]["tier_counts"]

    assert set(node["required"]) == {"T0", "T1", "T2", "T3", "LLM"}
    assert set(node["properties"]) == {"T0", "T1", "T2", "T3", "LLM"}
    assert all(sub["type"] == "integer" for sub in node["properties"].values())
    assert node["additionalProperties"] is False, "an unknown tier must be rejected here too"
