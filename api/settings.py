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

#: --- connectors (Phase C) -----------------------------------------------------
#:
#: Every one of these is OPTIONAL, and unset is the default rather than a
#: failure: `core/connectors/*.available()` reports `False` and the console
#: shows the connector as off. A clone with none of them set must start, run
#: and pass its tests, which is why nothing here raises on absence.
#:
#: They are read HERE and nowhere under `core/`, on the same rule the analyst
#: credentials follow: `core/` takes an already-configured object and never an
#: environment. And no value below ever reaches an error message -- every
#: refusal in `core/connectors/` names the VARIABLE.
RAZORPAY_KEY_ID_ENV = "RECON_RAZORPAY_KEY_ID"
RAZORPAY_KEY_SECRET_ENV = "RECON_RAZORPAY_KEY_SECRET"

#: The mailbox the merchant's bank already sends statements to. `RECON_IMAP_
#: PASSWORD` is a secret: on Gmail it must be an App Password rather than the
#: account password, because this connects over plain IMAP and Google refuses
#: an account password there.
IMAP_HOST_ENV = "RECON_IMAP_HOST"
IMAP_PORT_ENV = "RECON_IMAP_PORT"
IMAP_USER_ENV = "RECON_IMAP_USER"
IMAP_PASSWORD_ENV = "RECON_IMAP_PASSWORD"
IMAP_SENDERS_ENV = "RECON_IMAP_SENDERS"
IMAP_FOLDER_ENV = "RECON_IMAP_FOLDER"

#: An optional regular expression over attachment FILENAMES. When set, only
#: matching attachments are fetched. `statement` is the obvious value.
#:
#: It exists because sender filtering is too coarse a control for a personal
#: mailbox: a bank mails a merchant's credit report from the same address it
#: mails their statements, and on 2026-09-02 that is exactly what arrived.
#: `core/connectors/imap_mailbox.CREDIT_REPORT_DENY` covers the bureaus
#: unconditionally; this is how a merchant narrows the rest.
IMAP_FILENAME_PATTERN_ENV = "RECON_IMAP_FILENAME_PATTERN"

#: The password Indian banks put on a statement PDF. A secret like any other.
PDF_PASSWORD_ENV = "RECON_PDF_PASSWORD"

#: A directory bank statements land in when no other route reaches them.
WATCH_DIR_ENV = "RECON_WATCH_DIR"

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


# --- connectors ---------------------------------------------------------------


def _optional(name: str) -> str | None:
    """An environment value, or `None` when it is absent or blank.

    Whitespace counts as unset. A variable set to a space is a
    misconfiguration, and failing the availability check is friendlier than
    failing on the first login attempt.
    """
    return os.environ.get(name, "").strip() or None


def razorpay_key_id() -> str | None:
    return _optional(RAZORPAY_KEY_ID_ENV)


def razorpay_key_secret() -> str | None:
    return _optional(RAZORPAY_KEY_SECRET_ENV)


def imap_port() -> int:
    """The mailbox port. 993 -- IMAP over TLS -- unless overridden.

    A non-numeric value is refused rather than silently defaulted: a deployment
    that meant to reach a non-standard port and typed it wrong must not come up
    quietly pointing somewhere else.
    """
    raw = _optional(IMAP_PORT_ENV)
    if raw is None:
        return 993
    try:
        return int(raw)
    except ValueError as exc:
        raise AuthMisconfigured(f"{IMAP_PORT_ENV} must be a whole number") from exc


def imap_settings() -> dict:
    """Everything `ImapMailboxConnector` needs, with nothing else in it.

    Returns the password because the connector has to log in with it -- there
    is no boolean form of "authenticate". The containment is the same one
    `secret_key()` uses: the value goes to exactly one consumer, and every
    error path in that consumer names the variable instead of the value.
    """
    return {
        "host": _optional(IMAP_HOST_ENV),
        "port": imap_port(),
        "username": _optional(IMAP_USER_ENV),
        "password": _optional(IMAP_PASSWORD_ENV),
        "sender_filter": _optional(IMAP_SENDERS_ENV) or "",
        "folder": _optional(IMAP_FOLDER_ENV) or "INBOX",
        "pdf_password": _optional(PDF_PASSWORD_ENV),
        "filename_pattern": _optional(IMAP_FILENAME_PATTERN_ENV),
    }


def watch_dir() -> Path | None:
    """The watched folder, or `None` when none is configured.

    `None` rather than a default under `out/`: a directory this API would poll
    on every sync is not something to create by accident, and "off" has to be
    expressible.
    """
    raw = _optional(WATCH_DIR_ENV)
    return Path(raw) if raw else None


def razorpay_http():
    """The transport `RazorpayConnector` calls, or `None` when unconfigured.

    Built here rather than inside the connector for the reason the whole
    package exists: `core/` must stay importable and testable offline, so the
    one function that opens a socket lives on this side of the boundary and is
    injected. `None` when there is no key pair, so an unconfigured deployment
    holds no live transport at all.
    """
    if not (razorpay_key_id() and razorpay_key_secret()):
        return None

    import urllib.error
    import urllib.request

    def call(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        """An HTTP status is a RESULT, not an exception.

        `urlopen` raises on 4xx and 5xx, and the connector's whole job on a 401
        is to turn it into a message naming the variable to fix. So the status
        comes back either way and the connector decides what it means.
        """
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.status, error.read()

    return call


def connectors() -> dict:
    """Every connector this deployment can offer, configured from the
    environment. Built per call, like everything else here, so a test that sets
    a variable gets a connector that sees it."""
    from core.connectors.registry import default_connectors

    return default_connectors(
        razorpay_key_id=razorpay_key_id(),
        razorpay_key_secret=razorpay_key_secret(),
        http=razorpay_http(),
        imap=imap_settings(),
        watch_dir=watch_dir(),
    )


def connector_secrets() -> tuple[str, ...]:
    """Every connector secret this process holds, for redaction and nothing else.

    The one consumer is `api/connectors.py`, which forwards a connector's own
    error text into a 502 body. That text is legible on purpose -- "Razorpay
    returned HTTP 500 for 2026-08" is worth reading -- but it originates
    upstream, and no upstream is trusted not to quote back something it was
    sent. So the values are scrubbed out of it on the way to the wire.

    This is defence in depth, not the primary control: every refusal in
    `core/connectors/` already names the VARIABLE and never the value, and the
    tests hold them to it. This is what catches the message nobody wrote.
    """
    return tuple(
        value
        for value in (
            razorpay_key_id(),
            razorpay_key_secret(),
            _optional(IMAP_PASSWORD_ENV),
            _optional(PDF_PASSWORD_ENV),
        )
        if value
    )
