"""The Gemini analyst client -- the second implementation of the same seam.

The architecture claim this project rests on is that the LLM is interchangeable
behind a deterministic verifier. A second provider is how that claim is
demonstrated rather than asserted, so what this file pins is not "Gemini works"
but "Gemini is the same shape": it satisfies the same `AnalystClient` Protocol,
it withholds the same ground truth, and a response it cannot parse produces
nothing rather than a guess.

Every test here runs **offline**. No key is read -- the fixtures below delete
every credential variable -- and sockets are refused outright, so a test that
reached for the network would fail rather than silently pass on a developer
machine that happens to have one.

One thing is deliberately NOT symmetric with the Anthropic path. `analyse`
drops malformed *responses*; a call that never produced a response at all --
a wrong model id, a model not available on this key -- is a different failure
and must stay loud, naming the id it tried and the variable that overrides it.
That is the last test in this file, and it is the one that stops a demo failing
as a silent empty result.
"""

from __future__ import annotations

import inspect
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.llm.analyst import (
    GEMINI_MODEL_ENV,
    GEMINI_PRICING_USD_PER_MTOK,
    AnalystClient,
    AnthropicAnalystClient,
    GeminiAnalystClient,
    GeminiCallFailed,
    analyse,
)
from core.llm.prompts import HYPOTHESIS_SCHEMA, TOOL_NAME
from core.models import BankLine, PSPTransaction, ReasonCode, ReconException

REPO_ROOT = Path(__file__).resolve().parents[2]

VALID_PAYLOAD = [
    {
        "subject_id": "BL-1",
        "proposed_bank_line_id": "BL-1",
        "proposed_psp_txn_ids": ["pay_1"],
        "proposed_order_ids": [],
        "reasoning": "amount matches",
        "self_confidence": 0.8,
    }
]

#: Every variable that could make one of these tests hit a live endpoint or
#: change its answer depending on whose machine it runs on.
CREDENTIAL_VARS = (
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "RECON_LLM_PROVIDER",
    "RECON_GEMINI_MODEL",
)


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    """No keys, no sockets. Asserting rather than skipping: a test in this file
    that opened a socket would be a bug in the client, not a reason to pass."""
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)

    def _refuse(*args, **kwargs):
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


# --- a fake in the shape the SDK actually returns ------------------------------


class FakeModels:
    """`client.models.generate_content(...)`, recording what it was handed."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def fake_sdk(*, function_calls=(), usage=None, error=None):
    """Returns `(sdk, models)` so a test can assert on what was sent."""
    response = SimpleNamespace(
        function_calls=list(function_calls), usage_metadata=usage
    )
    models = FakeModels(response, error)
    return SimpleNamespace(models=models), models


def tool_call(payload, name: str = TOOL_NAME):
    return SimpleNamespace(name=name, args={"hypotheses": payload})


def usage(prompt=0, candidates=0, thoughts=None, cached=0, total=None):
    """One `usage_metadata` block.

    `thoughts=None` OMITS the field entirely, which is what a non-thinking
    model's response actually looks like -- not a zero. `total=None` computes
    the total the way the API reports it, so a test has to opt in to the
    inconsistent case rather than stumble into it.
    """
    fields = {
        "prompt_token_count": prompt,
        "candidates_token_count": candidates,
        "cached_content_token_count": cached,
    }
    if thoughts is not None:
        fields["thoughts_token_count"] = thoughts
    fields["total_token_count"] = (
        total if total is not None else prompt + candidates + (thoughts or 0)
    )
    return SimpleNamespace(**fields)


@pytest.fixture
def one_exception() -> ReconException:
    return ReconException(
        exception_id="EXC-1",
        subject_type="bank_line",
        subject_id="BL-1",
        reason_code=ReasonCode.NO_SETTLEMENT_REF,
        amount=4_794_654,
        llm_hypothesis=None,
        verifier_verdict="not_attempted",
        verifier_reason=None,
        failed_check=None,
    )


@pytest.fixture
def minimal_context():
    from core.llm.prompts import AnalystContext

    return AnalystContext(
        bank_lines=[
            BankLine(
                line_id="BL-1",
                txn_date="2026-07-24",
                narration="NEFT RAZORPAY CREDIT",
                credit=4_794_654,
                debit=None,
                balance=0,
                utr=None,
            )
        ],
        psp_txns=[
            PSPTransaction(
                txn_id="pay_1",
                txn_type="payment",
                order_id="ORD-1",
                captured_at="2026-07-20T10:00:00",
                amount=4_932_000,
                settlement_id="setl_A",
                settled_at="2026-07-24",
            ),
            PSPTransaction(
                txn_id="fee_1",
                txn_type="fee",
                order_id=None,
                captured_at="2026-07-20T10:00:00",
                amount=-116_395,
                settlement_id="setl_A",
                settled_at="2026-07-24",
            ),
        ],
    )


# --- the seam ------------------------------------------------------------------


def test_the_gemini_client_conforms_to_the_analyst_protocol():
    """A structural check, not a comment claiming conformance.

    `isinstance` against a runtime-checkable Protocol verifies the method is
    really there. It only checks the NAME, though, so the signature is pinned
    against the Anthropic client's as well -- a `call(self)` taking no arguments
    would otherwise satisfy it. The negative case is included because a check
    that everything passes is a check that proves nothing.
    """
    gemini = GeminiAnalystClient(SimpleNamespace())
    anthropic = AnthropicAnalystClient(SimpleNamespace())

    assert isinstance(gemini, AnalystClient)
    assert isinstance(anthropic, AnalystClient)
    assert not isinstance(SimpleNamespace(), AnalystClient)

    assert (
        list(inspect.signature(GeminiAnalystClient.call).parameters)
        == list(inspect.signature(AnthropicAnalystClient.call).parameters)
    )


def test_returns_typed_hypotheses_through_the_gemini_path(
    one_exception, minimal_context
):
    sdk, _ = fake_sdk(function_calls=[tool_call(VALID_PAYLOAD)])
    out = analyse([one_exception], minimal_context, GeminiAnalystClient(sdk))
    assert [h.subject_id for h in out] == ["BL-1"]
    assert out[0].proposed_psp_txn_ids == ["pay_1"]


def test_the_frozen_schema_is_sent_verbatim(one_exception, minimal_context):
    """The same `HYPOTHESIS_SCHEMA` the Anthropic path sends, unmodified.

    `parameters_json_schema` takes raw JSON Schema, so the union type on
    `proposed_bank_line_id` survives -- which is what makes a translation layer
    (and a second, drifting copy of the contract) unnecessary.
    """
    sdk, models = fake_sdk(function_calls=[tool_call([])])
    analyse([one_exception], minimal_context, GeminiAnalystClient(sdk))

    declaration = models.calls[0]["config"]["tools"][0]["function_declarations"][0]
    assert declaration["name"] == TOOL_NAME
    assert declaration["parameters_json_schema"] is HYPOTHESIS_SCHEMA
    assert declaration["parameters_json_schema"]["properties"]["hypotheses"]["items"][
        "properties"
    ]["proposed_bank_line_id"]["type"] == ["string", "null"]


def test_the_model_is_forced_to_answer_through_the_tool(
    one_exception, minimal_context
):
    """Structured output, not prose. Free text would give `analyse` a string to
    guess at, which is exactly what the drop-never-repair contract forbids."""
    sdk, models = fake_sdk(function_calls=[tool_call([])])
    analyse([one_exception], minimal_context, GeminiAnalystClient(sdk))

    calling_config = models.calls[0]["config"]["tool_config"]["function_calling_config"]
    assert calling_config["mode"] == "ANY"
    assert calling_config["allowed_function_names"] == [TOOL_NAME]


# --- what the prompt may and may not carry -------------------------------------


def test_the_prompt_sent_to_gemini_contains_no_ground_truth(
    one_exception, minimal_context
):
    """The same guarantee the Anthropic path has, asserted on what was actually
    handed to this SDK rather than on what `render_prompt` returned."""
    sdk, models = fake_sdk(function_calls=[tool_call([])])
    analyse([one_exception], minimal_context, GeminiAnalystClient(sdk))

    sent = models.calls[0]["contents"]
    assert "truth" not in sent.lower()
    assert "unresolvable" not in sent.lower()


def test_that_guarantee_is_not_vacuous_on_an_empty_prompt(
    one_exception, minimal_context
):
    """A client that sent the empty string would pass the test above. The
    evidence has to be there for withholding the answers to mean anything."""
    sdk, models = fake_sdk(function_calls=[tool_call([])])
    analyse([one_exception], minimal_context, GeminiAnalystClient(sdk))

    sent = models.calls[0]["contents"]
    assert "BL-1" in sent
    assert "pay_1" in sent
    assert "setl_A" in sent
    assert "net=4815605" in sent  # 4_932_000 - 116_395, computed for the model


# --- malformed in, nothing out -------------------------------------------------


def test_a_response_with_no_function_call_yields_no_hypotheses(
    one_exception, minimal_context
):
    sdk, _ = fake_sdk(function_calls=[])
    assert analyse([one_exception], minimal_context, GeminiAnalystClient(sdk)) == []


def test_a_function_call_of_another_name_is_ignored(one_exception, minimal_context):
    sdk, _ = fake_sdk(function_calls=[tool_call(VALID_PAYLOAD, name="something_else")])
    assert analyse([one_exception], minimal_context, GeminiAnalystClient(sdk)) == []


@pytest.mark.parametrize("payload", [None, "not a list", 7, {"hypotheses": []}])
def test_a_response_of_the_wrong_shape_yields_no_hypotheses(
    one_exception, minimal_context, payload
):
    sdk, _ = fake_sdk(function_calls=[tool_call(payload)])
    assert analyse([one_exception], minimal_context, GeminiAnalystClient(sdk)) == []


def test_a_malformed_entry_is_dropped_without_discarding_the_valid_ones(
    one_exception, minimal_context
):
    """Dropping means dropping -- never repairing a partial object into a
    plausible one, and never letting one bad entry take the good ones with it."""
    sdk, _ = fake_sdk(
        function_calls=[
            tool_call([{"garbage": 1}, *VALID_PAYLOAD, {"subject_id": "BL-9"}])
        ]
    )
    out = analyse([one_exception], minimal_context, GeminiAnalystClient(sdk))
    assert [h.subject_id for h in out] == ["BL-1"]


# --- what it billed ------------------------------------------------------------


def test_the_token_figure_is_the_total_the_api_reported():
    """Google is the authority on what it billed, so `tokens` is its total --
    never a sum reconstructed on this side.

    The figures are a real `usageMetadata` block from this model: a five-token
    prompt and a one-token reply cost seventy tokens, because sixty-four of them
    were reasoning. A client reporting `prompt + candidates` would have said
    six. That understatement is not a fixed factor either -- thinking scales
    with how hard the problem is, not with how long the prompt is, so the
    analyst's long structured prompts would be wrong by an unpredictable amount.
    """
    sdk, _ = fake_sdk(
        function_calls=[tool_call([])], usage=usage(prompt=5, candidates=1, thoughts=64)
    )
    client = GeminiAnalystClient(sdk)

    client.call("prompt", HYPOTHESIS_SCHEMA)
    assert client.tokens == 70
    client.call("prompt", HYPOTHESIS_SCHEMA)
    assert client.tokens == 140


def test_reasoning_tokens_are_broken_out_and_counted_as_output():
    """Broken out because "how much of this spend was thinking" is a genuinely
    interesting number for a reconciliation workload and costs nothing to
    capture -- and folded into output because that is the rate they bill at.

    `cached_content_token_count` is deliberately NOT added anywhere: Gemini
    reports it as a SUBSET of `prompt_token_count`, unlike Anthropic which
    reports cache tokens alongside `input_tokens`. Adding it would double-count.
    """
    sdk, _ = fake_sdk(
        function_calls=[tool_call([])],
        usage=usage(prompt=5, candidates=1, thoughts=64, cached=3),
    )
    client = GeminiAnalystClient(sdk)
    client.call("prompt", HYPOTHESIS_SCHEMA)

    assert client.thoughts_tokens == 64
    assert client.input_tokens == 5
    assert client.output_tokens == 65


def test_a_model_that_reports_no_reasoning_tokens_does_not_raise():
    """Non-thinking models omit the field outright rather than sending a zero.
    Absent must mean zero, not an AttributeError in the middle of a run."""
    sdk, _ = fake_sdk(
        function_calls=[tool_call([])], usage=usage(prompt=5, candidates=1)
    )
    client = GeminiAnalystClient(sdk)

    assert client.call("prompt", HYPOTHESIS_SCHEMA) == []
    assert client.thoughts_tokens == 0
    assert client.tokens == 6


def test_a_response_carrying_no_usage_block_does_not_take_the_run_down():
    """Usage is telemetry. A run that reconciled correctly must not fail
    because the provider omitted a counter."""
    sdk, _ = fake_sdk(function_calls=[tool_call([])], usage=None)
    client = GeminiAnalystClient(sdk)
    assert client.call("prompt", HYPOTHESIS_SCHEMA) == []
    assert client.tokens == 0
    assert client.cost_usd == 0.0


def test_a_usage_block_with_no_total_falls_back_to_its_parts():
    """Preferring the reported total must not mean reporting zero when the
    provider omits it -- that would understate a real spend as nothing."""
    sdk, _ = fake_sdk(
        function_calls=[tool_call([])],
        usage=SimpleNamespace(prompt_token_count=100, candidates_token_count=20),
    )
    client = GeminiAnalystClient(sdk)
    client.call("prompt", HYPOTHESIS_SCHEMA)
    assert client.tokens == 120


def test_cost_prices_reasoning_at_the_output_rate():
    """One million in, and one million out of which most is reasoning."""
    sdk, _ = fake_sdk(
        function_calls=[tool_call([])],
        usage=usage(prompt=1_000_000, candidates=400_000, thoughts=600_000),
    )
    model = next(iter(GEMINI_PRICING_USD_PER_MTOK))
    client = GeminiAnalystClient(sdk, model=model)
    client.call("prompt", HYPOTHESIS_SCHEMA)

    per_input, per_output = GEMINI_PRICING_USD_PER_MTOK[model]
    assert client.cost_usd == pytest.approx(per_input + per_output)


def test_an_unpriced_model_reports_no_cost_rather_than_a_guess():
    """The rates in the table are unverified defaults. A model id absent from it
    reports 0.0 -- an absent cost, not an invented one -- while `tokens` still
    reports truthfully what was spent."""
    sdk, _ = fake_sdk(
        function_calls=[tool_call([])], usage=usage(prompt=5_000, candidates=5)
    )
    client = GeminiAnalystClient(sdk, model="some-unlisted-model")
    client.call("prompt", HYPOTHESIS_SCHEMA)
    assert client.tokens == 5_005
    assert client.cost_usd == 0.0


# --- the failure that must stay loud -------------------------------------------


def test_a_failed_call_names_the_model_id_and_the_variable_that_overrides_it():
    """"404 model not found" with no context is the worst thing to read during a
    recorded demo. The message has to say which id was tried and which knob
    changes it, and it must not claim to know that the id is the problem."""
    sdk, _ = fake_sdk(error=RuntimeError("404 NOT_FOUND: model not found"))
    client = GeminiAnalystClient(sdk, model="gemini-no-such-model")

    with pytest.raises(GeminiCallFailed) as raised:
        client.call("prompt", HYPOTHESIS_SCHEMA)

    message = str(raised.value)
    assert "gemini-no-such-model" in message
    assert GEMINI_MODEL_ENV in message
    assert "404 NOT_FOUND: model not found" in message
    assert "RuntimeError" in message


def test_a_failed_call_is_not_swallowed_as_an_empty_result(
    one_exception, minimal_context
):
    """`analyse` drops malformed RESPONSES. A call that never produced one is a
    different thing, and must not arrive at the caller looking like "the model
    considered the evidence and proposed nothing"."""
    sdk, _ = fake_sdk(error=RuntimeError("404 NOT_FOUND"))
    with pytest.raises(GeminiCallFailed):
        analyse(
            [one_exception],
            minimal_context,
            GeminiAnalystClient(sdk, model="gemini-no-such-model"),
        )


def test_the_original_exception_is_kept_as_the_cause():
    """Wrapping adds context; it must not destroy the traceback that says what
    actually went wrong at the transport layer."""
    original = RuntimeError("connection reset")
    sdk, _ = fake_sdk(error=original)
    with pytest.raises(GeminiCallFailed) as raised:
        GeminiAnalystClient(sdk).call("prompt", HYPOTHESIS_SCHEMA)
    assert raised.value.__cause__ is original


# --- neither SDK is imported by core -------------------------------------------


def _import_probe(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_importing_the_analyst_pulls_in_neither_sdk():
    """Both clients import their SDK lazily, inside the builder.

    This is what keeps `core/` importable on a machine with no credentials and
    no provider package, and it is why the whole suite can run offline. A
    module-level `import anthropic` or `from google import genai` would pass
    every other test in this file and break exactly that.
    """
    probe = _import_probe(
        "import sys, core.llm.analyst;"
        "leaked = sorted(m for m in sys.modules"
        " if m.split('.')[0] == 'anthropic' or m.startswith('google.genai'));"
        "print(leaked);"
        "sys.exit(1 if leaked else 0)"
    )
    assert probe.returncode == 0, f"an SDK was imported: {probe.stdout}{probe.stderr}"


def test_that_probe_is_not_passing_because_the_sdks_are_missing():
    """The test above would pass just as well in an environment where neither
    SDK is installed, proving nothing. Both must actually be importable."""
    probe = _import_probe("import anthropic; from google import genai")
    assert probe.returncode == 0, probe.stderr


# --- spec §4: the model default, the rates, and the 503 retry -------------------


def test_the_default_model_is_the_one_a_clone_can_run_free():
    """`gemini-3.5-flash`, not `gemini-3.7-flash`.

    A default that bills on every run is a default that costs a reader money
    for cloning the repository. 3.7 stays reachable through
    `RECON_GEMINI_MODEL`; it is an override, not the floor.
    """
    from core.llm.analyst import GEMINI_MODEL

    assert GEMINI_MODEL == "gemini-3.5-flash"


def test_the_pricing_table_carries_both_documented_models_at_sourced_rates():
    """The committed `(0.30, 2.50)` was carried over from an older Flash tier
    and was wrong for every id in this table.

    Source: https://ai.google.dev/gemini-api/docs/pricing, read 2026-08-29.
    `gemini-3.7-flash` is $0.75/$3.75 per 1M through 2026-12-31 ($1.50/$7.50
    after); `gemini-3.5-flash` is $1.50/$9.00, output inclusive of thinking
    tokens -- which is why this client counts reasoning as output.
    """
    from core.llm.analyst import GEMINI_MODEL, GEMINI_PRICING_USD_PER_MTOK

    assert GEMINI_PRICING_USD_PER_MTOK["gemini-3.7-flash"] == (0.75, 3.75)
    assert GEMINI_PRICING_USD_PER_MTOK["gemini-3.5-flash"] == (1.50, 9.00)
    assert GEMINI_MODEL in GEMINI_PRICING_USD_PER_MTOK, (
        "the default model must be priced, or every run reports a cost of 0.0"
    )


class _Unavailable(RuntimeError):
    """A 503 in the shape the SDK raises one: a `code` attribute, plus text."""

    def __init__(self, message: str = "503 UNAVAILABLE: model is overloaded"):
        super().__init__(message)
        self.code = 503


class FlakyModels(FakeModels):
    """Fails the first `failures` attempts, then answers."""

    def __init__(self, response, failures: int, error=None):
        super().__init__(response, None)
        self._remaining = failures
        self._flake = error if error is not None else _Unavailable()

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self._remaining > 0:
            self._remaining -= 1
            raise self._flake
        return self._response


def _flaky_sdk(failures: int, *, error=None, payload=()):
    response = SimpleNamespace(
        function_calls=[tool_call(list(payload))], usage_metadata=usage(prompt=1)
    )
    models = FlakyModels(response, failures, error)
    return SimpleNamespace(models=models), models


def test_a_503_is_retried_and_the_call_still_succeeds():
    """Free-tier capacity is shared and returned 503 on two of three attempts
    during live testing. A recorded demo must not fail on transient load."""
    sdk, models = _flaky_sdk(2, payload=VALID_PAYLOAD)
    slept: list[float] = []
    client = GeminiAnalystClient(sdk, sleep=slept.append)

    assert client.call("prompt", HYPOTHESIS_SCHEMA) == VALID_PAYLOAD
    assert len(models.calls) == 3
    assert slept == [1.0, 2.0], "backoff must grow, not hammer a loaded endpoint"


def test_usage_is_counted_once_and_only_for_the_attempt_that_answered():
    """A retried call must not bill the caller for the attempts that 503'd --
    they returned no `usage_metadata` at all."""
    sdk, _ = _flaky_sdk(2, payload=[])
    client = GeminiAnalystClient(sdk, sleep=lambda _s: None)
    client.call("prompt", HYPOTHESIS_SCHEMA)
    assert client.input_tokens == 1


def test_a_503_that_never_clears_is_still_a_loud_failure():
    """Retry is not a way to turn an outage into an empty hypothesis list."""
    sdk, models = _flaky_sdk(99)
    client = GeminiAnalystClient(sdk, sleep=lambda _s: None)

    with pytest.raises(GeminiCallFailed) as raised:
        client.call("prompt", HYPOTHESIS_SCHEMA)

    from core.llm.analyst import GEMINI_MAX_ATTEMPTS

    assert len(models.calls) == GEMINI_MAX_ATTEMPTS
    assert str(GEMINI_MAX_ATTEMPTS) in str(raised.value)


def test_a_404_is_not_retried():
    """Only transient capacity is retried. A wrong model id is wrong on every
    attempt, and retrying it delays the one message that would fix it."""
    sdk, models = _flaky_sdk(99, error=RuntimeError("404 NOT_FOUND: model not found"))
    slept: list[float] = []
    client = GeminiAnalystClient(sdk, sleep=slept.append)

    with pytest.raises(GeminiCallFailed):
        client.call("prompt", HYPOTHESIS_SCHEMA)

    assert len(models.calls) == 1
    assert slept == []


def test_a_503_is_recognised_from_the_message_when_there_is_no_code_attribute():
    """Not every SDK version attaches a numeric `code`; some raise a plain
    exception whose text is the status. Both shapes must retry, because the one
    that does not is an outage that fails the demo."""
    sdk, models = _flaky_sdk(
        1,
        error=RuntimeError("ServerError: 503 UNAVAILABLE. The model is overloaded."),
        payload=[],
    )
    client = GeminiAnalystClient(sdk, sleep=lambda _s: None)
    client.call("prompt", HYPOTHESIS_SCHEMA)
    assert len(models.calls) == 2
