"""AES-256-GCM for a credential, and the binding that makes a row's own.

`core/store/blobstore.py` binds a blob's ADDRESS in as additional
authenticated data so a ciphertext cannot be moved to another address. This
module's values have no address -- they are columns of one row -- so what is
bound in is `f"{connection_id}:{field_name}"`, and these tests are the proof
that the binding does the job it is there for. Two attacks and one accident:

* a ciphertext lifted out of one connection's row and pasted into another's;
* the pdf secret swapped into the password column of the same row;
* a key rotation that leaves the old ciphertexts undecryptable, which must
  raise rather than return plausible bytes.

Nothing here uses a real credential. The plaintexts are obviously synthetic and
`.env` is never read.
"""

from __future__ import annotations

import os

import pytest

from core.store.secretbox import (
    NONCE_BYTES,
    SecretBox,
    SecretRefused,
    secret_aad,
)

#: Sixteen characters with no spaces, the shape of a Gmail App Password and
#: emphatically not one. The suite must never hold a real credential.
FAKE_PASSWORD = "abcdefghijklmnop"
FAKE_PDF_PASSWORD = "0123456789ABCDEF"

KEY = b"k" * 32
OTHER_KEY = b"j" * 32


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(KEY)


def test_a_sealed_secret_round_trips_under_its_own_binding(box):
    sealed = box.seal(FAKE_PASSWORD, connection_id="con-1", field="secret")
    assert box.unseal(sealed, connection_id="con-1", field="secret") == FAKE_PASSWORD


def test_the_ciphertext_does_not_contain_the_plaintext(box):
    sealed = box.seal(FAKE_PASSWORD, connection_id="con-1", field="secret")
    assert FAKE_PASSWORD.encode("utf-8") not in sealed


def test_a_ciphertext_from_one_connection_cannot_be_replayed_into_another(box):
    """The whole point of binding the connection id in as AAD.

    Somebody with write access to the database copies org A's
    `secret_ciphertext` into org B's row and points it at their own mail
    server. Without the binding it decrypts and B's fetcher logs in as A.
    """
    sealed = box.seal(FAKE_PASSWORD, connection_id="con-1", field="secret")
    with pytest.raises(SecretRefused):
        box.unseal(sealed, connection_id="con-2", field="secret")


def test_the_pdf_secret_cannot_be_swapped_into_the_password_field(box):
    """Same row, different column -- which is why the FIELD is bound too.

    An id alone would leave the two columns interchangeable, and a merchant
    whose statement password and mailbox password differ would have the wrong
    one presented at login with nothing saying so.
    """
    sealed = box.seal(
        FAKE_PDF_PASSWORD, connection_id="con-1", field="pdf_secret"
    )
    with pytest.raises(SecretRefused):
        box.unseal(sealed, connection_id="con-1", field="secret")


def test_a_different_key_cannot_read_it(box):
    sealed = box.seal(FAKE_PASSWORD, connection_id="con-1", field="secret")
    with pytest.raises(SecretRefused):
        SecretBox(OTHER_KEY).unseal(sealed, connection_id="con-1", field="secret")


def test_a_tampered_ciphertext_is_refused_rather_than_returned(box):
    sealed = bytearray(box.seal(FAKE_PASSWORD, connection_id="con-1", field="secret"))
    sealed[-1] ^= 0xFF
    with pytest.raises(SecretRefused):
        box.unseal(bytes(sealed), connection_id="con-1", field="secret")


def test_bytes_that_are_not_an_envelope_are_refused(box):
    with pytest.raises(SecretRefused):
        box.unseal(b"not an envelope at all", connection_id="con-1", field="secret")


def test_every_seal_uses_a_fresh_nonce(box):
    """Two seals of the same value must differ, and by the nonce.

    GCM is catastrophic under nonce reuse: two values sealed under one nonce
    leak their XOR and the authentication key. A deterministic envelope would
    also make the column a fingerprint -- two merchants with the same password
    would be visibly the same in the database.
    """
    first = box.seal(FAKE_PASSWORD, connection_id="con-1", field="secret")
    second = box.seal(FAKE_PASSWORD, connection_id="con-1", field="secret")
    assert first != second
    header = len(first) - NONCE_BYTES - len(FAKE_PASSWORD) - 16
    assert first[header : header + NONCE_BYTES] != second[header : header + NONCE_BYTES]


def test_the_key_is_never_rendered(box):
    """No repr, no message names the key. The blobstore rule, restated."""
    assert "k" * 32 not in repr(box)
    assert repr(KEY) not in repr(box)
    try:
        box.unseal(b"junk", connection_id="con-1", field="secret")
    except SecretRefused as error:
        assert repr(KEY) not in str(error)


@pytest.mark.parametrize("length", [0, 1, 15, 17, 31, 33])
def test_a_key_of_the_wrong_length_is_refused_without_naming_it(length):
    with pytest.raises(ValueError) as error:
        SecretBox(os.urandom(length))
    assert str(length) in str(error.value)


def test_the_aad_is_the_documented_string():
    """Spelled once, so the two callers cannot drift apart."""
    assert secret_aad("con-1", "secret") == b"con-1:secret"
