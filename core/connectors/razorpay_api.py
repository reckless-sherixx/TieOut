"""Pulls the settlement recon report a merchant would otherwise download by
hand from the Razorpay dashboard.

`GET /v1/settlements/recon/combined?year=&month=` is the API form of that
report. Probed 2026-09-02 with this project's own test credentials: HTTP 200,
`count: 0` -- reachable and authorised, empty because the account has never
produced a settlement. `POST /v1/settlements/ondemand` is blocked in test mode
(`instant_settlements_test_mode_blocked`), so no test account can manufacture
one; the first real settlement is what fills this.

The key pair is read by `api/`, never here, and never appears in an exception.
"""

from __future__ import annotations

import base64
import json
from datetime import date
from typing import Callable

from core.connectors.base import FetchedFile

_BASE = "https://api.razorpay.com/v1/settlements/recon/combined"

#: (url, headers) -> (status, body). Injected so every test runs offline.
HttpCall = Callable[[str, dict[str, str]], tuple[int, bytes]]


def _months(start: date, end: date) -> list[tuple[int, int]]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


class RazorpayConnector:
    name = "razorpay-api"

    def __init__(self, key_id: str | None, key_secret: str | None, http: HttpCall):
        self._key_id = key_id
        self._key_secret = key_secret
        self._http = http

    def available(self) -> bool:
        return bool(self._key_id and self._key_secret)

    def fetch(self, start: date, end: date) -> list[FetchedFile]:
        if not self.available():
            raise RuntimeError(
                "razorpay-api is not configured: set RECON_RAZORPAY_KEY_ID and "
                "RECON_RAZORPAY_KEY_SECRET"
            )
        token = base64.b64encode(
            f"{self._key_id}:{self._key_secret}".encode()
        ).decode()
        headers = {"Authorization": f"Basic {token}"}

        files: list[FetchedFile] = []
        for year, month in _months(start, end):
            status, body = self._http(f"{_BASE}?year={year}&month={month}", headers)
            if status == 401 or status == 403:
                # Never echo the credential -- name the variable to fix.
                raise RuntimeError(
                    f"Razorpay refused the credentials (HTTP {status}). The "
                    f"variables to fix are RECON_RAZORPAY_KEY_ID and "
                    f"RECON_RAZORPAY_KEY_SECRET."
                )
            if status != 200:
                raise RuntimeError(
                    f"Razorpay returned HTTP {status} for {year}-{month:02d}."
                )
            payload = json.loads(body)
            if not payload.get("items"):
                # A settlement-free month is normal; an empty file in the
                # quarantine queue would read as a broken export.
                continue
            files.append(
                FetchedFile(
                    suggested_name=f"razorpay-recon-{year}-{month:02d}.json",
                    content=body,
                    source_name=self.name,
                    window_start=date(year, month, 1),
                    window_end=date(year, month, 1),
                )
            )
        return files
