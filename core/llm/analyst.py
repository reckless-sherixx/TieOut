"""The LLM analyst (spec §8.1) -- proposes, never decides.

`analyse` renders the unmatched residue into one prompt, asks the model for
structured output through tool use, and turns the response into typed
`Hypothesis` objects. Nothing here computes money and nothing here accepts
anything: every hypothesis this function returns is re-checked by
`core.llm.verifier.verify` before it can become a match.

Two properties are load-bearing and both are tested:

**The client is injected.** `analyse(exceptions, context, client)` takes the
client as an argument, so the suite runs with a stub -- no module-level client,
no `os.environ["ANTHROPIC_API_KEY"]` read at import time, no network in tests.

**A malformed response yields no hypotheses.** An entry that does not validate
against the frozen `Hypothesis` contract is dropped. It is never repaired into
a plausible object and never guessed at; `analyse` returning `[]` is a correct
outcome, and so is a model declining a subject it cannot determine.

There are **two implementations of that seam** -- `AnthropicAnalystClient` and
`GeminiAnalystClient` -- and keeping both is the point. This project's claim is
that the LLM is interchangeable behind a deterministic verifier; two working
providers demonstrate it rather than asserting it. Everything downstream of
`call` is shared, so neither the analyst, the verifier nor the accept loop can
tell which one it is talking to. Which one runs is decided at the API boundary
(`api/settings.resolve_provider`), never here.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from core.llm.prompts import (
    HYPOTHESIS_SCHEMA,
    TOOL_NAME,
    AnalystContext,
    render_prompt,
)
from core.models import Hypothesis, ReconException

#: Spec §8.1. Named here rather than read from the environment so the choice is
#: reviewable in the diff.
MODEL = "claude-sonnet-5"

MAX_TOKENS = 8192

#: List price in USD per million tokens, `(input, output)`, for the models this
#: client may be pointed at. Named here rather than computed anywhere else so
#: `Metrics.llm_cost_usd_per_100` traces to one reviewable number per model. A
#: model absent from this table reports a cost of 0.0 rather than a guess --
#: `llm_tokens_per_100` still reports what was actually spent.
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.00, 10.00),
}

TOOL_DESCRIPTION = (
    "Submit your reconciliation proposals. Return an empty list rather than a "
    "proposal you are not certain the evidence determines."
)


@runtime_checkable
class AnalystClient(Protocol):
    """The seam the analyst is tested through.

    One method, taking a rendered prompt and the tool's input schema and
    returning the raw `hypotheses` list the model produced. Anything narrower
    would drag the Anthropic SDK's response objects into every test.

    Token and cost accounting is deliberately NOT on this Protocol. A client
    that bills something exposes `tokens` and `cost_usd` (see
    `AnthropicAnalystClient`) and the accept loop reads them defensively; a stub
    exposes neither and its run reports zero, which is the truth about a run
    that spent nothing.
    """

    def call(self, prompt: str, schema: dict) -> Any: ...


class AnthropicAnalystClient:
    """The real client: `claude-sonnet-5`, structured output via tool use.

    Takes an already-constructed SDK client so nothing in `core/` reads an API
    key, constructs a network client at import time, or stamps a wall clock.

    It also counts what it spent. `input_tokens` and `output_tokens` accumulate
    across every call this instance makes, so `Metrics.llm_cost_usd_per_100` and
    `llm_tokens_per_100` come from what the API reported it billed -- not from an
    estimate, and not from a token count computed on this side.
    """

    def __init__(
        self,
        client: Any,
        model: str = MODEL,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self.input_tokens = 0
        self.output_tokens = 0

    @property
    def tokens(self) -> int:
        """Everything billed, input and output."""
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float:
        """List price for what this client has spent so far."""
        rates = PRICING_USD_PER_MTOK.get(self._model)
        if rates is None:
            return 0.0
        per_input, per_output = rates
        return (
            self.input_tokens * per_input + self.output_tokens * per_output
        ) / 1_000_000

    def _record_usage(self, message: Any) -> None:
        """Fold one response's reported usage into the running totals.

        Read through `getattr` with a zero default because usage is telemetry:
        a response that carries no `usage` block must not take a reconciliation
        run down. Cache tokens are input tokens and are counted as such -- this
        client sets no `cache_control`, so in practice they are always zero, but
        counting them keeps the total honest if that ever changes.
        """
        usage = getattr(message, "usage", None)
        if usage is None:
            return
        for name in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            self.input_tokens += int(getattr(usage, name, 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)

    def call(self, prompt: str, schema: dict) -> Any:
        message = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            tools=[
                {
                    "name": TOOL_NAME,
                    "description": TOOL_DESCRIPTION,
                    "input_schema": schema,
                }
            ],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=[{"role": "user", "content": prompt}],
        )
        self._record_usage(message)
        for block in getattr(message, "content", []) or []:
            if getattr(block, "type", None) != "tool_use":
                continue
            payload = getattr(block, "input", None)
            if isinstance(payload, dict):
                return payload.get("hypotheses")
        return None


def build_anthropic_client(api_key: str | None = None, **kwargs: Any) -> AnthropicAnalystClient:
    """Construct the real client. Called explicitly by the caller that has a
    key -- never at import time, and never from a test."""
    from anthropic import Anthropic  # lazy: keeps `core.llm` importable offline

    sdk = Anthropic() if api_key is None else Anthropic(api_key=api_key)
    return AnthropicAnalystClient(sdk, **kwargs)


# --- the second provider -------------------------------------------------------

#: The Gemini model this client points at unless told otherwise.
#:
#: A DEFAULT, not a claim that this id is current. Model ids move and this file
#: will outlive any list of them, so the id is stated here to be overridden with
#: one environment variable (`GEMINI_MODEL_ENV`) rather than a code change.
#:
#: It is the CHEAP one on purpose. A default that bills is a default that costs
#: a reader money for cloning the repository and running the demo once;
#: `gemini-3.7-flash` remains one environment variable away for anyone who wants
#: the stronger model and is willing to pay for it.
GEMINI_MODEL = "gemini-3.5-flash"

#: The variable the API layer reads to override that default.
#:
#: The NAME lives here so `GeminiCallFailed` can tell an operator which knob to
#: turn without the message being assembled two layers up. Nothing under `core/`
#: reads the environment; `api/settings.py` imports this name rather than
#: spelling it a second time.
GEMINI_MODEL_ENV = "RECON_GEMINI_MODEL"

#: Paid-tier list price in USD per million tokens, `(input, output)`, mirroring
#: `PRICING_USD_PER_MTOK` above so `Metrics.llm_cost_usd_per_100` still traces
#: to one reviewable number per model.
#:
#: **Source and date, because a rate a reader cannot verify is worse than an
#: absent one:** https://ai.google.dev/gemini-api/docs/pricing, read
#: 2026-08-29. The previously committed `(0.30, 2.50)` was carried over from an
#: older Flash tier and was wrong for every id here.
#:
#: * `gemini-3.5-flash` -- $1.50 in, $9.00 out. The published output rate is
#:   stated as inclusive of thinking tokens, which is exactly how
#:   `_record_usage` counts them.
#: * `gemini-3.7-flash` -- $0.75 in, $3.75 out as an INTRODUCTORY rate through
#:   2026-12-31; $1.50 / $7.50 from 2027-01-01. The introductory figures are the
#:   ones here because they are the ones a run today is billed at. See
#:   `GEMINI_PRICING_FROM_2027` for the successor rates, and change the table
#:   over rather than editing a number in place, so the diff says which régime a
#:   quoted cost came from.
#:
#: Reasoning tokens bill at the OUTPUT rate and are counted as output (see
#: `GeminiAnalystClient._record_usage`), so on a thinking model the second
#: number governs most of the spend -- which makes it the one most worth
#: getting right.
#:
#: A model absent from this table reports 0.0: an absent cost rather than an
#: invented one, while `tokens` still reports what was actually spent.
GEMINI_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.7-flash": (0.75, 3.75),
}

#: What `gemini-3.7-flash` costs once the introductory period above expires.
#: Recorded rather than applied: `core/` reads no clock, so nothing here may
#: decide which régime today falls in. Swapping it into the table on
#: 2027-01-01 is a one-line, reviewable change.
GEMINI_PRICING_FROM_2027: dict[str, tuple[float, float]] = {
    "gemini-3.7-flash": (1.50, 7.50),
}

#: How many times one analyst call may be attempted before it is a failure.
#:
#: Free-tier capacity is shared, and live testing on 2026-08-29 saw HTTP 503 on
#: two attempts out of three. A demo that dies on transient load is a demo about
#: Google's queue depth, not about this system -- but a retry that never gives
#: up is worse, because it turns an outage into a hang. Four attempts, then the
#: failure is raised with the count in the message.
GEMINI_MAX_ATTEMPTS = 4

#: Seconds before the first retry; each subsequent wait doubles it. Total idle
#: time across a fully exhausted call is 1 + 2 + 4 = 7 seconds.
#:
#: No jitter. `core/` owns no RNG and the caller is a single process, so the
#: thundering-herd problem jitter solves does not exist here -- and a fixed
#: schedule is one a test can assert on exactly.
GEMINI_BACKOFF_SECONDS = 1.0

#: The one status that is worth trying again. 429 is deliberately absent: a
#: rate-limit refusal is about a quota window this client cannot outwait in
#: seven seconds, and retrying it spends the remaining quota faster.
GEMINI_RETRY_STATUS = 503

#: How a 503 is recognised when the exception carries no numeric status. Some
#: SDK versions raise a typed error with `.code`; others raise something whose
#: text is all there is, so the message is the fallback and not the primary
#: test. Word-anchored on either the HTTP status or the gRPC status name; it is
#: a heuristic over free text and it is only reached when no status attribute
#: was present at all.
_UNAVAILABLE_RE = re.compile(r"\b503\b|\bUNAVAILABLE\b", re.IGNORECASE)


def _is_unavailable(exc: BaseException) -> bool:
    """Whether this failure is the shared-capacity 503 worth waiting out.

    A numeric status wins outright, in either of the two spellings the SDKs
    use, so an exception that says `code=404` is never retried because its text
    happens to mention a 503 elsewhere. Only when neither attribute is present
    does the message get read.
    """
    for name in ("code", "status_code"):
        status = getattr(exc, name, None)
        if isinstance(status, int):
            return status == GEMINI_RETRY_STATUS
    return bool(_UNAVAILABLE_RE.search(str(exc)))


class GeminiCallFailed(RuntimeError):
    """A Gemini call never produced a response at all.

    Deliberately distinct from a malformed response, which `analyse` drops on
    purpose. A wrong or unavailable model id must surface as a sentence naming
    the id that was tried and the variable that changes it -- not as an empty
    hypothesis list, which is indistinguishable from a model that weighed the
    evidence and correctly declined.

    `api/jobs.py` catches it, keeps the deterministic result, and puts the
    message in the run's `stage`, so the failure is legible without costing a
    run that had already succeeded.
    """


class GeminiAnalystClient:
    """The second implementation of `AnalystClient`: Gemini, via function calling.

    Deliberately the same shape as `AnthropicAnalystClient` -- an
    already-constructed SDK client in, the raw `hypotheses` list out, usage
    accumulated across calls -- because the whole point of having two is that
    the analyst, the verifier and the accept loop cannot tell them apart.
    Nothing here reads a key, builds a network client at import time, or stamps
    a clock.

    Structured output goes through `parameters_json_schema`, which takes raw
    JSON Schema, so the frozen `HYPOTHESIS_SCHEMA` is sent **verbatim** -- the
    same object the Anthropic path sends, union types and all. A translated
    copy would be a second spelling of the contract, free to drift from the
    first.
    """

    def __init__(
        self,
        client: Any,
        model: str = GEMINI_MODEL,
        max_output_tokens: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._model = model
        # Injected so the retry schedule is assertable without a test spending
        # seven seconds idling. `time.sleep` is not a clock reading -- nothing
        # here branches on what time it is -- so it does not breach `core/`'s
        # no-wall-clock rule.
        self._sleep = sleep
        # Left unset by default, unlike the Anthropic client's required
        # `max_tokens`. On a thinking model this budget covers reasoning as well
        # as the answer, and reasoning scales with how hard the problem is --
        # so a fixed cap borrowed from the other provider is a truncated
        # function call on exactly the hardest residue. An operator who wants a
        # ceiling can still pass one.
        self._max_output_tokens = max_output_tokens
        self.input_tokens = 0
        self.output_tokens = 0
        #: Reasoning tokens, a SUBSET of `output_tokens`. Broken out because how
        #: much of a reconciliation's spend was thinking is worth reporting.
        self.thoughts_tokens = 0
        self.total_tokens = 0

    @property
    def tokens(self) -> int:
        """Everything billed, as the API itself totalled it.

        The provider's own `total_token_count` rather than `input + output`
        computed here: Google is the authority on what it charged for, and a
        billed category this client does not enumerate would otherwise go
        unreported.
        """
        return self.total_tokens

    @property
    def cost_usd(self) -> float:
        """List price for what this client has spent so far.

        Priced off the input/output split rather than off `tokens`, because a
        total cannot be priced without knowing which side of it billed at which
        rate. See the pricing table's note: the rates are unverified.
        """
        rates = GEMINI_PRICING_USD_PER_MTOK.get(self._model)
        if rates is None:
            return 0.0
        per_input, per_output = rates
        return (
            self.input_tokens * per_input + self.output_tokens * per_output
        ) / 1_000_000

    def _record_usage(self, response: Any) -> None:
        """Fold one response's reported usage into the running totals.

        Read through `getattr` with a zero default because usage is telemetry:
        a response carrying no `usage_metadata` must not take a reconciliation
        run down, and non-thinking models omit `thoughts_token_count` outright
        rather than sending a zero.

        `cached_content_token_count` is deliberately NOT added. Gemini reports
        it as a SUBSET of `prompt_token_count` -- where Anthropic reports its
        cache tokens alongside `input_tokens` -- so counting it here would
        double-count the same tokens.
        """
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return

        def count(name: str) -> int:
            return int(getattr(usage, name, 0) or 0)

        prompt = count("prompt_token_count")
        candidates = count("candidates_token_count")
        thoughts = count("thoughts_token_count")

        self.input_tokens += prompt
        # Reasoning bills at the output rate, so it belongs in output rather
        # than in a fourth bucket no pricing table knows how to charge for.
        self.output_tokens += candidates + thoughts
        self.thoughts_tokens += thoughts
        # Prefer the reported total; fall back to its parts rather than report a
        # real spend as zero when the provider omits the field.
        self.total_tokens += count("total_token_count") or (
            prompt + candidates + thoughts
        )

    def call(self, prompt: str, schema: dict) -> Any:
        response = self._generate_with_retry(prompt, schema)
        self._record_usage(response)
        for function_call in getattr(response, "function_calls", None) or []:
            if getattr(function_call, "name", None) != TOOL_NAME:
                continue
            args = getattr(function_call, "args", None)
            if isinstance(args, dict):
                return args.get("hypotheses")
        return None

    def _generate_with_retry(self, prompt: str, schema: dict) -> Any:
        """One analyst call, retried only while the endpoint says 503.

        Free-tier capacity is shared; live testing saw 503 on two attempts out
        of three. Retrying that is the difference between a demo and an
        anecdote about someone else's queue.

        Two things it deliberately does not do. It does not retry anything
        else -- a wrong model id is wrong on every attempt, and waiting seven
        seconds before saying so delays the only message that would fix it. And
        it does not swallow the final failure into `None`: `analyse` would turn
        that into `[]`, which is indistinguishable from a model that weighed the
        evidence and correctly declined.
        """
        wait = GEMINI_BACKOFF_SECONDS
        for attempt in range(1, GEMINI_MAX_ATTEMPTS + 1):
            try:
                return self._client.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=self._config(schema),
                )
            except Exception as exc:  # noqa: BLE001 -- re-raised, with context
                retryable = _is_unavailable(exc)
                if retryable and attempt < GEMINI_MAX_ATTEMPTS:
                    self._sleep(wait)
                    wait *= 2
                    continue
                raise GeminiCallFailed(self._failure_text(exc, attempt)) from exc
        raise AssertionError("unreachable: the loop returns or raises")

    def _failure_text(self, exc: Exception, attempts: int) -> str:
        """The sentence an operator reads when a call never produced a response.

        It names the model id, the variable that changes it, and -- when the
        call was retried -- how many attempts were spent, so "it was slow and
        then it failed" is distinguishable from "it failed immediately".
        """
        tried = (
            f" after {attempts} attempts"
            if attempts > 1
            else ""
        )
        exhausted = (
            f" The endpoint stayed unavailable for all {GEMINI_MAX_ATTEMPTS} "
            f"attempts, which is shared free-tier capacity rather than a "
            f"configuration fault."
            if attempts >= GEMINI_MAX_ATTEMPTS
            else ""
        )
        return (
            f"the Gemini analyst call failed for model {self._model!r}{tried} "
            f"({type(exc).__name__}: {exc}). If that model id is wrong or "
            f"is not available on this key, override it with "
            f"{GEMINI_MODEL_ENV}.{exhausted}"
        )

    def _config(self, schema: dict) -> dict:
        """The request config, as a plain dict the SDK validates for us.

        `mode: ANY` with a single allowed name is Gemini's equivalent of
        Anthropic's forced `tool_choice`: the model answers through the tool or
        not at all, so there is never prose in between for `analyse` to guess
        at.
        """
        config: dict[str, Any] = {
            "tools": [
                {
                    "function_declarations": [
                        {
                            "name": TOOL_NAME,
                            "description": TOOL_DESCRIPTION,
                            "parameters_json_schema": schema,
                        }
                    ]
                }
            ],
            "tool_config": {
                "function_calling_config": {
                    "mode": "ANY",
                    "allowed_function_names": [TOOL_NAME],
                }
            },
        }
        if self._max_output_tokens is not None:
            config["max_output_tokens"] = self._max_output_tokens
        return config


def build_gemini_client(api_key: str | None = None, **kwargs: Any) -> GeminiAnalystClient:
    """Construct the real Gemini client. Called explicitly by the caller that
    has a key -- never at import time, and never from a test.

    The SDK import is lazy for the same reason `build_anthropic_client`'s is:
    `core/` has to stay importable on a machine with neither provider package
    and no credential, which is what lets the whole suite run offline. A
    module-level import here would break that and nothing but the boundary test
    would notice.
    """
    from google import genai  # lazy: keeps `core.llm` importable offline

    # With no key passed, the SDK reads GOOGLE_API_KEY or GEMINI_API_KEY from
    # the environment itself -- so the credential never enters a variable this
    # codebase could log, persist into a run's `stage`, or serialise by accident.
    sdk = genai.Client() if api_key is None else genai.Client(api_key=api_key)
    return GeminiAnalystClient(sdk, **kwargs)


def _to_hypothesis(raw: Any) -> Hypothesis | None:
    """Validate one raw entry, or drop it. Never repair it."""
    if not isinstance(raw, dict):
        return None
    try:
        return Hypothesis.model_validate(raw)
    except ValidationError:
        return None


def analyse(
    exceptions: Sequence[ReconException],
    context: AnalystContext,
    client: AnalystClient,
) -> list[Hypothesis]:
    """Propose resolutions for the unmatched residue.

    Batched: one call carrying every exception, because the candidate
    settlements are shared and two subjects competing for the same candidate is
    itself evidence.

    Returns `[]` for an empty residue, for a model that declines, and for any
    response that does not parse. None of those three is an error.
    """
    if not exceptions:
        return []

    prompt = render_prompt(exceptions, context)
    payload = client.call(prompt, HYPOTHESIS_SCHEMA)

    # A response of the wrong SHAPE is as malformed as one with the wrong
    # fields. Accepting only `list`/`tuple` is what excludes `str` and `dict`:
    # both are iterable, so a looser check would walk a bare string character by
    # character, or a dict key by key, and call each one an entry.
    if not isinstance(payload, (list, tuple)):
        return []

    return [h for h in (_to_hypothesis(raw) for raw in payload) if h is not None]
