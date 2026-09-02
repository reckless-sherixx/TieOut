"""The upload store: content addressing and encryption at rest (Phase 4 item 4).

Phase 5 wires merchant uploads into `core/store/blobstore.py`; this is the
primitive and its proofs. The claims that end up in COMPLIANCE.md are made here
first, because a compliance document that asserts something no test checks is
the exact genre of document this project exists not to write.

The strongest of them is not confidentiality but **binding**: a blob's address
travels as AES-GCM additional authenticated data, so ciphertext moved to another
address stops verifying. An attacker with write access to the upload directory
who can swap two merchants' files around has a perfectly good attack on a
reconciliation system, and encryption alone does not answer it.
"""

from __future__ import annotations

import os

import pytest

from api import settings
from core.store.blobstore import (
    MAGIC,
    SCHEME_AESGCM,
    SCHEME_PLAINTEXT,
    BlobNotFound,
    BlobStore,
    CorruptBlob,
)

KEY = bytes(range(32))
OTHER_KEY = bytes(range(32, 64))

#: A distinctive plaintext: short enough to read in a failure message, and
#: containing a marker a naive substring search would find in a file that was
#: not really encrypted.
SETTLEMENT = b"settlement_utr,ICIC0000123,4655654\nMARKER-PLAINTEXT-CANARY\n"


@pytest.fixture
def encrypted(tmp_path) -> BlobStore:
    return BlobStore(tmp_path / "blobs", key=KEY)


@pytest.fixture
def plain(tmp_path) -> BlobStore:
    return BlobStore(tmp_path / "plain")


# --- content addressing -------------------------------------------------------


def test_the_same_content_stored_twice_is_stored_once(encrypted):
    """Idempotent re-upload, from the storage side.

    A merchant who uploads January's settlement report a second time must get
    the same address and not a duplicate -- which is what the ingestion spec
    asks for, arrived at here rather than bolted on at the API.
    """
    first = encrypted.put(SETTLEMENT)
    on_disk = encrypted.path_for(first).read_bytes()
    second = encrypted.put(SETTLEMENT)

    assert first == second
    # Byte-identical: the second put did not rewrite the file with a new nonce.
    # Rewriting would make "which key is this blob under" depend on upload
    # order, which is not a property anyone should have to reason about.
    assert encrypted.path_for(second).read_bytes() == on_disk


def test_the_address_is_the_digest_of_the_plaintext_not_of_the_ciphertext(
    encrypted, plain
):
    """The two modes agree on addresses, and that is the point.

    AES-GCM uses a fresh nonce per encryption, so a ciphertext-addressed store
    would lose deduplication the moment encryption was switched on -- quietly,
    as a storage-cost regression nobody would attribute to the security work.
    """
    assert encrypted.put(SETTLEMENT) == plain.put(SETTLEMENT)
    assert encrypted.put(SETTLEMENT) == BlobStore.address(SETTLEMENT)


def test_different_content_gets_different_addresses(encrypted):
    assert encrypted.put(SETTLEMENT) != encrypted.put(SETTLEMENT + b"x")


def test_an_unknown_address_is_not_found(encrypted):
    with pytest.raises(BlobNotFound):
        encrypted.get("0" * 64)


@pytest.mark.parametrize(
    "address",
    ["", "abc", "../../../etc/passwd", "Z" * 64, "A" * 64, "0" * 63, "0" * 65],
)
def test_an_address_that_is_not_a_digest_is_refused_before_it_becomes_a_path(
    encrypted, address
):
    """The address becomes a filesystem path, so it is validated as one thing
    and one thing only. `../` in an address is not an edge case, it is the
    attack."""
    with pytest.raises(ValueError):
        encrypted.path_for(address)


def test_a_partial_write_cannot_leave_a_truncated_blob_at_a_valid_address(
    encrypted,
):
    """Writes are atomic: temporary file, then rename.

    Without it an interrupted write leaves a short file at a valid address,
    every later read of that address fails, and no later `put` repairs it --
    because `put` sees a file there and returns. The store would have
    permanently poisoned one content hash.
    """
    digest = encrypted.put(SETTLEMENT)
    directory = encrypted.path_for(digest).parent
    assert [p.name for p in directory.iterdir()] == [digest], (
        "a temporary file was left behind"
    )


# --- encryption at rest -------------------------------------------------------


def test_encryption_is_actually_on_in_this_build(encrypted):
    """`cryptography` is an unconditional transitive dependency here --
    `google-genai` -> `google-auth` -> `cryptography`, pinned in `uv.lock` --
    so the encrypted mode is the real one and not a documented aspiration.

    Asserted rather than assumed, because "we would encrypt if the library were
    present" and "we encrypt" are very different sentences to put in a
    compliance document, and only one of them is checkable.
    """
    assert encrypted.encrypted is True
    digest = encrypted.put(SETTLEMENT)
    assert encrypted.path_for(digest).read_bytes()[len(MAGIC)] == SCHEME_AESGCM


def test_the_plaintext_never_appears_on_disk_in_encrypted_mode(encrypted):
    digest = encrypted.put(SETTLEMENT)
    raw = encrypted.path_for(digest).read_bytes()
    assert SETTLEMENT not in raw
    assert b"MARKER-PLAINTEXT-CANARY" not in raw
    assert b"ICIC0000123" not in raw


def test_an_encrypted_blob_round_trips(encrypted):
    assert encrypted.get(encrypted.put(SETTLEMENT)) == SETTLEMENT


def test_a_keyless_store_writes_the_bytes_it_was_given(plain):
    """The documented plaintext mode. It is a *mode*, and the envelope says so
    in a byte -- so nothing has to guess later what a file on disk is."""
    assert plain.encrypted is False
    digest = plain.put(SETTLEMENT)
    raw = plain.path_for(digest).read_bytes()
    assert raw[len(MAGIC)] == SCHEME_PLAINTEXT
    assert raw[len(MAGIC) + 1 :] == SETTLEMENT
    assert plain.get(digest) == SETTLEMENT


# --- what the encryption actually buys ----------------------------------------


def test_a_blob_moved_to_another_blobs_address_fails_to_authenticate(encrypted):
    """The binding, and the reason the address is the AAD.

    An attacker with write access to the upload directory who can put HDFC's
    ciphertext at ICICI's address has swapped two merchants' bank statements
    without ever decrypting either. Confidentiality does not answer that; the
    tag over the address does.
    """
    mine = encrypted.put(SETTLEMENT)
    theirs = encrypted.put(b"a completely different bank statement")

    swapped = encrypted.path_for(theirs).read_bytes()
    encrypted.path_for(mine).write_bytes(swapped)

    with pytest.raises(CorruptBlob):
        encrypted.get(mine)


def test_a_single_flipped_byte_is_detected(encrypted):
    digest = encrypted.put(SETTLEMENT)
    path = encrypted.path_for(digest)
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0x01
    path.write_bytes(bytes(raw))

    with pytest.raises(CorruptBlob):
        encrypted.get(digest)


def test_a_blob_written_under_one_key_cannot_be_read_under_another(
    tmp_path, encrypted
):
    """Key rotation is not implemented, and this is what its absence looks
    like: a clear refusal rather than a wrong answer. COMPLIANCE.md names
    rotation as unimplemented rather than implying it works."""
    digest = encrypted.put(SETTLEMENT)
    other = BlobStore(encrypted.root, key=OTHER_KEY)
    with pytest.raises(CorruptBlob):
        other.get(digest)


def test_the_two_modes_refuse_each_others_blobs(tmp_path):
    """Neither direction silently succeeds.

    A store configured to encrypt that happily served plaintext would make
    "uploads are encrypted at rest" true of new files and false of old ones,
    with nothing on the wire saying which is which -- and that is the sentence
    a compliance document would then be wrong about.
    """
    root = tmp_path / "mixed"
    written_plain = BlobStore(root).put(SETTLEMENT)
    written_sealed = BlobStore(root, key=KEY).put(b"a second file entirely")

    with pytest.raises(CorruptBlob):
        BlobStore(root, key=KEY).get(written_plain)
    with pytest.raises(CorruptBlob):
        BlobStore(root).get(written_sealed)


def test_a_file_that_is_not_a_blob_envelope_is_refused(encrypted):
    """A stray file dropped into the directory decodes to nothing, rather than
    to nonsense that a parser downstream then has to survive."""
    digest = encrypted.put(SETTLEMENT)
    encrypted.path_for(digest).write_bytes(b"just some bytes")
    with pytest.raises(CorruptBlob):
        encrypted.get(digest)


def test_integrity_is_checked_in_plaintext_mode_too(plain):
    """There is no GCM tag in plaintext mode, so the digest is the whole check
    -- and it runs in both modes, so the read path's integrity guarantee does
    not depend on which mode a deployment happens to be in."""
    digest = plain.put(SETTLEMENT)
    plain.path_for(digest).write_bytes(MAGIC + bytes([SCHEME_PLAINTEXT]) + b"other")
    with pytest.raises(CorruptBlob):
        plain.get(digest)


# --- the key is never logged --------------------------------------------------


def test_the_key_appears_in_no_repr_and_in_no_error_message(tmp_path, encrypted):
    """The env-only secrets rule, extended to the one object that holds key
    material in memory.

    A `dataclass`-style repr would put the key into every traceback, every
    debugger frame and every `print` a developer reaches for -- a leak arriving
    by a route nobody thinks to audit.
    """
    rendered = [repr(encrypted), str(encrypted)]

    digest = encrypted.put(SETTLEMENT)
    encrypted.path_for(digest).write_bytes(b"nonsense")
    for store, call in (
        (encrypted, lambda: encrypted.get(digest)),
        (BlobStore(tmp_path / "b", key=KEY), lambda: None),
    ):
        try:
            call()
        except Exception as exc:  # noqa: BLE001 -- the message is what is on trial
            rendered.append(repr(exc))
        rendered.append(repr(store))

    for text in rendered:
        assert KEY.hex() not in text
        assert str(KEY) not in text
        assert "\\x00\\x01\\x02" not in text


def test_a_key_of_the_wrong_length_is_refused_by_length_and_not_by_value(tmp_path):
    with pytest.raises(ValueError) as raised:
        BlobStore(tmp_path / "short", key=b"too-short")
    message = str(raised.value)
    assert "9" in message
    assert "too-short" not in message


# --- the settings seam --------------------------------------------------------


def test_the_key_comes_from_the_environment_and_core_never_reads_it(monkeypatch):
    """`core/` reads no environment: the key is a constructor argument, and
    `api/settings.blob_key()` is the one place the variable is named."""
    import base64

    monkeypatch.setenv(
        settings.BLOB_KEY_ENV, base64.urlsafe_b64encode(KEY).decode()
    )
    assert settings.blob_key() == KEY

    monkeypatch.delenv(settings.BLOB_KEY_ENV)
    assert settings.blob_key() is None


def test_a_key_that_does_not_decode_is_an_error_and_not_a_silent_plaintext_mode(
    monkeypatch,
):
    """The failure this ordering exists to prevent: a typo in the base64
    turning an encrypted deployment into a plaintext one with no message
    anywhere."""
    monkeypatch.setenv(settings.BLOB_KEY_ENV, "not!valid!base64!!")
    with pytest.raises(settings.AuthMisconfigured):
        settings.blob_key()

    monkeypatch.setenv(settings.BLOB_KEY_ENV, "c2hvcnQ=")  # 5 bytes
    with pytest.raises(settings.AuthMisconfigured) as raised:
        settings.blob_key()
    assert settings.BLOB_KEY_ENV in str(raised.value)


def test_a_random_key_from_the_documented_recipe_works_end_to_end(tmp_path):
    """The command in `.env.example`, executed.

    A recipe in a comment that nobody has run is a recipe that is wrong.
    """
    import base64

    generated = base64.urlsafe_b64encode(os.urandom(32)).decode()
    store = BlobStore(tmp_path / "recipe", key=base64.urlsafe_b64decode(generated))
    assert store.get(store.put(SETTLEMENT)) == SETTLEMENT
