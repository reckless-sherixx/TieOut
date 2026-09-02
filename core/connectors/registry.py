"""Name -> connector, for the one caller that has to list them.

Deliberately thinner than `core/adapters/registry.py`, and the difference is
the point. That registry has to *decide* which adapter reads a file, because a
merchant's upload arrives with no reliable statement of what it is. A connector
is chosen by name, by a person, in a request -- there is nothing to detect and
no ambiguity to refuse, so this is a mapping and not an algorithm.

Every argument is passed in rather than read from the environment: `core/` reads
no configuration and no clock, so `api/settings.py` supplies the credentials,
the watch directory and the injected transports, exactly as it does for the
analyst layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from core.connectors.base import SourceConnector


def default_connectors(
    *,
    razorpay_key_id: str | None = None,
    razorpay_key_secret: str | None = None,
    http: Callable[..., Any] | None = None,
    imap: dict[str, Any] | None = None,
    watch_dir: Path | None = None,
) -> dict[str, SourceConnector]:
    """Every connector this build ships, keyed by the name the API uses.

    Imported inside the function for the same reason `default_adapters` is: the
    registry stays importable while a connector module is being written, and a
    connector can import from `base` without a cycle through here.

    A connector with no configuration behind it is still constructed and still
    listed. `available()` is how the console says "this one is off", and a
    connector missing from the list entirely would be indistinguishable from
    one this build does not have.
    """
    from core.connectors.imap_mailbox import ImapMailboxConnector
    from core.connectors.razorpay_api import RazorpayConnector
    from core.connectors.watched_folder import WatchedFolderConnector

    return {
        "razorpay-api": RazorpayConnector(
            razorpay_key_id, razorpay_key_secret, http or _no_http
        ),
        "imap-mailbox": ImapMailboxConnector(**(imap or {"host": None})),
        "watched-folder": WatchedFolderConnector(watch_dir),
    }


def _no_http(*_args, **_kwargs):  # pragma: no cover - unreachable when unavailable
    """The transport an unconfigured Razorpay connector would use.

    It cannot be called: `RazorpayConnector.fetch` refuses before it reaches the
    transport when `available()` is false, and `api/settings.py` supplies a real
    one whenever the key pair is set. Raising here rather than defaulting to a
    live urllib call means a wiring mistake fails loudly instead of dialling out.
    """
    raise RuntimeError(
        "razorpay-api has no HTTP transport wired: api/settings.py supplies one "
        "when RECON_RAZORPAY_KEY_ID and RECON_RAZORPAY_KEY_SECRET are set"
    )
