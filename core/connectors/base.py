"""Fetching a file and reading a file are different jobs.

A connector's entire output is bytes plus provenance. It never parses, never
canonicalises and never decides a format -- `core/adapters/` does all three, and
routing a pulled file through the same sniff/quarantine path is what keeps a
file that arrived over HTTPS from being trusted more than the same file
dragged onto the page.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, model_validator


class ConnectorUnconfigured(RuntimeError):
    """This connector was asked to fetch and nothing told it where to look.

    A `RuntimeError` subclass so it is still what every one of these modules
    documents itself as raising, and a distinct type so `api/connectors.py` can
    answer it with a 422 -- "you have not set this up" -- while a genuine
    upstream failure gets a 502. The two need different words on the screen and
    a bare `RuntimeError` would give them the same ones.

    **The message names the variable and never its value**, on the same rule
    `api/settings.AuthMisconfigured` follows.
    """


class FetchedFile(BaseModel):
    model_config = {"frozen": True}

    suggested_name: str
    content: bytes
    source_name: str
    window_start: date
    window_end: date

    @model_validator(mode="after")
    def _window_is_ordered(self) -> FetchedFile:
        if self.window_end < self.window_start:
            raise ValueError(
                f"window_end {self.window_end} precedes window_start "
                f"{self.window_start}; a fetch window cannot run backwards"
            )
        return self


@runtime_checkable
class SourceConnector(Protocol):
    name: str

    def available(self) -> bool:
        """False when this connector is unconfigured.

        Unconfigured is the DEFAULT and is not an error: a clone with no
        Razorpay key must start, and the console must be able to say the
        connector is off rather than showing it broken.
        """

    def fetch(self, start: date, end: date) -> list[FetchedFile]: ...
