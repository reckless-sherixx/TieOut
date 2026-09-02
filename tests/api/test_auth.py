"""Session cookies, the env-seeded user table, and the 401s (Phase 4 item 1).

The unit half of the auth work. The *surface* half -- "with `RECON_AUTH`
unset the whole existing API behaves exactly as it did" -- lives in
`tests/api/test_tenancy.py`, because it is parametrised over the same route
table the cross-org isolation proof walks and the table must have one owner.

Two properties are load-bearing here and are asserted rather than described:

* **A wrong password and an unknown address produce the identical response.**
  Byte-for-byte: same status, same body. A login form that answers "no such
  user" for one and "wrong password" for the other is a user-enumeration
  oracle, and the fastest way for that to regress is for someone to make the
  message "more helpful".
* **No secret value can reach a response or an exception message.** The
  project already holds the analyst credentials to that rule
  (`api/settings.has_anthropic_api_key` returns a bool, never the key); this
  extends it to `RECON_SECRET_KEY` and the password material.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api import auth, settings

# A secret with a distinctive shape, so a substring search for it in a
# response body cannot pass by accident.
SECRET = "s3cr3t-signing-key-do-not-echo-a1b2c3"
PASSWORD_A = "correct horse battery staple"
PASSWORD_B = "hunter2-but-for-globex"


def seed_users() -> str:
    """`RECON_USERS`, built with the same helper an operator would use.

    The hashes are minted here rather than pasted in as literals: a literal
    would freeze the KDF parameters into the test file, and the point of the
    encoded prefix is that they can be raised without a migration.
    """
    return ",".join(
        (
            f"alice@acme.test|org-acme|{auth.password_hash(PASSWORD_A)}",
            f"bob@globex.test|org-globex|{auth.password_hash(PASSWORD_B)}",
        )
    )


@pytest.fixture
def enabled_client(tmp_path, monkeypatch):
    """A client on an isolated database with `RECON_AUTH=enabled`."""
    monkeypatch.setenv("RECON_DB_PATH", str(tmp_path / "recon.db"))
    monkeypatch.setenv("RECON_DATASETS_DIR", str(tmp_path / "datasets"))
    monkeypatch.setenv(settings.AUTH_ENV, "enabled")
    monkeypatch.setenv(settings.SECRET_KEY_ENV, SECRET)
    monkeypatch.setenv(settings.USERS_ENV, seed_users())
    from api.main import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def disabled_client(tmp_path, monkeypatch):
    """The default configuration -- every auth variable absent."""
    monkeypatch.setenv("RECON_DB_PATH", str(tmp_path / "recon.db"))
    monkeypatch.setenv("RECON_DATASETS_DIR", str(tmp_path / "datasets"))
    for name in (settings.AUTH_ENV, settings.SECRET_KEY_ENV, settings.USERS_ENV):
        monkeypatch.delenv(name, raising=False)
    from api.main import create_app

    with TestClient(create_app()) as client:
        yield client


# --- the signer ---------------------------------------------------------------


def test_a_token_round_trips_through_the_signer():
    key = SECRET.encode()
    token = auth.sign(b'{"sub":"alice"}', key)
    assert auth.unsign(token, key) == b'{"sub":"alice"}'


def test_a_token_signed_with_another_key_is_refused():
    """The whole point of the signature: the payload is public, the key is not."""
    token = auth.sign(b'{"sub":"alice"}', b"one-key")
    with pytest.raises(auth.BadSession):
        auth.unsign(token, b"another-key")


def test_a_tampered_payload_is_refused_even_though_it_is_readable():
    """The cookie is signed, not encrypted -- so tampering is what must fail.

    A client can decode the payload and read its own org id; that is fine and
    deliberate. What it must not be able to do is edit the org id and have the
    server believe it, which is exactly the swap this performs.
    """
    key = SECRET.encode()
    token = auth.sign(json.dumps({"org": "org-acme"}).encode(), key)
    payload, signature = token.split(".", 1)
    forged = auth.b64e(json.dumps({"org": "org-globex"}).encode()) + "." + signature
    with pytest.raises(auth.BadSession):
        auth.unsign(forged, key)


@pytest.mark.parametrize(
    "token", ["", "no-dot", "a.b.c.d", "!!!.???", "eyJhIjoxfQ."]
)
def test_a_malformed_token_raises_the_session_error_and_never_a_decode_error(token):
    """Garbage in the cookie jar is a 401, not a 500.

    The cookie is attacker-controlled input on every request; a `binascii`
    error escaping the signer would be a crash on a hostile header.
    """
    with pytest.raises(auth.BadSession):
        auth.unsign(token, SECRET.encode())


# --- the session --------------------------------------------------------------


def test_a_session_carries_its_subject_and_org_and_comes_back_whole():
    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    token = auth.issue_session(
        "alice@acme.test", "org-acme", key=SECRET.encode(), now=now, ttl_seconds=3600
    )
    principal = auth.read_session(token, key=SECRET.encode(), now=now)
    assert principal.subject == "alice@acme.test"
    assert principal.org_id == "org-acme"
    assert principal.authenticated is True


def test_a_session_past_its_expiry_is_refused():
    """Expiry is checked against a clock the caller hands in.

    `api/` owns the clock, so the check takes `now` as an argument -- which is
    also what makes this testable without sleeping for twelve hours.
    """
    issued = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    token = auth.issue_session(
        "alice@acme.test", "org-acme", key=SECRET.encode(), now=issued, ttl_seconds=60
    )
    with pytest.raises(auth.BadSession):
        auth.read_session(
            token, key=SECRET.encode(), now=issued + timedelta(seconds=61)
        )


# --- the password hashes ------------------------------------------------------


def test_two_hashes_of_one_password_differ_and_both_verify():
    """Per-entry salt. Two users who chose the same password must not be
    visibly identical in the environment listing."""
    first = auth.password_hash(PASSWORD_A)
    second = auth.password_hash(PASSWORD_A)
    assert first != second
    assert auth.verify_password(PASSWORD_A, first)
    assert auth.verify_password(PASSWORD_A, second)


def test_a_wrong_password_does_not_verify():
    assert not auth.verify_password(PASSWORD_B, auth.password_hash(PASSWORD_A))


@pytest.mark.parametrize(
    "encoded", ["", "not-a-hash", "pbkdf2_sha256$abc", "scrypt$1$aa$bb"]
)
def test_a_malformed_or_unknown_hash_never_verifies_and_never_raises(encoded):
    """A typo in `RECON_USERS` must lock the account, not crash the login route
    and not -- far worse -- fall through to an accepting branch."""
    assert not auth.verify_password(PASSWORD_A, encoded)


# --- the user table -----------------------------------------------------------


def test_the_user_table_parses_two_users_into_two_orgs():
    table = auth.parse_users(seed_users())
    assert set(table) == {"alice@acme.test", "bob@globex.test"}
    assert table["alice@acme.test"].org_id == "org-acme"
    assert table["bob@globex.test"].org_id == "org-globex"


def test_addresses_are_matched_case_insensitively():
    """Nobody types their own address with consistent capitalisation, and an
    email address's local part being case-sensitive in the RFC has never once
    been what a user meant."""
    table = auth.parse_users(seed_users())
    assert auth.lookup(table, "Alice@ACME.test") is not None


@pytest.mark.parametrize(
    "raw", ["alice@acme.test", "alice@acme.test|org-acme", "|org-acme|hash"]
)
def test_a_malformed_user_entry_is_refused_loudly_at_parse_time(raw):
    """Refused, never skipped. A silently dropped entry is an account that
    stops working with no message anywhere."""
    with pytest.raises(auth.AuthMisconfigured):
        auth.parse_users(raw)


# --- the routes ---------------------------------------------------------------


def test_login_sets_a_session_cookie_and_names_the_org(enabled_client):
    response = enabled_client.post(
        "/api/auth/login",
        json={"email": "alice@acme.test", "password": PASSWORD_A},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"email": "alice@acme.test", "org_id": "org-acme"}
    assert auth.SESSION_COOKIE in response.cookies


def test_the_session_cookie_is_httponly_and_samesite(enabled_client):
    """The two flags that decide whether an XSS or a cross-site form can use
    the session. Asserted on the header rather than the parsed jar, because
    that is where the flags live."""
    response = enabled_client.post(
        "/api/auth/login",
        json={"email": "alice@acme.test", "password": PASSWORD_A},
    )
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/" in header


def test_a_wrong_password_and_an_unknown_address_are_indistinguishable(
    enabled_client,
):
    """The user-enumeration guard, stated as an equality rather than as prose."""
    wrong = enabled_client.post(
        "/api/auth/login",
        json={"email": "alice@acme.test", "password": "not-her-password"},
    )
    unknown = enabled_client.post(
        "/api/auth/login",
        json={"email": "nobody@nowhere.test", "password": PASSWORD_A},
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert auth.SESSION_COOKIE not in wrong.cookies


def test_a_failed_login_names_no_user_and_no_org(enabled_client):
    """Not even indirectly: the detail must not echo back what was submitted."""
    body = enabled_client.post(
        "/api/auth/login",
        json={"email": "alice@acme.test", "password": "not-her-password"},
    ).text
    for leak in ("alice", "acme", "org-acme", "not-her-password"):
        assert leak not in body


def test_an_unauthenticated_read_is_a_401_that_names_the_missing_thing(
    enabled_client,
):
    response = enabled_client.get("/api/runs")
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert "session" in detail.lower()
    # ... and says how to get one, without naming a user or a key.
    assert "/api/auth/login" in detail


def test_a_tampered_cookie_is_a_401_and_not_a_500(enabled_client):
    enabled_client.post(
        "/api/auth/login",
        json={"email": "alice@acme.test", "password": PASSWORD_A},
    )
    enabled_client.cookies.set(auth.SESSION_COOKIE, "eyJvcmciOiJvcmctZ2xvYmV4In0.xx")
    assert enabled_client.get("/api/runs").status_code == 401


def test_logout_clears_the_cookie_and_the_next_read_is_refused(enabled_client):
    enabled_client.post(
        "/api/auth/login",
        json={"email": "alice@acme.test", "password": PASSWORD_A},
    )
    assert enabled_client.get("/api/runs").status_code == 200
    assert enabled_client.post("/api/auth/logout").status_code == 204
    assert enabled_client.get("/api/runs").status_code == 401


def test_logout_is_idempotent_and_needs_no_session(enabled_client):
    """Logging out of a session you do not have is not an error; a 401 here
    would strand a client holding a cookie the server has stopped trusting."""
    assert enabled_client.post("/api/auth/logout").status_code == 204


def test_login_is_refused_when_auth_is_disabled(disabled_client):
    """`disabled_client` is the default configuration: `RECON_AUTH` unset.

    503 rather than 404: the route exists and the contract declares it; what is
    missing is the configuration, and the detail says which variable.
    """
    response = disabled_client.post(
        "/api/auth/login", json={"email": "alice@acme.test", "password": PASSWORD_A}
    )
    assert response.status_code == 503
    assert settings.AUTH_ENV in response.json()["detail"]


# --- the secrets posture ------------------------------------------------------


def test_no_secret_value_reaches_a_response_body(enabled_client):
    """The rule `api/settings.py` already applies to the analyst credentials,
    extended to the session key and the passwords, and enforced over every
    response an unauthenticated or badly-authenticated caller can provoke."""
    enabled_client.cookies.set(auth.SESSION_COOKIE, "garbage.garbage")
    bodies = [
        enabled_client.get("/api/runs").text,
        enabled_client.get("/api/runs/nope").text,
        enabled_client.post(
            "/api/auth/login", json={"email": "alice@acme.test", "password": "x"}
        ).text,
        enabled_client.post("/api/auth/login", json={"email": "a", "password": "b"}).text,
    ]
    for body in bodies:
        for secret in (SECRET, PASSWORD_A, PASSWORD_B):
            assert secret not in body


def test_a_missing_secret_key_fails_by_naming_the_variable_never_a_value(
    tmp_path, monkeypatch
):
    """Auth enabled with no key is a misconfiguration, and it stops rather than
    quietly generating a per-process key -- a generated key would sign sessions
    that every restart silently invalidates, which reads as a flaky login."""
    monkeypatch.setenv(settings.AUTH_ENV, "enabled")
    monkeypatch.delenv(settings.SECRET_KEY_ENV, raising=False)
    with pytest.raises(settings.AuthMisconfigured) as raised:
        settings.secret_key()
    assert settings.SECRET_KEY_ENV in str(raised.value)


def test_a_malformed_user_table_is_reported_without_echoing_the_table(
    tmp_path, monkeypatch
):
    """The variable holds password material. When it is wrong the operator
    needs to know *which variable*, and needs the message not to paste its
    contents into a log they are about to send to somebody."""
    monkeypatch.setenv(settings.AUTH_ENV, "enabled")
    monkeypatch.setenv(settings.SECRET_KEY_ENV, SECRET)
    monkeypatch.setenv(settings.USERS_ENV, f"alice@acme.test|{PASSWORD_A}")
    with pytest.raises(settings.AuthMisconfigured) as raised:
        auth.user_table()
    message = str(raised.value)
    assert settings.USERS_ENV in message
    assert PASSWORD_A not in message
