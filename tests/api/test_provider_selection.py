"""Which analyst provider a deployment uses, and how it refuses to guess.

Two providers behind one Protocol is only useful if choosing between them is
predictable. The rule this file pins:

* an explicit `RECON_LLM_PROVIDER` always wins;
* with no explicit choice and exactly one credential present, that one is used;
* with no explicit choice and either **both** or **neither** credential present,
  resolution FAILS with a sentence naming the variable that would settle it.

The last clause is the one that matters. Silently preferring one provider when
two keys are present means a run's cost, latency and results depend on which
variable happened to be exported -- a difference nobody would think to look for.

No test here reads a real key or opens a socket; both are refused outright by
the fixtures below. `.env` support is tested against a temporary file, never the
developer's own.
"""

from __future__ import annotations

import os
import socket

import pytest

from api import jobs, settings

#: Every variable these tests read, set, or could pollute. `load_dotenv` writes
#: straight into `os.environ`, so this file snapshots and restores rather than
#: leaning on `monkeypatch.delenv`, which records nothing for a variable that
#: was not set to begin with and would let a `.env` value leak into later tests.
MANAGED = (
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "RECON_LLM_PROVIDER",
    "RECON_GEMINI_MODEL",
)

#: A value that must never appear in any error message this module can produce.
FAKE_KEY = "test-key-must-never-be-echoed-0123456789"


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    saved = {name: os.environ.get(name) for name in MANAGED}
    for name in MANAGED:
        os.environ.pop(name, None)

    def _refuse(*args, **kwargs):
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)

    yield

    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


# --- an explicit choice wins ---------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "key_var"),
    [
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("gemini", "GEMINI_API_KEY"),
        ("gemini", "GOOGLE_API_KEY"),
    ],
)
def test_an_explicit_provider_resolves_to_itself(monkeypatch, provider, key_var):
    """`GOOGLE_API_KEY` is accepted as an alias for the Gemini credential:
    both spellings are conventional for Google's SDKs, and a user who already
    has one exported should not have to rename it."""
    monkeypatch.setenv("RECON_LLM_PROVIDER", provider)
    monkeypatch.setenv(key_var, FAKE_KEY)
    assert settings.resolve_provider() == provider


def test_the_provider_name_is_read_case_and_whitespace_insensitively(monkeypatch):
    """A value pasted out of a shell or a `.env` file often carries both."""
    monkeypatch.setenv("RECON_LLM_PROVIDER", "  Gemini  ")
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    assert settings.resolve_provider() == "gemini"


def test_an_unknown_provider_name_fails_loudly(monkeypatch):
    monkeypatch.setenv("RECON_LLM_PROVIDER", "gemeni")
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)

    with pytest.raises(settings.ProviderNotResolved) as raised:
        settings.resolve_provider()

    message = str(raised.value)
    assert "RECON_LLM_PROVIDER" in message
    assert "gemeni" in message          # says what it was given
    assert "anthropic" in message and "gemini" in message  # and what is valid


def test_an_explicit_provider_with_no_credential_fails_loudly(monkeypatch):
    """Naming a provider you have no key for is a misconfiguration, and failing
    here is friendlier than failing on the first HTTP 401. It must NOT silently
    fall back to the provider that does have a key -- that would run and bill a
    model the operator did not ask for."""
    monkeypatch.setenv("RECON_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)

    with pytest.raises(settings.ProviderNotResolved) as raised:
        settings.resolve_provider()

    message = str(raised.value)
    assert "gemini" in message
    assert "GEMINI_API_KEY" in message


# --- no explicit choice --------------------------------------------------------


@pytest.mark.parametrize(
    ("key_var", "expected"),
    [
        ("ANTHROPIC_API_KEY", "anthropic"),
        ("GEMINI_API_KEY", "gemini"),
        ("GOOGLE_API_KEY", "gemini"),
    ],
)
def test_one_credential_and_no_explicit_choice_needs_no_variable(
    monkeypatch, key_var, expected
):
    """The common case: a user with one key should not have to set a second
    variable to say the obvious thing."""
    monkeypatch.setenv(key_var, FAKE_KEY)
    assert settings.resolve_provider() == expected


def test_two_credentials_and_no_explicit_choice_refuses_to_guess(monkeypatch):
    """Picking one silently makes a run's cost, latency and results depend on
    which variable happened to be exported -- a difference nobody would think
    to go looking for."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)

    with pytest.raises(settings.ProviderNotResolved) as raised:
        settings.resolve_provider()

    message = str(raised.value)
    assert "RECON_LLM_PROVIDER" in message
    assert "anthropic" in message and "gemini" in message


def test_no_credential_at_all_names_every_variable_that_would_help():
    """The path this repository actually runs on in CI. The message is what
    `api/jobs.py` puts into a run's terminal `stage`, so it is read by someone
    looking at a progress bar, not at a stack trace."""
    with pytest.raises(settings.ProviderNotResolved) as raised:
        settings.resolve_provider()

    message = str(raised.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "GEMINI_API_KEY" in message
    assert "GOOGLE_API_KEY" in message


@pytest.mark.parametrize(
    "env",
    [
        {"ANTHROPIC_API_KEY": FAKE_KEY, "GEMINI_API_KEY": FAKE_KEY},
        {"RECON_LLM_PROVIDER": "gemini", "ANTHROPIC_API_KEY": FAKE_KEY},
        {"RECON_LLM_PROVIDER": "nonsense", "GEMINI_API_KEY": FAKE_KEY},
    ],
)
def test_no_failure_message_ever_contains_a_key_value(monkeypatch, env):
    """This project's output gets recorded on video. An error may name the
    VARIABLE that is missing or wrong; it may never echo what is in it."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(settings.ProviderNotResolved) as raised:
        settings.resolve_provider()

    assert FAKE_KEY not in str(raised.value)


def test_a_whitespace_only_credential_counts_as_unset(monkeypatch):
    """A variable exported empty is a misconfiguration, not a credential."""
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    assert not settings.has_gemini_api_key()
    with pytest.raises(settings.ProviderNotResolved):
        settings.resolve_provider()


# --- the model id --------------------------------------------------------------


def test_the_default_model_is_used_when_the_variable_is_unset():
    from core.llm.analyst import GEMINI_MODEL

    assert settings.gemini_model() == GEMINI_MODEL


def test_the_model_id_is_overridable_without_a_code_change(monkeypatch):
    """The whole reason the default is a default. A model id that has moved on
    must be fixable by whoever is running the demo."""
    monkeypatch.setenv("RECON_GEMINI_MODEL", "gemini-something-else")
    assert settings.gemini_model() == "gemini-something-else"


# --- .env at the repo root -----------------------------------------------------


def test_a_missing_env_file_is_not_an_error(tmp_path):
    """`.env` is optional. CI and every deployment will have none, and every
    value has to resolve from the real environment alone."""
    assert settings.load_env_file(tmp_path / "nothing-here") is False
    with pytest.raises(settings.ProviderNotResolved):
        settings.resolve_provider()


def test_the_file_fills_a_variable_the_environment_does_not_set(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=from-the-file\n", encoding="utf-8")

    assert settings.load_env_file(env_file) is True
    assert settings.resolve_provider() == "gemini"


def test_a_real_environment_variable_wins_over_the_file(tmp_path):
    """The direction that fails safe. A value exported in the shell or set by a
    deployment platform must never be silently overridden by a stale file that
    happens to be sitting on disk."""
    env_file = tmp_path / ".env"
    env_file.write_text("RECON_GEMINI_MODEL=from-the-file\n", encoding="utf-8")
    os.environ["RECON_GEMINI_MODEL"] = "from-the-shell"

    settings.load_env_file(env_file)

    assert os.environ["RECON_GEMINI_MODEL"] == "from-the-shell"
    assert settings.gemini_model() == "from-the-shell"


def test_the_repo_root_env_file_is_the_one_that_is_loaded():
    """Named next to `api/`, not relative to whatever directory the server
    happened to be started from."""
    assert settings.ENV_FILE.name == ".env"
    assert (settings.ENV_FILE.parent / "pyproject.toml").exists()


# --- what the job actually constructs ------------------------------------------


def test_the_job_builds_a_gemini_client_when_gemini_is_selected(monkeypatch):
    """The wiring, with the SDK never reached: the socket guard above would
    fire if this constructed a real client."""
    import core.llm.analyst as analyst

    seen: dict = {}

    def fake_builder(**kwargs):
        seen.update(kwargs)
        return "the-gemini-client"

    monkeypatch.setattr(analyst, "build_gemini_client", fake_builder)
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)

    assert jobs.build_analyst_client() == "the-gemini-client"
    assert seen["model"] == settings.gemini_model()


def test_the_job_builds_an_anthropic_client_when_anthropic_is_selected(monkeypatch):
    """The pre-existing path, unchanged: no key is passed, because the SDK reads
    the variable itself and the credential never enters this codebase."""
    import core.llm.analyst as analyst

    seen: dict = {}

    def fake_builder(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return "the-anthropic-client"

    monkeypatch.setattr(analyst, "build_anthropic_client", fake_builder)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)

    assert jobs.build_analyst_client() == "the-anthropic-client"
    assert seen["args"] == () and seen["kwargs"] == {}


def test_an_unresolvable_provider_leaves_the_job_to_explain_itself():
    """`build_analyst_client` raises rather than returning None, so the caller
    cannot mistake "could not decide" for "decided on nothing"."""
    with pytest.raises(settings.ProviderNotResolved):
        jobs.build_analyst_client()
