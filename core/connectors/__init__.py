"""Pulling a file, beside `core/adapters/` which reads one.

`core/adapters/` answers "what is in this file". This package answers the
question in front of it -- "where did the file come from" -- and answers it in
bytes. A connector fetches; an adapter parses. Keeping the two apart is what
lets a pulled file take exactly the same sniff, quarantine and blob-store path
an uploaded one takes, which is the only reason a file that arrived over HTTPS
is not trusted more than one a merchant dragged onto the page.

Nothing here reads a clock, a credential or the environment. `api/` supplies
the window, the key material and the timestamps, exactly as it does for
`core/adapters/`.
"""

from core.connectors.base import (
    ConnectorUnconfigured,
    FetchedFile,
    SourceConnector,
)

__all__ = ["ConnectorUnconfigured", "FetchedFile", "SourceConnector"]
