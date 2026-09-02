"""A directory the merchant's bank statement lands in.

The fallback with no counterparty. `core/connectors/imap_mailbox.py` is the
route that reaches a real Indian bank statement without an FIU licence, and it
needs a mailbox the bank actually sends to; a watched directory needs nothing at
all. It is what covers the merchant whose statement arrives by courier, by
portal download, or from a bank whose mail this build cannot read yet.

It removes the manual upload step without claiming to be an integration, and
`SourceConnector` means a better source later is a new file rather than a
refactor.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from core.connectors.base import FetchedFile


class WatchedFolderConnector:
    name = "watched-folder"

    def __init__(self, root: Path | None):
        self._root = root

    def available(self) -> bool:
        return self._root is not None

    def fetch(self, start: date, end: date) -> list[FetchedFile]:
        if self._root is None or not self._root.is_dir():
            return []
        out: list[FetchedFile] = []
        for p in sorted(self._root.iterdir()):
            # A dotfile is never a merchant export. Quarantining `.DS_Store`
            # would teach a merchant that quarantine is noise.
            if not p.is_file() or p.name.startswith("."):
                continue
            out.append(
                FetchedFile(
                    suggested_name=p.name,
                    content=p.read_bytes(),
                    source_name=self.name,
                    window_start=start,
                    window_end=end,
                )
            )
        return out
