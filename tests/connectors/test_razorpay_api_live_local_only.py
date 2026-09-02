"""Skipped everywhere except a machine with real credentials in `.env`.

Prints AGGREGATES only -- never a settlement id, an amount or a key. What this
proves is that the endpoint still answers and still has the shape the adapter
reads; what it cannot prove is the layout of a report nobody has yet produced.

It is read-only: one `GET /v1/settlements/recon/combined` per month in the
window and nothing else. It creates no settlement, no payment and no refund.
"""

import os
from datetime import date

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("RAZORPAY_KEY_ID") and os.environ.get("RAZORPAY_KEY_SECRET")),
    reason=(
        "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. They live in a "
        "gitignored `.env` on the one machine that holds a real test account, "
        "so a skip here is the expected outcome everywhere else and in CI always."
    ),
)


def test_the_recon_endpoint_answers():
    import urllib.error
    import urllib.request

    from core.connectors.razorpay_api import RazorpayConnector

    def http(url, headers):
        """An HTTP status is a RESULT here, not an exception.

        `urlopen` raises on 4xx and 5xx, which would rob the connector of the
        one thing this probe is checking -- that it turns a refusal into a
        message naming the variable to fix rather than a traceback. So the
        status comes back either way, and only a genuine connectivity failure
        (`URLError` that is not an `HTTPError`) is left to the caller, which
        skips: an aeroplane is not a regression.
        """
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as error:
            return error.status, error.read()

    c = RazorpayConnector(
        os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"], http
    )
    try:
        files = c.fetch(date(2026, 8, 1), date(2026, 8, 31))
    except urllib.error.URLError as error:
        pytest.skip(f"the Razorpay API was unreachable: {error.reason}")
    print(f"months with settlements: {len(files)}")
    assert isinstance(files, list)
