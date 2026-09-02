"""Signed session cookies, an env-seeded user table, and the request principal.

**This is not a user-management product, and it is not trying to become one.**
There is no signup, no password reset, no invite flow, no roles, no SSO and no
`users` table in the database. Accounts are seeded from one environment
variable, and the deliberate consequence is that adding a user is a deployment
action rather than a feature. Everything a real product would need here --
lifecycle, delegated administration, MFA, an identity provider -- is named as
absent in `COMPLIANCE.md` rather than half-built here. What this module owes the
rest of the system is exactly one thing: a trustworthy answer to *which org is
this request*, because `core/store/repo.py` filters every query by it.

Three deliberate choices:

* **Stdlib `hmac`, no JWT library.** A session cookie here carries a subject, an
  org and an expiry -- three fields, all server-issued. JWT would add an
  algorithm-negotiation field whose famous failure mode (`alg: none`) is a
  vulnerability this design simply does not have, plus a dependency, to solve a
  problem (cross-service token exchange) this system does not have either.
  Signing is `HMAC-SHA256` over the payload with `RECON_SECRET_KEY`, compared in
  constant time.

* **Signed, not encrypted.** A holder can read their own org id out of their own
  cookie; that is not a leak, it is a fact they already know. What they must not
  be able to do is *change* it, which is what the signature prevents.

* **The clock is a parameter.** `issue_session` and `read_session` take `now`.
  `core/` may not read a wall clock and `api/` is where the boundary's clock
  lives (`api/jobs.utc_now`); passing it makes the expiry check testable without
  waiting twelve hours for one.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from core.store.schema import DEFAULT_ORG_ID

from api import settings
from api.settings import AuthMisconfigured

__all__ = [
    "AuthMisconfigured",
    "BadSession",
    "Principal",
    "SESSION_COOKIE",
    "SeededUser",
    "authenticate",
    "b64d",
    "b64e",
    "current_principal",
    "issue_session",
    "parse_users",
    "password_hash",
    "read_session",
    "require_principal",
    "router",
    "sign",
    "single_user_principal",
    "unsign",
    "user_table",
    "verify_password",
]

#: The cookie name. Prefixed so it cannot collide with anything the console
#: sets on the same origin during a same-origin deployment.
SESSION_COOKIE = "recon_session"

#: The subject recorded for requests served with auth switched off. It appears
#: in the access log, so "who read this" has an answer in single-user mode too
#: -- "the local operator" is a worse answer than a name, and a much better one
#: than a blank column.
SINGLE_USER_SUBJECT = "single-user"

#: The password verifier format written into `RECON_USERS`.
#: `pbkdf2_sha256$<rounds>$<salt-b64>$<hash-b64>`. The rounds travel inside the
#: value so they can be raised for new entries without invalidating old ones,
#: and so this constant is a default rather than a hidden global.
PBKDF2_SCHEME = "pbkdf2_sha256"
PBKDF2_ROUNDS = 240_000
PBKDF2_SALT_BYTES = 16

#: Field and record separators for `RECON_USERS`. `|` because it appears in
#: neither an email address nor base64; `,` between records for the same reason.
_FIELD_SEPARATOR = "|"
_RECORD_SEPARATOR = ","


class BadSession(ValueError):
    """A cookie that cannot be trusted: unsigned, forged, malformed or expired.

    One exception for all four on purpose. The route turns every one of them
    into the same 401 with the same body, because telling a caller *which* way
    their forgery failed is free help to the next attempt.
    """


@dataclass(frozen=True)
class Principal:
    """Who this request is, and -- the load-bearing part -- which org it reads.

    `org_id` is the only field `core/store/repo.py` cares about, and it can be
    reached in exactly two ways: the signed cookie, or the single-user default.
    There is no third path, and in particular no request body or query
    parameter can produce one (`tests/api/test_tenancy.py` asserts the routes
    module never even mentions the name).
    """

    subject: str
    org_id: str
    authenticated: bool


@dataclass(frozen=True)
class SeededUser:
    email: str
    org_id: str
    verifier: str


def single_user_principal() -> Principal:
    """The principal every request gets when `RECON_AUTH` is disabled."""
    return Principal(
        subject=SINGLE_USER_SUBJECT, org_id=DEFAULT_ORG_ID, authenticated=False
    )


# --- base64 without padding ---------------------------------------------------


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    """Decode unpadded urlsafe base64, raising `BadSession` on anything else.

    Every caller is decoding attacker-controlled cookie material, so the
    `binascii` error is translated here rather than at four call sites -- one
    of which would eventually forget and turn a hostile header into a 500.
    """
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise BadSession("not valid base64") from exc


# --- the signer ---------------------------------------------------------------


def sign(payload: bytes, key: bytes) -> str:
    """`<payload>.<hmac>`, both unpadded urlsafe base64."""
    digest = hmac.new(key, payload, hashlib.sha256).digest()
    return f"{b64e(payload)}.{b64e(digest)}"


def unsign(token: str, key: bytes) -> bytes:
    """The payload of `token`, or `BadSession`.

    `hmac.compare_digest` rather than `==`: the comparison is against a value
    an attacker controls and can retry, which is the textbook shape for a
    timing oracle. The cost of getting it right is one import.
    """
    if not isinstance(token, str) or token.count(".") != 1:
        raise BadSession("malformed session token")
    encoded, signature = token.split(".", 1)
    if not encoded or not signature:
        raise BadSession("malformed session token")
    payload = b64d(encoded)
    expected = hmac.new(key, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(b64d(signature), expected):
        raise BadSession("session signature does not verify")
    return payload


# --- the session --------------------------------------------------------------


def issue_session(
    subject: str, org_id: str, *, key: bytes, now: datetime, ttl_seconds: int
) -> str:
    """Mint a signed cookie value for this subject and org.

    The expiry is absolute and baked into the signed payload, so it cannot be
    extended by editing the cookie's own `Max-Age` in a browser.
    """
    issued = int(now.timestamp())
    payload = json.dumps(
        {"sub": subject, "org": org_id, "iat": issued, "exp": issued + int(ttl_seconds)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sign(payload, key)


def read_session(token: str, *, key: bytes, now: datetime) -> Principal:
    """Verify, decode and un-expire a cookie value into a `Principal`."""
    payload = unsign(token, key)
    try:
        claims = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BadSession("session payload is not JSON") from exc
    if not isinstance(claims, dict):
        raise BadSession("session payload is not an object")

    subject = claims.get("sub")
    org_id = claims.get("org")
    expires = claims.get("exp")
    if not isinstance(subject, str) or not isinstance(org_id, str):
        raise BadSession("session payload is missing a subject or an org")
    if not isinstance(expires, int) or isinstance(expires, bool):
        raise BadSession("session payload is missing an expiry")
    if int(now.timestamp()) > expires:
        raise BadSession("session has expired")
    return Principal(subject=subject, org_id=org_id, authenticated=True)


# --- passwords ----------------------------------------------------------------


def password_hash(
    password: str, *, salt: bytes | None = None, rounds: int = PBKDF2_ROUNDS
) -> str:
    """A verifier for `RECON_USERS`, in the encoded format described above.

    PBKDF2-HMAC-SHA256 from the stdlib rather than argon2 or bcrypt, and the
    reason is the same one that keeps `cryptography` at arm's length elsewhere:
    this is a seeded demo table of a handful of accounts, and a new hard
    dependency for it would buy a marginally better memory-hardness story at
    the cost of a build-time C extension. The scheme name is stored with the
    hash precisely so a future entry can say `argon2id` without a migration.
    """
    salt = os.urandom(PBKDF2_SALT_BYTES) if salt is None else salt
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"{PBKDF2_SCHEME}${rounds}${b64e(salt)}${b64e(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Whether `password` matches the verifier. **Never raises.**

    A malformed, empty or unknown-scheme verifier returns `False`. That is the
    safe direction: a typo in `RECON_USERS` locks an account, where an
    exception would take the login route down and any "be lenient" branch would
    hand the account to whoever asked.
    """
    try:
        scheme, rounds, salt, expected = encoded.split("$", 3)
    except (ValueError, AttributeError):
        return False
    if scheme != PBKDF2_SCHEME:
        return False
    try:
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), b64d(salt), int(rounds)
        )
        return hmac.compare_digest(candidate, b64d(expected))
    except (BadSession, ValueError):
        return False


# --- the seeded user table ----------------------------------------------------


def parse_users(raw: str) -> dict[str, SeededUser]:
    """`email|org_id|verifier` records, comma-separated, keyed by lowercased email.

    A malformed record is an error, never a skip: an entry silently dropped for
    a stray separator is an account that stops working with no message anywhere,
    at the exact moment somebody is trying to log in.

    The error names `RECON_USERS` and the record's *position*, never its
    contents -- the value holds password material and error messages end up in
    logs and screenshots.
    """
    table: dict[str, SeededUser] = {}
    records = [r.strip() for r in raw.split(_RECORD_SEPARATOR) if r.strip()]
    if not records:
        raise AuthMisconfigured(
            f"{settings.AUTH_ENV} is enabled but {settings.USERS_ENV} seeds no "
            "users: the format is "
            "email|org_id|verifier[,email|org_id|verifier...]"
        )
    for position, record in enumerate(records, start=1):
        fields = record.split(_FIELD_SEPARATOR)
        if len(fields) != 3 or not all(field.strip() for field in fields):
            raise AuthMisconfigured(
                f"{settings.USERS_ENV} record {position} is malformed: expected "
                "email|org_id|verifier with all three present"
            )
        email, org_id, verifier = (field.strip() for field in fields)
        key = email.lower()
        if key in table:
            raise AuthMisconfigured(
                f"{settings.USERS_ENV} record {position} repeats an address "
                "already seeded; one address maps to one org"
            )
        table[key] = SeededUser(email=email, org_id=org_id, verifier=verifier)
    return table


def user_table() -> dict[str, SeededUser]:
    """The parsed seeded users. Read from the environment on every call, in
    keeping with this API's settings convention -- there is no process-lifetime
    cache to invalidate when an operator rotates the variable and restarts."""
    return parse_users(settings.users_definition())


def lookup(table: dict[str, SeededUser], email: str) -> SeededUser | None:
    return table.get(email.strip().lower())


def authenticate(email: str, password: str) -> Principal | None:
    """The seeded user this pair identifies, or `None`.

    **One `None` for both failures.** The caller cannot tell "no such address"
    from "wrong password" because this function cannot tell it either -- and an
    unknown address still pays for a PBKDF2 round against a throwaway verifier,
    so the two paths do not differ in latency either. A login form that answers
    the two differently is an account-enumeration oracle, whether it does so in
    its wording or in its timing.
    """
    table = user_table()
    found = lookup(table, email)
    verifier = found.verifier if found is not None else _DUMMY_VERIFIER
    matched = verify_password(password, verifier)
    if found is None or not matched:
        return None
    return Principal(subject=found.email, org_id=found.org_id, authenticated=True)


#: A verifier for a password nobody has, hashed once at import so an unknown
#: address costs the same PBKDF2 work as a known one. See `authenticate`.
_DUMMY_VERIFIER = password_hash(base64.urlsafe_b64encode(os.urandom(32)).decode())


# --- the request principal ----------------------------------------------------

#: The 401 body, spelled once. It names what is missing (a session) and how to
#: get one (the login route), and names no user, no org and no key -- "no such
#: user" and "wrong password" are both facts an unauthenticated caller has not
#: earned.
UNAUTHENTICATED_DETAIL = (
    "authentication required: no valid session cookie. "
    "POST /api/auth/login to obtain one."
)


def current_principal(request: Request) -> Principal:
    """The dependency every route is mounted behind.

    With auth disabled this is a constant and touches neither the request nor
    the environment beyond the mode check -- which is what makes the disabled
    path byte-for-byte today's behaviour.
    """
    if not settings.auth_enabled():
        return single_user_principal()

    from api.jobs import utc_now

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail=UNAUTHENTICATED_DETAIL)
    try:
        return read_session(token, key=settings.secret_key(), now=utc_now())
    except BadSession as exc:
        # The reason is deliberately dropped rather than surfaced: forged,
        # expired and malformed all answer the same way.
        raise HTTPException(status_code=401, detail=UNAUTHENTICATED_DETAIL) from exc


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require_principal(principal: PrincipalDep) -> Principal:
    """`current_principal` under the name that says what mounting it does.

    Attached to the router itself rather than to each handler, so a route added
    tomorrow is behind auth by having been added at all.
    """
    return principal


#: The login/logout router, kept OUT of `api/routes.py` on purpose.
#:
#: `api/routes.py` carries every operation that reads or writes financial data,
#: and `tests/api/test_tenancy.py` asserts that the identifier `org_id` does
#: not occur anywhere in that module -- the org must arrive from the session
#: through `api/deps.get_repo` and from no other path. The login response
#: legitimately names an org (a client needs to label its own screen), so
#: putting these two handlers beside the data routes would either weaken that
#: check or force it to carve out an exception. They live here instead, with
#: the code that mints the session, and the data router keeps a rule with no
#: exceptions in it.
#:
#: It also carries no `require_principal` dependency, which is the other half:
#: these are the two operations that must work *without* a session.
router = APIRouter(tags=["auth"])


# --- the two routes -----------------------------------------------------------


class LoginRequest(BaseModel):
    """The whole of this product's identity input surface."""

    email: str
    password: str


@router.post("/api/auth/login", response_model=None, status_code=200)
def login(body: LoginRequest, response: Response) -> dict:
    """Exchange a seeded credential for a signed session cookie.

    **503 when `RECON_AUTH` is disabled**, which is the default. Not a 404: the
    route exists and the contract declares it; what is absent is the
    configuration, and the detail names the variable to set. Answering 404
    would tell an operator their deployment is missing the feature when it is
    only missing a line of environment.

    **401 for both failure modes, with one body.** A wrong password and an
    address nobody has seeded are the same response byte for byte -- see
    `api/auth.authenticate` for why that includes the latency, and
    `tests/api/test_auth.py` for the assertion that holds it there.
    """
    if not settings.auth_enabled():
        raise HTTPException(
            status_code=503,
            detail=(
                f"authentication is not enabled on this deployment: set "
                f"{settings.AUTH_ENV}=enabled (with {settings.SECRET_KEY_ENV} "
                f"and {settings.USERS_ENV}) to use sessions"
            ),
        )

    principal = authenticate(body.email, body.password)
    if principal is None:
        raise HTTPException(status_code=401, detail="invalid email or password")

    # Imported here rather than at module scope for the same reason
    # `current_principal` does it: `api/jobs.py` pulls in the whole engine, and
    # this module has to stay importable by anything that only wants to verify
    # a cookie.
    from api.jobs import utc_now

    response.set_cookie(
        SESSION_COOKIE,
        issue_session(
            principal.subject,
            principal.org_id,
            key=settings.secret_key(),
            now=utc_now(),
            ttl_seconds=settings.session_ttl_seconds(),
        ),
        max_age=settings.session_ttl_seconds(),
        httponly=True,
        # `lax` rather than `strict`: the console is a separate origin in dev
        # and `strict` would drop the cookie on a top-level navigation back
        # into it. `none` is never used -- it would require `secure` and would
        # permit exactly the cross-site request this setting exists to stop.
        samesite="lax",
        # Set only when the deployment is actually served over TLS: a `secure`
        # cookie is silently dropped over plain http, which on localhost would
        # present as a login that succeeds and then does nothing.
        secure=False,
        path="/",
    )
    return {"email": principal.subject, "org_id": principal.org_id}


@router.post("/api/auth/logout", status_code=204, response_model=None)
def logout(response: Response) -> Response:
    """Drop the session cookie. Idempotent, and needs no session of its own.

    Requiring authentication to log out would strand a client holding a cookie
    the server has already stopped trusting -- the exact state a user reaches
    by leaving a tab open overnight, and the one where the logout button has to
    work.

    The session is stateless, so "revocation" here is the browser dropping the
    cookie. What that does NOT do is invalidate a copy someone took off the
    wire or off the disk before it expired; COMPLIANCE.md names that limitation
    and the TTL that bounds it rather than implying a server-side revocation
    list this system does not have.
    """
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = 204
    return response

