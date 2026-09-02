"""Environment-driven configuration for the local-dev API.

Every VALUE here is read at call time rather than at import, so a test or a
shell can set the variable and get the new value without reimporting the
package. The one thing that happens at import is `load_env_file()`, which fills
variables the environment does not already define from an optional `.env` --
see that function for why it happens once, and at this boundary.

Defaults live under `out/`, which `.gitignore` already excludes -- so running
the API never leaves an untracked database or dataset in the working tree, and
no `.gitignore` edit (a file this lane does not own) is needed.

This module also decides **which analyst provider a deployment uses**. The
analyst is provider-agnostic by construction -- `core/llm/analyst.py` ships two
implementations of one Protocol and knows about neither an environment nor a
credential -- so choosing between them is configuration, and configuration
lives here.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

#: The Next.js dev origin. Lane E's page is served from here and fetches
#: http://localhost:8000, which is cross-origin the moment MSW is switched off.
DEFAULT_CORS_ORIGIN = "http://localhost:3000"

CORS_ORIGINS_ENV = "RECON_CORS_ORIGINS"
DB_PATH_ENV = "RECON_DB_PATH"
DATASETS_DIR_ENV = "RECON_DATASETS_DIR"
#: Where the content-addressed blob store keeps uploaded files. Under `out/`
#: like the database and the datasets, so running the API leaves nothing
#: untracked in the working tree.
UPLOADS_DIR_ENV = "RECON_UPLOADS_DIR"

#: The analyst layer's credentials. Read HERE and nowhere else: `core/` must
#: stay importable and testable offline, so nothing under it reads these
#: variables -- `core/llm/analyst.py` takes an already-constructed client
#: instead.
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
#: Google's SDKs conventionally accept either spelling, and the SDK itself reads
#: both. A user who already has one exported should not have to rename it.
GOOGLE_API_KEY_ENV = "GOOGLE_API_KEY"

#: Which of the two analyst implementations to use. Consulted first, and
#: decisive: see `resolve_provider`.
LLM_PROVIDER_ENV = "RECON_LLM_PROVIDER"

#: --- security (Phase 4, spec section 5) ---------------------------------------
#:
#: `disabled` (the default, and what an unset variable means) is **exactly
#: today's behaviour**: no login, no cookie, every request served as the single
#: local operator, every row filed under `DEFAULT_ORG_ID`. That default is not
#: laziness, it is the compatibility contract -- the console, the demo script
#: and every existing test run against an API with no auth configured, and none
#: of them may have to learn about one.
AUTH_ENV = "RECON_AUTH"
AUTH_MODES = ("disabled", "enabled")

#: The HMAC key session cookies are signed with. Read HERE and nowhere else,
#: and -- like the analyst credentials above -- its VALUE never leaves this
#: module in an error message: `AuthMisconfigured` names the variable.
SECRET_KEY_ENV = "RECON_SECRET_KEY"

#: The seeded user table, parsed by `api/auth.py`. See that module for the
#: format and for why this project has one at all.
USERS_ENV = "RECON_USERS"

#: How long a session cookie stays valid. A ceiling on the damage from a cookie
#: copied off a laptop, and the only reason it is configurable is that the
#: right answer differs between a demo and a deployment.
SESSION_TTL_ENV = "RECON_SESSION_TTL_SECONDS"
DEFAULT_SESSION_TTL_SECONDS = 12 * 60 * 60

#: The upload blob store's AES-GCM key, base64 (urlsafe or standard), decoding
#: to 16, 24 or 32 bytes. Absent means the store writes plaintext -- see
#: `core/store/blobstore.py` and COMPLIANCE.md.
BLOB_KEY_ENV = "RECON_BLOB_KEY"

#: The providers this build knows how to construct, in the order they are
#: reported. Adding a third means adding a client in `core/llm/analyst.py`, a
#: credential check below, and a branch in `api/jobs.build_analyst_client` --
#: and nothing in `core/llm/verifier.py`, which is the point of the seam.
PROVIDERS = ("anthropic", "gemini")

DEFAULT_DB_PATH = Path("out") / "recon.db"
DEFAULT_DATASETS_DIR = Path("out") / "datasets"
DEFAULT_UPLOADS_DIR = Path("out") / "uploads"

#: The optional `.env` beside `pyproject.toml`. Anchored to this file rather
#: than to the process's working directory, so `uvicorn` started from anywhere
#: reads the same one. Already listed in `.gitignore`; it must never be committed.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class AuthMisconfigured(RuntimeError):
    """Authentication was switched on and something it needs is missing.

    Raised rather than defaulted, for the same reason `ProviderNotResolved` is:
    every remaining case is one where guessing is worse than stopping. A
    per-process signing key generated on the fly would sign sessions that the
    next restart silently rejects, which presents as a login that "randomly"
    stops working; an empty user table would present as a password everybody
    gets wrong.

    **The message names the variable and never its value.** That is the same
    no-secrets-in-errors rule the analyst credentials already follow, and
    `tests/api/test_auth.py` holds this class to it.
    """


class ProviderNotResolved(RuntimeError):
    """Which analyst provider to use could not be decided.

    Raised rather than resolved to a default, because every remaining case is
    one where guessing is worse than stopping: no credential at all, a
    credential for both providers with nothing saying which is meant, or an
    explicit choice that has no key behind it. `api/jobs.py` catches this and
    puts the message in the run's terminal `stage`, so the run still completes
    on its deterministic result and the reason is legible on the progress bar.
    """


def load_env_file(path: Path | None = None) -> bool:
    """Fill variables the environment does not already define from `.env`.

    `override=False` is the whole design: a value exported in a shell or set by
    a deployment platform must never be silently replaced by a stale file
    sitting on disk. The file loses every conflict; it only fills gaps.

    Called ONCE, at this module's import, rather than per call site: repeating
    it would let a file re-supply a variable a test had just deleted, and
    scattering it would make "where did this value come from" unanswerable.
    Nothing under `core/` calls it -- loading configuration is the API layer's
    job, and `core/` has to stay importable with no configuration at all.

    A missing file is not an error; it returns `False`. A missing
    `python-dotenv` is not an error either -- it arrives as a transitive
    dependency, so this degrades to "environment only" rather than making the
    whole API unimportable if that ever changes.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover -- present transitively today
        return False
    return load_dotenv(path if path is not None else ENV_FILE, override=False)


_DOTENV_LOADED = load_env_file()


def cors_origins() -> list[str]:
    """The allowed browser origins, comma-separated in `RECON_CORS_ORIGINS`.

    Configurable by environment so a deployed origin can be added **without a
    code change**, and deliberately never `*`: a wildcard is refused loudly
    rather than accepted quietly, because "it worked when I tested it" is how a
    wildcard survives into a deployment.
    """
    raw = os.environ.get(CORS_ORIGINS_ENV, "")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if not origins:
        return [DEFAULT_CORS_ORIGIN]
    if "*" in origins:
        raise ValueError(
            f"{CORS_ORIGINS_ENV} may not contain a wildcard origin: name the "
            "origins explicitly, comma-separated "
            f"(default {DEFAULT_CORS_ORIGIN!r})."
        )
    return origins


def db_path() -> Path:
    return Path(os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH)


def datasets_dir() -> Path:
    return Path(os.environ.get(DATASETS_DIR_ENV) or DEFAULT_DATASETS_DIR)


def uploads_dir() -> Path:
    """The blob store's root. See `core/store/blobstore.py`.

    Read at call time like everything else here, so a test can point it at a
    temporary directory without reimporting the package -- which is what keeps
    the upload tests from writing into a developer's own `out/`.
    """
    return Path(os.environ.get(UPLOADS_DIR_ENV) or DEFAULT_UPLOADS_DIR)


def has_anthropic_api_key() -> bool:
    """Whether this process can make a live analyst call.

    Returns a BOOLEAN, never the key. The value is needed for exactly one
    decision -- construct the real client or do not -- and the SDK reads the
    variable itself, so the secret never has to pass through this codebase and
    can never be logged, persisted in a run's `stage`, or serialised into a
    response by accident. A variable set to whitespace counts as unset: that is
    a misconfiguration, and failing at the check is friendlier than failing on
    the first HTTP 401.
    """
    return bool(os.environ.get(ANTHROPIC_API_KEY_ENV, "").strip())


def has_gemini_api_key() -> bool:
    """Whether a Gemini credential is present, under either conventional name.

    A BOOLEAN, never the key -- for the same reasons as above, and one more:
    this project's output is recorded, so no code path here may return a value
    that could reach a log, a run's `stage`, or an error message.
    """
    return any(
        bool(os.environ.get(name, "").strip())
        for name in (GEMINI_API_KEY_ENV, GOOGLE_API_KEY_ENV)
    )


def _has_credential(provider: str) -> bool:
    return has_gemini_api_key() if provider == "gemini" else has_anthropic_api_key()


def _credential_hint(provider: str) -> str:
    """How to name the missing variable, without ever naming its value."""
    if provider == "gemini":
        return f"{GEMINI_API_KEY_ENV} (or {GOOGLE_API_KEY_ENV})"
    return ANTHROPIC_API_KEY_ENV


def resolve_provider() -> str:
    """Which analyst provider this process should use.

    The order is: an explicit `RECON_LLM_PROVIDER` wins outright; failing that,
    exactly one credential decides it; anything else raises.

    An explicit choice with no key behind it is an error rather than a fallback
    to the provider that *does* have one -- falling back would run and bill a
    model the operator did not ask for, and the resulting cost and accuracy
    figures would be attributed to the wrong provider.

    Both keys present with no explicit choice is likewise an error. Preferring
    one silently would make a run's cost, latency and results depend on which
    variable happened to be exported, which is a difference nobody would think
    to go looking for.
    """
    requested = os.environ.get(LLM_PROVIDER_ENV, "").strip().lower()
    if requested:
        if requested not in PROVIDERS:
            raise ProviderNotResolved(
                f"{LLM_PROVIDER_ENV}={requested!r} is not a provider this build "
                f"knows about: set it to one of {', '.join(PROVIDERS)}"
            )
        if not _has_credential(requested):
            raise ProviderNotResolved(
                f"{LLM_PROVIDER_ENV} asks for {requested!r}, but "
                f"{_credential_hint(requested)} is not set"
            )
        return requested

    available = [name for name in PROVIDERS if _has_credential(name)]
    if len(available) == 1:
        return available[0]
    if not available:
        raise ProviderNotResolved(
            "no analyst credential is set: set "
            f"{_credential_hint('anthropic')} or {_credential_hint('gemini')}, "
            f"and {LLM_PROVIDER_ENV} if you set both"
        )
    raise ProviderNotResolved(
        f"credentials are set for more than one provider "
        f"({', '.join(available)}), so which to use cannot be inferred: set "
        f"{LLM_PROVIDER_ENV} to the one you mean"
    )


def gemini_model() -> str:
    """The Gemini model id, overridable by `RECON_GEMINI_MODEL`.

    The default lives in `core/llm/analyst.py` beside the client that uses it,
    and is imported lazily: `api/` must stay importable in a tree where the
    analyst layer was never built (`jobs.analyst_layer_available`), so this
    module cannot depend on `core.llm` at import time.
    """
    from core.llm.analyst import GEMINI_MODEL, GEMINI_MODEL_ENV

    return os.environ.get(GEMINI_MODEL_ENV, "").strip() or GEMINI_MODEL


# --- security -----------------------------------------------------------------


def auth_mode() -> str:
    """`disabled` (default) or `enabled`, from `RECON_AUTH`.

    An unrecognised value is refused rather than treated as "not enabled": a
    deployment that meant to switch auth on and typed `RECON_AUTH=true` must
    not come up wide open because the string did not match.
    """
    mode = os.environ.get(AUTH_ENV, "").strip().lower() or "disabled"
    if mode not in AUTH_MODES:
        raise AuthMisconfigured(
            f"{AUTH_ENV}={mode!r} is not a mode this build knows about: set it "
            f"to one of {', '.join(AUTH_MODES)} (default disabled)"
        )
    return mode


def auth_enabled() -> bool:
    return auth_mode() == "enabled"


def secret_key() -> bytes:
    """The session signing key, as bytes. **Never returned to a caller that
    is going to render it** -- the only consumer is `api/auth.py`'s HMAC.

    Unlike `has_anthropic_api_key`, this one cannot be reduced to a boolean:
    signing needs the material. So the containment is different -- the value
    is returned to exactly one module, and every error path here names the
    variable instead of the value.
    """
    raw = os.environ.get(SECRET_KEY_ENV, "").strip()
    if not raw:
        raise AuthMisconfigured(
            f"{AUTH_ENV} is enabled but {SECRET_KEY_ENV} is not set: sessions "
            "cannot be signed. Generate one with "
            "`python -c \"import secrets;print(secrets.token_urlsafe(48))\"` "
            "and set it in the environment (never in a committed file)."
        )
    return raw.encode("utf-8")


def session_ttl_seconds() -> int:
    """How long an issued session stays valid. A positive integer."""
    raw = os.environ.get(SESSION_TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_SESSION_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError as exc:
        raise AuthMisconfigured(
            f"{SESSION_TTL_ENV} must be a whole number of seconds"
        ) from exc
    if ttl <= 0:
        raise AuthMisconfigured(f"{SESSION_TTL_ENV} must be greater than zero")
    return ttl


def users_definition() -> str:
    """The RAW `RECON_USERS` value, for `api/auth.py` to parse and nobody else.

    Deliberately not exported through anything that renders: the string holds
    password verifiers, and the parsed table -- which is what every other
    caller wants -- is `api/auth.user_table()`.
    """
    return os.environ.get(USERS_ENV, "")


def blob_key() -> bytes | None:
    """The upload encryption key, or `None` when none is configured.

    `None` is a supported state, not a failure: the blob store's plaintext mode
    is what a local demo runs on, and COMPLIANCE.md says so in as many words.
    A key that is set but undecodable IS a failure -- silently falling back to
    plaintext because the base64 had a typo is precisely the outcome an
    encryption-at-rest control exists to prevent.
    """
    raw = os.environ.get(BLOB_KEY_ENV, "").strip()
    if not raw:
        return None
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError) as exc:
        raise AuthMisconfigured(
            f"{BLOB_KEY_ENV} is set but is not valid base64"
        ) from exc
    if len(key) not in (16, 24, 32):
        raise AuthMisconfigured(
            f"{BLOB_KEY_ENV} decodes to {len(key)} bytes; AES-GCM needs 16, 24 "
            "or 32. Generate one with `python -c "
            '"import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`'
        )
    return key
