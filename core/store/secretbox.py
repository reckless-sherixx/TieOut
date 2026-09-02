"""AES-256-GCM for a stored credential, bound to the row it belongs to.

`core/store/blobstore.py` already encrypts a merchant's FILES with this key
and this cipher, and everything below follows it deliberately rather than
inventing a second scheme: the same `RECON_BLOB_KEY`, the same 96-bit nonce
per value, the same lazy import of `cryptography`, the same refusal to name
the key in a repr or a message. Read that module first; this one is its
sibling for values too small to be blobs.

**Three differences, and each is the reason this file exists.**

**1. There is no plaintext mode.** A blob store with no key writes plaintext
and says so in a byte on disk, because a local demo has to run. A credential
vault may not: `RECON_BLOB_KEY` unset means `POST /api/connections` is
REFUSED (spec 2026-09-02 section 3.3, rule 1), so this class cannot be
constructed without a key and there is no envelope shape that means "not
encrypted". "We would encrypt if configured" and "we encrypt" are different
sentences to put in front of a merchant, and a mode flag on disk is how the
first one quietly becomes the second.

**2. What is bound in as additional authenticated data is
`f"{connection_id}:{field_name}"`.** A blob has an address and the address is
what gets bound; a credential is a COLUMN OF A ROW and has no address, so the
row and the column are what identify it. Without that binding, anybody with
write access to the database could:

* copy one connection's `secret_ciphertext` into another connection's row --
  including another ORG's -- and the fetcher would then log in to the first
  merchant's mailbox on the second merchant's schedule and file the statements
  under the second merchant's org. Confidentiality is intact and the tenancy
  boundary is gone;
* swap `pdf_secret_ciphertext` into `secret_ciphertext` in the same row, so
  the PDF password is presented to the mail server. On Gmail that is a failed
  login and an account-security email; the point is that the store cannot tell
  the two apart and must not be asked to.

Both are stopped by the AAD, both are tested in `tests/store/test_secretbox.py`,
and the string is spelled once in `secret_aad` so sealing and opening cannot
drift.

**3. It works on `str`, not `bytes`.** A password is text the merchant typed;
UTF-8 is applied here so no caller has to remember to, and `unseal` returns
the string rather than bytes that some later `repr` would render as `b'...'`.

**The key is never rendered.** Not in `__repr__`, not in an exception, not in
a path. `SecretRefused` collapses every failure -- wrong key, wrong row, wrong
column, tampered bytes, junk -- into one message for the reason `CorruptBlob`
does: no caller can act on the distinction and an attacker probing the
database can.

No clock is read here. `core/` may not read one, and nothing in this module
has any use for a timestamp.
"""

from __future__ import annotations

import os

__all__ = [
    "MAGIC",
    "NONCE_BYTES",
    "SecretBox",
    "SecretRefused",
    "SecretsUnavailable",
    "secret_aad",
]

#: Envelope magic, distinct from the blob store's so a value from one can never
#: be read by the other even if a future refactor points a caller at the wrong
#: helper. Present so "what is this column" is answerable without the key.
MAGIC = b"RCNSEC1"

#: AES-GCM's standard nonce length. 96 bits is the size the mode is specified
#: and optimised for; anything else costs an extra GHASH pass and buys nothing.
NONCE_BYTES = 12


class SecretsUnavailable(RuntimeError):
    """A key was supplied but `cryptography` is not importable.

    Raised rather than degraded, exactly as `blobstore.EncryptionUnavailable`
    is. A vault that was asked to encrypt and silently did not would make
    every statement this system makes about the credential false, with nothing
    anywhere reporting it.
    """


class SecretRefused(ValueError):
    """A sealed value would not open: bad envelope, bad key, or wrong binding.

    One exception for all of them, and the message quotes neither the key nor
    the ciphertext. A caller can act on "this credential is not trustworthy";
    no caller can act on *which* of the four it was, and an attacker with read
    access to the database can.
    """


def secret_aad(connection_id: str, field: str) -> bytes:
    """The additional authenticated data binding a value to its row and column.

    Spelled once because `seal` and `unseal` must agree exactly: a mismatch
    would not be a wrong answer, it would be every stored credential becoming
    unreadable at the next deployment, which is a silent outage of the
    fetcher.

    Neither part is secret -- `connection_id` is in the URL and `field` is in
    this file -- and neither needs to be. AAD is authenticated, not encrypted:
    what it buys is that the tag stops verifying when a ciphertext is moved.
    """
    return f"{connection_id}:{field}".encode("utf-8")


class SecretBox:
    """Seals and opens one deployment's stored credentials.

    One instance per key. The key is supplied by the caller -- `core/` reads no
    environment -- so `api/settings.blob_key()` is where it comes from in this
    deployment, and `api/connections.py` is what refuses when there is none.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) not in (16, 24, 32):
            # Length only. The value is never named, here or anywhere.
            raise ValueError(
                f"an AES key must be 16, 24 or 32 bytes; got {len(key)}"
            )
        self._aesgcm = _aesgcm(key)

    def __repr__(self) -> str:
        """Says that a key is held and never what it is.

        A dataclass-style repr carrying the key would put it into every
        traceback, every debugger frame and every `print` a future developer
        reaches for -- the leak the env-only secrets rule exists to prevent,
        arriving by a route nobody audits.
        """
        return "SecretBox(configured=True)"

    def seal(self, value: str, *, connection_id: str, field: str) -> bytes:
        """`value`, encrypted and bound to `connection_id` and `field`.

        A fresh nonce per call, never derived from the row: GCM under a reused
        nonce leaks the XOR of the two plaintexts and the authentication key,
        and a deterministic envelope would additionally turn the column into a
        fingerprint -- two merchants who chose the same password would be
        visibly identical in the database.
        """
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._aesgcm.encrypt(
            nonce, value.encode("utf-8"), secret_aad(connection_id, field)
        )
        return MAGIC + nonce + sealed

    def unseal(self, envelope: bytes, *, connection_id: str, field: str) -> str:
        """The plaintext, or `SecretRefused`. Never a guess.

        Deliberately NOT chained (`from None`) on the library's `InvalidTag`:
        a chained traceback is one more surface a key could one day be
        rendered into, and the underlying exception says nothing this message
        does not.
        """
        head = len(MAGIC) + NONCE_BYTES
        if len(envelope) <= head or not envelope.startswith(MAGIC):
            raise SecretRefused("this value is not a sealed credential")
        nonce = envelope[len(MAGIC) : head]
        try:
            plaintext = self._aesgcm.decrypt(
                nonce, envelope[head:], secret_aad(connection_id, field)
            )
        except Exception:
            raise SecretRefused(
                "a stored credential failed to authenticate: it was sealed "
                "under a different key, or for a different connection or "
                "field, or it has been altered"
            ) from None
        return plaintext.decode("utf-8")


def _aesgcm(key: bytes):
    """The AES-GCM primitive, imported at construction rather than at import.

    Lazy for the reason `core/store/blobstore.py` gives: `core/` has to stay
    importable in a tree where the dependency that drags `cryptography` in was
    never installed. Unlike the blob store there is no keyless path here, so
    the absence is always an error -- and a named one, rather than a silent
    downgrade to storing a mailbox password in the clear.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover -- present transitively today
        raise SecretsUnavailable(
            "credentials cannot be stored because the `cryptography` package "
            "is not installed. Install it; there is deliberately no plaintext "
            "mode for a mailbox password."
        ) from exc
    return AESGCM(key)
