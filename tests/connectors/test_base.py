"""A connector produces BYTES, and nothing else.

The whole value of the existing adapter layer is that it turns one merchant's
file into canonical records with a quarantine for what it cannot read. A
connector that returned parsed records would bypass that and would be trusted
more than an uploaded file for no reason other than how it arrived.
"""

from datetime import date

import pytest

from core.connectors.base import FetchedFile


def test_a_fetched_file_is_bytes_not_records():
    f = FetchedFile(
        suggested_name="razorpay-settlements-2026-08.csv",
        content=b"settlement_id,amount\n",
        source_name="razorpay-api",
        window_start=date(2026, 8, 1),
        window_end=date(2026, 8, 31),
    )
    assert isinstance(f.content, bytes)
    assert not hasattr(f, "records")


def test_a_window_that_ends_before_it_starts_is_refused():
    with pytest.raises(ValueError):
        FetchedFile(
            suggested_name="x.csv", content=b"", source_name="s",
            window_start=date(2026, 8, 31), window_end=date(2026, 8, 1),
        )
