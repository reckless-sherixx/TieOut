"""A content-addressed store for uploaded files, encrypted at rest.

Phase 5 wires merchant uploads into this; Phase 4 builds and tests the
primitive. Two properties, and they are not independent -- the second is what
makes the first safe.

**Content-addressed.** A blob's address is the SHA-256 of its *plaintext*, so
the same file stored twice is stored once and the second `put` is a no-op. That
is the idempotent re-upload the ingestion spec asks for (2026-08-30 §3, A3),
arrived at from the storage side rather than bolted on: a merchant who uploads
January's settlement report again gets the same address, not a duplicate.

Addressing on the plaintext and not on the ciphertext is deliberate. AES-GCM
uses a fresh nonce per encryption, so the same file encrypted twice produces two
different ciphertexts; a ciphertext-addressed store would silently lose
deduplication the moment encryption was switched on -- and would lose it
*quietly*, as a storage-cost regression nobody attributes to the security work.

**Encrypted at rest, with the address bound in.** The digest travels as AES-GCM
additional authenticated data, so a blob's ciphertext cannot be moved to another
blob's address: the tag stops verifying and `get` raises rather than returning
the wrong merchant's file under the right name. Confidentiality without that
binding would still let an attacker with write access to the directory swap two
files around, which for a reconciliation input is a perfectly good attack.

**On the dependency.** `cryptography` is already an unconditional transitive
dependency of this project -- `google-genai` requires `google-auth`, which
requires `cryptography` in its base requirement set, and `uv.lock` pins 50.0.1.
So AES-GCM here adds nothing to the dependency surface. It is imported lazily
all the same, and its absence is an error only for a store that was given a key:
a build where the analyst layer was stripped out must still be able to run the
plaintext store, and must not be able to *believe* it is encrypting when it is
not.

**Plaintext is a mode, not a fallback.** No key means no encryption, and the
envelope says so in a byte on disk. What must never happen is a store that was
asked to encrypt and quietly did not, so the two modes refuse each other's
blobs: a keyed store will not read a plaintext blob and a keyless store will not
read a ciphertext one. Both raise. COMPLIANCE.md says which mode the demo runs
in and why.

**The key is never logged.** It is held on the instance and named in no
`__repr__`, no exception message and no path. Nothing in this module formats it.

No clock is read here -- `core/` may not -- so a blob carries no timestamp of
its own; when it needs one it will be a column beside the address, stamped at
the API boundary like every other timestamp in this system.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

__all__ = [
    "BlobNotFound",
    "BlobStore",
    "CorruptBlob",
    "EncryptionUnavailable",
    "SCHEME_AESGCM",
    "SCHEME_PLAINTEXT",
]

#: Envelope magic. Present in both modes so that "what is this file" is
#: answerable without the key, and so a file from some other tool landing in the
#: directory is rejected rather than decoded into nonsense.
MAGIC = b"RCNBLOB1"

#: Stored, not inferred. The mode a blob was written in is a property of the
#: blob, which is what lets a store refuse to read one it cannot honestly
#: handle -- and what will let a future key rotation re-encrypt in place.
SCHEME_PLAINTEXT = 0
SCHEME_AESGCM = 1

#: AES-GCM's standard nonce length. 96 bits is the size the mode is specified
#: and optimised for; anything else costs an extra GHASH pass and buys nothing.
NONCE_BYTES = 12

#: Two levels of two hex characters, so no directory holds more than 256
#: entries at either level. A flat directory of a million uploads is a
#: filesystem that becomes slow to open long before it becomes full.
_FANOUT = (2, 2)


class BlobNotFound(KeyError):
    """No blob at this address."""


class CorruptBlob(ValueError):
    """A blob failed to verify: bad envelope, bad tag, or the wrong content.

    One exception for all three, and the message never quotes the bytes. A
    caller can act on "this blob is not trustworthy"; a caller cannot act on
    *which* of the three it was, and an attacker probing a directory can.
    """


class EncryptionUnavailable(RuntimeError):
    """A key was supplied but `cryptography` is not importable.

    Raised rather than degraded to plaintext. A store that was asked to encrypt
    and silently did not is the single worst outcome available here: every
    downstream statement about encryption at rest becomes false, and nothing
    anywhere reports it.
    """


class BlobStore:
    """Content-addressed file storage under `root`, optionally encrypted.

    One instance per (directory, key). The key is supplied by the caller --
    `core/` reads no environment, so `api/settings.blob_key()` is where it comes
    from in this deployment.
    """

    def __init__(self, root: Path | str, *, key: bytes | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if key is not None:
            if len(key) not in (16, 24, 32):
                # Length only. The value is never named, here or anywhere.
                raise ValueError(
                    f"an AES key must be 16, 24 or 32 bytes; got {len(key)}"
                )
            self._aesgcm = _aesgcm(key)
        else:
            self._aesgcm = None

    def __repr__(self) -> str:
        """Names the mode, never the key.

        A `dataclass`-style repr that included `key=b'...'` would put the
        encryption key into every traceback, every debugger frame and every
        `print` a future developer reaches for -- which is exactly the leak the
        env-only secrets rule exists to prevent, arriving by a route nobody
        thinks to audit.
        """
        mode = "aes-gcm" if self.encrypted else "plaintext"
        return f"BlobStore(root={str(self.root)!r}, mode={mode!r})"

    @property
    def encrypted(self) -> bool:
        """Whether this store encrypts. A boolean, never the key -- the same
        discipline `api/settings.has_anthropic_api_key` follows."""
        return self._aesgcm is not None

    # --- addressing -----------------------------------------------------------

    @staticmethod
    def address(data: bytes) -> str:
        """The address `data` will be stored at: SHA-256 of the plaintext, hex.

        Exposed so a caller can ask "do I already have this file" before
        reading it into the store, which is what makes the re-upload path cheap
        rather than merely correct.
        """
        return hashlib.sha256(data).hexdigest()

    def path_for(self, digest: str) -> Path:
        """Where a digest lives on disk. Fanned out two levels."""
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            # Validated because this value becomes a path. A digest is the only
            # thing that may address a blob, and "../../etc/passwd" is not one.
            raise ValueError("a blob address is 64 lowercase hex characters")
        first, second = _FANOUT
        return (
            self.root / digest[:first] / digest[first : first + second] / digest
        )

    # --- reads and writes -----------------------------------------------------

    def exists(self, digest: str) -> bool:
        return self.path_for(digest).is_file()

    def put(self, data: bytes) -> str:
        """Store `data` and return its address. Idempotent.

        A blob already at the address is left exactly as it is, rather than
        rewritten. Rewriting would be visible only as a changed mtime and a new
        nonce, and it would mean a re-upload silently re-encrypting a file --
        so the same content stored under two different keys over time would
        depend on upload order, which is not a property anyone wants to reason
        about.

        The write is atomic: content lands in a temporary file in the same
        directory and is then renamed. An interrupted write must not leave a
        truncated file sitting at a valid address, because every later read of
        that address would fail and no later `put` would repair it -- the store
        would have permanently poisoned one content hash.
        """
        digest = self.address(data)
        target = self.path_for(digest)
        if target.is_file():
            return digest

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.{os.urandom(8).hex()}.tmp")
        temporary.write_bytes(self._wrap(data, digest))
        os.replace(temporary, target)
        return digest

    def get(self, digest: str) -> bytes:
        """The plaintext at `digest`, verified.

        Verification is not belt-and-braces. In encrypted mode the GCM tag
        already covers the ciphertext *and* the address; the digest is checked
        again afterwards because in plaintext mode there is no tag at all, and a
        read path whose integrity guarantee depends on which mode is configured
        is a read path nobody can make a statement about.
        """
        target = self.path_for(digest)
        try:
            envelope = target.read_bytes()
        except FileNotFoundError as exc:
            raise BlobNotFound(digest) from exc

        data = self._unwrap(envelope, digest)
        if self.address(data) != digest:
            raise CorruptBlob("blob content does not match its address")
        return data

    # --- the envelope ---------------------------------------------------------

    def _wrap(self, data: bytes, digest: str) -> bytes:
        if self._aesgcm is None:
            return MAGIC + bytes([SCHEME_PLAINTEXT]) + data
        nonce = os.urandom(NONCE_BYTES)
        # The address is the additional authenticated data: it is not secret --
        # it is the filename -- but binding it means a ciphertext moved to
        # another address stops verifying. Confidentiality alone would leave an
        # attacker with write access free to swap two merchants' files.
        sealed = self._aesgcm.encrypt(nonce, data, digest.encode("ascii"))
        return MAGIC + bytes([SCHEME_AESGCM]) + nonce + sealed

    def _unwrap(self, envelope: bytes, digest: str) -> bytes:
        header = len(MAGIC) + 1
        if len(envelope) < header or not envelope.startswith(MAGIC):
            raise CorruptBlob("not a blob envelope")
        scheme = envelope[len(MAGIC)]
        body = envelope[header:]

        if scheme == SCHEME_PLAINTEXT:
            if self._aesgcm is not None:
                # Refused, not read. A store configured to encrypt that happily
                # served plaintext would make "uploads are encrypted at rest"
                # true of new files and false of old ones, with nothing saying
                # which is which.
                raise CorruptBlob(
                    "this blob is stored in plaintext and this store is "
                    "configured to encrypt; it was written by a differently "
                    "configured deployment"
                )
            return body

        if scheme == SCHEME_AESGCM:
            if self._aesgcm is None:
                raise CorruptBlob(
                    "this blob is encrypted and this store has no key "
                    "configured"
                )
            nonce, sealed = body[:NONCE_BYTES], body[NONCE_BYTES:]
            if len(nonce) != NONCE_BYTES:
                raise CorruptBlob("truncated blob envelope")
            try:
                return self._aesgcm.decrypt(nonce, sealed, digest.encode("ascii"))
            except Exception:
                # The library's `InvalidTag` means "wrong key, wrong address or
                # tampered ciphertext" and a caller can act on none of those
                # distinctions -- while an attacker probing the directory can.
                # Collapsed to one, and deliberately NOT chained (`from None`):
                # a chained traceback is one more surface a key could one day
                # be rendered into.
                raise CorruptBlob("blob failed to authenticate") from None

        raise CorruptBlob("unknown blob scheme")


def _aesgcm(key: bytes):
    """The AES-GCM primitive, imported at construction rather than at import.

    Lazy because `core/` has to stay importable in a tree where the analyst
    layer -- and with it the dependency that drags `cryptography` in -- was
    never installed. A store with no key never reaches this function, so that
    tree can still run the plaintext mode; a store *with* a key gets a named
    error instead of a silent downgrade.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover -- present transitively today
        raise EncryptionUnavailable(
            "a blob encryption key was configured but the `cryptography` "
            "package is not installed, so uploads cannot be encrypted at rest. "
            "Install it, or unset the key to run the store in its documented "
            "plaintext mode."
        ) from exc
    return AESGCM(key)
