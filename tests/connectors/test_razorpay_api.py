"""Offline. The HTTP callable is injected, so no test here touches the network."""

import json
from datetime import date

import pytest

from core.connectors.razorpay_api import RazorpayConnector


def _http(payload, status=200):
    def call(url, headers):
        return status, json.dumps(payload).encode()
    return call


#: Sentinel credentials, distinctive enough that "this string is not in the
#: message" is a claim worth making. A one-letter key would make the
#: no-echo assertion below pass on the letter appearing nowhere in ordinary
#: English prose rather than on the credential being withheld -- and no
#: message that says "check RECON_RAZORPAY_KEY_ID" can avoid the letters of
#: its own sentence.
KEY_ID = "kid-9f3c"
KEY_SECRET = "sec-4a17"


def test_it_is_unavailable_without_credentials():
    """The default configuration of a clone, and not an error."""
    assert RazorpayConnector(None, None, _http({})).available() is False


def test_it_emits_one_file_per_month_in_the_window():
    c = RazorpayConnector(KEY_ID, KEY_SECRET,
                          _http({"entity": "collection", "count": 1,
                                 "items": [{"settlement_id": "setl_1"}]}))
    files = c.fetch(date(2026, 7, 15), date(2026, 9, 2))
    assert [f.suggested_name for f in files] == [
        "razorpay-recon-2026-07.json",
        "razorpay-recon-2026-08.json",
        "razorpay-recon-2026-09.json",
    ]


def test_an_empty_month_produces_no_file_rather_than_an_empty_one():
    """An empty settlement month is normal. A zero-row file in the quarantine
    queue would read as a broken export."""
    c = RazorpayConnector(KEY_ID, KEY_SECRET,
                          _http({"entity": "collection", "count": 0, "items": []}))
    assert c.fetch(date(2026, 8, 1), date(2026, 8, 31)) == []


def test_a_401_names_the_variable_to_fix_and_never_the_key():
    c = RazorpayConnector(KEY_ID, KEY_SECRET,
                          _http({"error": {"description": "auth"}}, status=401))
    with pytest.raises(RuntimeError) as exc:
        c.fetch(date(2026, 8, 1), date(2026, 8, 31))
    assert "RECON_RAZORPAY_KEY_ID" in str(exc.value)
    assert KEY_ID not in str(exc.value) and KEY_SECRET not in str(exc.value)


def test_an_unconfigured_fetch_names_both_variables_rather_than_returning_nothing():
    """An empty list would read as "the account had no settlements", which is
    the opposite of "nobody told this process where to look"."""
    with pytest.raises(RuntimeError) as exc:
        RazorpayConnector(None, None, _http({})).fetch(
            date(2026, 8, 1), date(2026, 8, 31)
        )
    assert "RECON_RAZORPAY_KEY_ID" in str(exc.value)
    assert "RECON_RAZORPAY_KEY_SECRET" in str(exc.value)
