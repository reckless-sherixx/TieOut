"""Reads bank statements out of the merchant's own email.

**Why email and not an API.** There is no merchant-facing pull API for an Indian
bank statement. The RBI Account Aggregator framework is the only sanctioned
route and it is closed to this project twice over: production FIU registration
is open only to entities regulated by RBI, SEBI, IRDAI or PFRDA, and the
self-serve sandbox serves mock data, so an AA integration could demonstrate a
flow and could never fetch a real merchant's real statement. Email can. The bank
already sends the statement every month, to an address the merchant already
controls; this reads the attachment out of it.

**Three rules make this safe to point at a real inbox**, and each is asserted on
the literal IMAP command rather than on the outcome, because the outcome looks
identical whether or not they hold:

1. **The SEARCH is narrowed on both axes** -- the fetch window (`SINCE`/`BEFORE`)
   and the configured senders (`FROM`). One search per sender rather than one
   nested `OR` chain: servers differ on how deeply they will nest, and a search
   that silently fails on one provider is worse than two that work everywhere.
   With no sender configured this refuses outright. An unfiltered SEARCH is not
   a reconciliation input, it is a mailbox dump.
2. **The mailbox is opened read-only and the FETCH uses `BODY.PEEK[]`.** A plain
   `BODY[]` sets `\\Seen` as a side effect, so reconciling would mark a
   merchant's unread mail read. Nothing is stored, flagged, moved or deleted.
3. **Attachment filenames are attacker-controlled** -- they are whatever the
   sender typed -- and reach a filesystem path downstream, so `sanitise_filename`
   flattens every one of them before it becomes a `suggested_name`.
4. **A credit report is never fetched.** `CREDIT_REPORT_DENY` is applied to
   every attachment name always, and the optional
   `RECON_IMAP_FILENAME_PATTERN` narrows further on top of it. Both are
   decided BEFORE the payload is decoded, because filtering at quarantine is
   too late: by then the file has been read out of the mailbox, decrypted in
   memory and written to the blob store. Whatever is declined is COUNTED and
   NAMED in `last_skipped` -- never silently dropped.

**Encrypted PDFs.** Indian bank statement PDFs arrive password-protected as a
matter of course, and `slice-pdf-v1` reads a text layer it cannot reach through
the encryption. So a configured `pdf_password` is applied here, before the
adapter sees the bytes. When there is no password, or it does not work, the file
is emitted **unchanged** rather than dropped or raised on: it then fails
visibly in the quarantine queue, where a human can act on it. A statement that
silently disappears is the one failure mode this module must not have.

No credential is read here -- `api/settings.py` supplies them -- and no
credential appears in any message this module raises. The variable to fix is
named; the value never is.
"""

from __future__ import annotations

import email
import imaplib
import io
import re
from datetime import date, timedelta
from email import policy
from email.message import Message
from typing import Callable, Iterable

from core.connectors.base import ConnectorUnconfigured, FetchedFile

#: Builds a connection. Injected so every unit test runs offline against a fake
#: -- the same seam `core/connectors/razorpay_api.py` uses for its HTTP call.
ImapFactory = Callable[[str, int], "imaplib.IMAP4"]

DEFAULT_PORT = 993
DEFAULT_FOLDER = "INBOX"

#: What this build can do something with once it is downloaded. Everything else
#: on a bank's email -- the logo in the signature, a terms-and-conditions
#: `.docx` -- would land in the quarantine queue every month and teach a
#: merchant that quarantine is noise.
STATEMENT_SUFFIXES = (".pdf", ".csv", ".xls", ".xlsx", ".txt", ".sta")

#: Filenames that are never fetched, whatever else is configured.
#:
#: On 2026-09-02 `sliceCIBILReportLatest.pdf` -- 326 KB -- came down with the
#: statements, because the sender filter matches everything from the bank's
#: address and the bank mails credit reports from that same address. It failed
#: to decrypt and landed in quarantine, so nothing leaked into a run; but a
#: CIBIL report -- score, loan history, enquiries -- has no business entering a
#: reconciliation process at all, and by the time it is quarantined it has been
#: read out of the mailbox, decrypted in memory and written to the blob store.
#: Sender filtering alone is too coarse a control for a personal mailbox.
#:
#: India-specific by design: CIBIL, Experian, Equifax and CRIF High Mark are the
#: four credit bureaus operating in India, and this is an India-first product.
#: Matched as a case-insensitive substring of the filename, because a bureau's
#: name appears in the middle of one (`sliceCIBILReportLatest.pdf`) as readily
#: as at the start.
#:
#: **This is not configuration.** `filename_pattern` is the merchant narrowing
#: what they want fetched; it is not a way to opt back in to a credit report,
#: and this list is applied after it for that reason.
CREDIT_REPORT_DENY = (
    "cibil",
    "creditreport",
    "credit_report",
    "credit-report",
    "experian",
    "equifax",
    "crif",
)

#: IMAP dates are `DD-Mon-YYYY` with English month abbreviations. Spelled out
#: rather than taken from `strftime("%b")`, which is locale-dependent: a server
#: handed `01-ago-2026` returns a parse error, and a connector whose search
#: silently fails under a different `LC_TIME` is a bug nobody would look for.
_IMAP_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_PATH_SEPARATORS = re.compile(r"[\\/]")


def imap_date(day: date) -> str:
    """`date(2026, 8, 1)` -> `01-Aug-2026`, the only date form IMAP accepts."""
    return f"{day.day:02d}-{_IMAP_MONTHS[day.month - 1]}-{day.year}"


def sanitise_filename(raw: str | None, fallback: str) -> str:
    """A mail attachment's name, flattened into something safe to write.

    The name is whatever the sender typed, so it is treated as hostile: the
    path is reduced to its last segment, every character outside
    `[A-Za-z0-9._-]` is dropped, and any surviving `..` is collapsed. What comes
    back cannot name a directory, cannot climb out of one and is never empty --
    an empty name would otherwise become a file called nothing at all.
    """
    base = _PATH_SEPARATORS.split(raw or "")[-1]
    cleaned = _UNSAFE_CHARS.sub("", base)
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip("._-")
    return cleaned or fallback


def parse_senders(sender_filter: str | None) -> list[str]:
    """`"a@x, b@y"` -> `["a@x", "b@y"]`. Empty means no sender, never "all"."""
    return [s.strip() for s in (sender_filter or "").split(",") if s.strip()]


def is_statement_attachment(part: Message) -> bool:
    """Whether this MIME part is a file a merchant's bank sent as a statement.

    `attachment` disposition only: an inline part is a signature logo or a
    tracking pixel, and a bank's mail carries one every month.
    """
    if part.get_content_maintype() == "multipart":
        return False
    if (part.get_content_disposition() or "") != "attachment":
        return False
    name = (part.get_filename() or "").lower()
    return name.endswith(STATEMENT_SUFFIXES)


def is_credit_report(filename: str | None) -> bool:
    """Whether this attachment name is a credit bureau's report.

    Substring, case-folded, against `CREDIT_REPORT_DENY`. Deliberately blunt:
    the cost of a false positive is a merchant renaming a statement that has the
    word `cibil` in it, and the cost of a false negative is a credit history in
    a reconciliation pipeline. Those are not the same size.
    """
    name = (filename or "").lower()
    return any(marker in name for marker in CREDIT_REPORT_DENY)


def decrypt_pdf(payload: bytes, password: str | None) -> bytes:
    """Decrypted PDF bytes, or `payload` unchanged when that is not possible.

    Never raises and never returns nothing. An encrypted statement with no
    working password still has to reach the ingest path so it can be refused
    where a human can see the refusal; dropping it would remove the only
    evidence the statement ever arrived.

    The password is a secret. It is used and never reported: no branch here
    puts it in a return value, a log line or an exception.
    """
    if not payload.startswith(b"%PDF-"):
        return payload
    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(io.BytesIO(payload))
        if not reader.is_encrypted:
            return payload
        if not password or not reader.decrypt(password):
            return payload
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - the bytes are owed either way
        return payload


class ImapMailboxConnector:
    name = "imap-mailbox"

    def __init__(
        self,
        host: str | None,
        port: int = DEFAULT_PORT,
        username: str | None = None,
        password: str | None = None,
        sender_filter: str | None = None,
        folder: str = DEFAULT_FOLDER,
        pdf_password: str | None = None,
        filename_pattern: str | None = None,
        imap_factory: ImapFactory | None = None,
    ):
        self._host = host
        self._port = port or DEFAULT_PORT
        self._username = username
        self._password = password
        self._senders = parse_senders(sender_filter)
        self._folder = folder or DEFAULT_FOLDER
        self._pdf_password = pdf_password
        self._factory = imap_factory or imaplib.IMAP4_SSL

        #: Attachment names this connector declined on the last `fetch`.
        #:
        #: **Counted and named, never silently dropped** -- the same argument
        #: the ingest boundary makes about a file that produced nothing: an
        #: attachment the fetcher declined is a fact the merchant is entitled
        #: to, and a file that vanishes with no record is indistinguishable
        #: from one the bank never sent.
        #:
        #: **Why an attribute and not a return value.** `fetch` returns
        #: `list[FetchedFile]` by the `SourceConnector` protocol, and widening
        #: it would reach `api/connectors.py`, which another lane owns this
        #: session. The trade-off is real and worth naming: an attribute is
        #: per-instance mutable state on a connector that is otherwise
        #: stateless, so it is reset at the top of every `fetch` and is only
        #: meaningful to the caller that made the call. When the sync result
        #: grows a `skipped_names` field, this should become part of the return
        #: and the attribute should go.
        self.last_skipped: list[str] = []

        #: Compiled here so a bad pattern is one error, but NOT raised here:
        #: `api/settings.connectors()` constructs every connector on requests
        #: that have nothing to do with mail, and a typo in one variable must
        #: not take the connector list down with it. `fetch` raises instead,
        #: naming the variable, which is where the merchant is asking for the
        #: thing that cannot be done.
        self._pattern = None
        self._pattern_error: str | None = None
        if filename_pattern:
            try:
                self._pattern = re.compile(filename_pattern, re.IGNORECASE)
            except re.error as error:
                self._pattern_error = str(error)

    def available(self) -> bool:
        """Whether a mailbox is configured at all.

        Unconfigured is the DEFAULT and is not an error: a clone with no mail
        account must start, and the console must be able to say the connector
        is off rather than showing it broken.
        """
        return bool(self._host and self._username and self._password)

    def fetch(self, start: date, end: date) -> list[FetchedFile]:
        if not self.available():
            raise ConnectorUnconfigured(
                "imap-mailbox is not configured: set RECON_IMAP_HOST, "
                "RECON_IMAP_USER and RECON_IMAP_PASSWORD"
            )
        if not self._senders:
            raise ConnectorUnconfigured(
                "imap-mailbox has no sender filter: set RECON_IMAP_SENDERS to "
                "the addresses or domains your bank sends statements from. "
                "Searching a mailbox unfiltered would download every message "
                "in the window, which is not a reconciliation input."
            )
        if self._pattern_error is not None:
            raise ConnectorUnconfigured(
                f"RECON_IMAP_FILENAME_PATTERN is not a valid regular "
                f"expression ({self._pattern_error}). It is optional -- unset "
                f"it to fetch every statement attachment -- but a mailbox is "
                f"not searched on a pattern nobody can compile."
            )

        # Per fetch, not cumulative: this reports what THIS call declined.
        self.last_skipped = []

        connection = self._factory(self._host, self._port)
        try:
            connection.login(self._username, self._password)
            # Read-only: the server cannot set `\Seen` even if a future edit
            # forgot the PEEK below. Two independent guards on one promise.
            connection.select(self._folder, readonly=True)

            files: list[FetchedFile] = []
            for uid in self._search(connection, start, end):
                for raw_name, payload in self._attachments(connection, uid):
                    files.append(
                        FetchedFile(
                            suggested_name=sanitise_filename(
                                raw_name, f"statement-{uid}.pdf"
                            ),
                            content=decrypt_pdf(payload, self._pdf_password),
                            source_name=self.name,
                            window_start=start,
                            window_end=end,
                        )
                    )
            return files
        finally:
            self._hang_up(connection)

    # -- internals -------------------------------------------------------------

    def _search(self, connection, start: date, end: date) -> list[str]:
        """Message uids in the window from a configured sender, deduplicated.

        `BEFORE` is exclusive in IMAP, so the day AFTER the window's last day is
        what includes the last day. Getting that wrong loses the 31st of every
        month, which is a whole statement in a period that ends on one.
        """
        since = imap_date(start)
        before = imap_date(end + timedelta(days=1))

        uids: list[str] = []
        seen: set[str] = set()
        for sender in self._senders:
            criteria = f'(SINCE {since} BEFORE {before} FROM "{sender}")'
            status, data = connection.uid("SEARCH", None, criteria)
            if status != "OK":
                raise RuntimeError(
                    f"the mail server refused a search of {self._folder!r} "
                    f"(status {status}). Check RECON_IMAP_FOLDER."
                )
            for uid in _uids_of(data):
                if uid not in seen:
                    seen.add(uid)
                    uids.append(uid)
        return uids

    def _attachments(self, connection, uid: str) -> Iterable[tuple[str | None, bytes]]:
        # BODY.PEEK[] rather than BODY[]: the second sets `\Seen`, and marking a
        # merchant's unread mail read as a side effect of reconciling is the
        # thing that would make this connector untrustworthy.
        status, data = connection.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK":
            raise RuntimeError(
                f"the mail server refused to return message {uid} from "
                f"{self._folder!r} (status {status})."
            )
        raw = _body_of(data)
        if raw is None:
            return []
        message = email.message_from_bytes(raw, policy=policy.default)
        out: list[tuple[str | None, bytes]] = []
        for part in message.walk():
            if not is_statement_attachment(part):
                continue
            raw_name = part.get_filename()
            if not self._wanted(raw_name):
                # Declined BEFORE `get_payload(decode=True)`, which is the whole
                # point: nothing beyond what walking the MIME tree already
                # required is read, the base64 is never decoded, the PDF
                # password is never applied to it, and no copy of it is made.
                # Quarantining a credit report is too late; this is not.
                self.last_skipped.append(sanitise_filename(raw_name, "attachment"))
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            out.append((raw_name, payload))
        return out

    def _wanted(self, filename: str | None) -> bool:
        """Whether this attachment is fetched at all.

        Two filters, and the order is the argument. The merchant's optional
        pattern narrows what they want; the deny list then applies REGARDLESS,
        because it is not configuration and a pattern is not a way to opt back
        in to a credit report.
        """
        if self._pattern is not None and not self._pattern.search(filename or ""):
            return False
        return not is_credit_report(filename)

    @staticmethod
    def _hang_up(connection) -> None:
        """Close and log out, and never let the teardown mask the real error."""
        for step in ("close", "logout"):
            try:
                getattr(connection, step)()
            except Exception:  # noqa: BLE001 - a failed hang-up is not the answer
                pass


def _uids_of(data) -> list[str]:
    """`[b"1 2 3"]` -> `["1", "2", "3"]`, tolerating an empty or odd reply."""
    out: list[str] = []
    for chunk in data or []:
        if not chunk:
            continue
        text = chunk.decode("ascii", "ignore") if isinstance(chunk, bytes) else str(chunk)
        out += text.split()
    return out


def _body_of(data) -> bytes | None:
    """The RFC822 bytes out of an imaplib FETCH reply.

    imaplib returns a list mixing tuples -- `(header, payload)` -- with bare
    byte strings for the closing parenthesis, so the payload is the second half
    of the first tuple and nothing else in the list is the message.
    """
    for chunk in data or []:
        if isinstance(chunk, tuple) and len(chunk) >= 2 and isinstance(chunk[1], bytes):
            return chunk[1]
    return None
