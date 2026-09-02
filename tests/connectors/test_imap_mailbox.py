"""Offline. The IMAP connection is injected, so nothing here touches a mailbox.

Two of these tests are about the mailbox rather than about the statement, and
they are the ones that decide whether anybody will ever point this at a real
inbox: the SEARCH is scoped, and the FETCH peeks. A connector that walked a
whole mailbox, or that marked a merchant's mail read as a side effect of
reconciling, is one nobody should trust with their bank mail -- so both are
asserted on the literal command string rather than on the outcome, because the
outcome looks identical either way.
"""

from __future__ import annotations

import io
from datetime import date
from email.message import EmailMessage

import pytest

from core.connectors.imap_mailbox import ImapMailboxConnector, sanitise_filename

SENDERS = "alerts@slice.co,statements@hdfcbank.net"


# --- a fake mailbox -----------------------------------------------------------


class FakeIMAP:
    """The subset of `imaplib.IMAP4_SSL` this connector uses, and no more.

    It RECORDS every command string, because the two properties that matter
    most here -- the search is narrowed, the fetch does not mutate -- are
    properties of the command and not of the reply.
    """

    def __init__(self, messages: dict[str, bytes]):
        self.messages = messages
        self.searches: list[str] = []
        self.fetches: list[tuple[str, str]] = []
        self.selected: tuple[str, bool] | None = None
        self.logged_in: tuple[str, str] | None = None
        self.closed = False
        self.logged_out = False

    # imaplib's own signatures, as the connector calls them.
    def login(self, user, password):
        self.logged_in = (user, password)
        return "OK", [b"logged in"]

    def select(self, folder, readonly=False):
        self.selected = (folder, readonly)
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            criteria = args[-1]
            self.searches.append(criteria)
            return "OK", [" ".join(self.messages).encode()]
        if command == "FETCH":
            uid, spec = args[0], args[1]
            self.fetches.append((str(uid), spec))
            raw = self.messages[str(uid)]
            return "OK", [(b"%s (BODY[] {%d}" % (str(uid).encode(), len(raw)), raw), b")"]
        raise AssertionError(f"unexpected UID command {command}")

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True


def _factory(fake):
    def build(host, port):
        fake.host, fake.port = host, port
        return fake

    return build


def _message(parts) -> bytes:
    """One multipart message. `parts` are `(filename, content_type, disposition,
    payload)`, and a `None` filename is a body part."""
    msg = EmailMessage()
    msg["From"] = "alerts@slice.co"
    msg["Subject"] = "Your monthly statement"
    msg.set_content("Please find your statement attached.")
    for filename, content_type, disposition, payload in parts:
        maintype, _, subtype = content_type.partition("/")
        msg.add_attachment(
            payload, maintype=maintype, subtype=subtype, filename=filename,
            disposition=disposition,
        )
    return msg.as_bytes()


def _connector(fake, **kwargs):
    options = dict(
        host="imap.example.test",
        port=993,
        username="merchant@example.test",
        password="app-password",
        sender_filter=SENDERS,
        imap_factory=_factory(fake),
    )
    options.update(kwargs)
    return ImapMailboxConnector(**options)


AUGUST = (date(2026, 8, 1), date(2026, 8, 31))


# --- configuration ------------------------------------------------------------


def test_it_is_unavailable_without_credentials():
    """The default configuration of a clone, and not an error."""
    c = ImapMailboxConnector(
        host=None, port=993, username=None, password=None, sender_filter="",
        imap_factory=_factory(FakeIMAP({})),
    )
    assert c.available() is False


def test_an_unconfigured_fetch_names_the_variables_and_never_a_value():
    c = ImapMailboxConnector(
        host=None, port=993, username=None, password="app-password",
        sender_filter="", imap_factory=_factory(FakeIMAP({})),
    )
    with pytest.raises(RuntimeError) as exc:
        c.fetch(*AUGUST)
    message = str(exc.value)
    assert "RECON_IMAP_HOST" in message
    assert "RECON_IMAP_USER" in message
    assert "RECON_IMAP_PASSWORD" in message
    assert "app-password" not in message


def test_a_configured_mailbox_with_no_sender_filter_refuses_to_walk_it_all():
    """An unfiltered SEARCH is every message the merchant has ever received.
    That is not a reconciliation input, it is a mailbox dump."""
    fake = FakeIMAP({})
    with pytest.raises(RuntimeError) as exc:
        _connector(fake, sender_filter="").fetch(*AUGUST)
    assert "RECON_IMAP_SENDERS" in str(exc.value)
    assert fake.searches == []


# --- the two properties that decide whether this is safe to point at an inbox --


def test_the_search_is_scoped_by_both_the_window_and_the_sender():
    fake = FakeIMAP({})
    _connector(fake).fetch(*AUGUST)

    assert len(fake.searches) == 2, "one narrowed search per configured sender"
    for criteria in fake.searches:
        assert "SINCE" in criteria
        assert "BEFORE" in criteria
        assert "FROM" in criteria
    assert '01-Aug-2026' in fake.searches[0]
    # BEFORE is exclusive in IMAP, so the day after the window's last day is
    # what includes the last day.
    assert '01-Sep-2026' in fake.searches[0]
    assert 'alerts@slice.co' in fake.searches[0]
    assert 'statements@hdfcbank.net' in fake.searches[1]


def test_the_fetch_peeks_and_therefore_does_not_mark_the_mail_read():
    """`BODY[]` sets \\Seen as a side effect; `BODY.PEEK[]` does not. The
    difference between reading a mailbox and altering one is this literal
    string, so it is asserted literally."""
    fake = FakeIMAP({"7": _message([("aug.pdf", "application/pdf", "attachment", b"%PDF-1.4 x")])})
    _connector(fake).fetch(*AUGUST)

    assert fake.fetches, "nothing was fetched, so the assertion below is vacuous"
    for _uid, spec in fake.fetches:
        assert "BODY.PEEK[]" in spec
        assert "BODY[]" not in spec.replace("BODY.PEEK[]", "")
    # And the folder was opened read-only, so the server cannot set the flag
    # even if a future edit forgot the PEEK.
    assert fake.selected == ("INBOX", True)


def test_it_deletes_nothing_and_stores_no_flag():
    fake = FakeIMAP({"7": _message([("aug.pdf", "application/pdf", "attachment", b"%PDF-1.4 x")])})
    _connector(fake).fetch(*AUGUST)
    assert not hasattr(fake, "stored"), "a STORE would be a flag change"
    assert fake.closed and fake.logged_out


# --- what comes back ----------------------------------------------------------


def test_a_pdf_attachment_becomes_one_fetched_file():
    fake = FakeIMAP({
        "7": _message([("slice-aug-2026.pdf", "application/pdf", "attachment",
                        b"%PDF-1.4 statement")]),
    })
    files = _connector(fake).fetch(*AUGUST)
    assert len(files) == 1
    assert files[0].suggested_name == "slice-aug-2026.pdf"
    assert files[0].source_name == "imap-mailbox"
    assert files[0].content == b"%PDF-1.4 statement"
    assert (files[0].window_start, files[0].window_end) == AUGUST


def test_a_message_with_no_attachment_yields_nothing_rather_than_an_error():
    fake = FakeIMAP({"7": _message([])})
    assert _connector(fake).fetch(*AUGUST) == []


def test_an_inline_image_is_not_a_statement():
    """A signature logo is on every message a bank sends. Treating it as a
    statement would put a PNG in the quarantine queue every month and teach a
    merchant that quarantine is noise."""
    fake = FakeIMAP({
        "7": _message([
            ("logo.png", "image/png", "inline", b"\x89PNG\r\n\x1a\n"),
            ("aug.csv", "text/csv", "attachment", b"Date,Narration\n"),
        ]),
    })
    files = _connector(fake).fetch(*AUGUST)
    assert [f.suggested_name for f in files] == ["aug.csv"]


def test_an_attachment_this_layer_cannot_read_is_not_fetched():
    fake = FakeIMAP({
        "7": _message([("terms.docx", "application/octet-stream", "attachment", b"PK\x03\x04")]),
    })
    assert _connector(fake).fetch(*AUGUST) == []


def test_a_traversal_filename_is_flattened_to_a_safe_name():
    """The attachment filename is attacker-controlled: it is whatever the
    sender typed. It reaches a filesystem path downstream, so it is sanitised
    here rather than trusted anywhere."""
    fake = FakeIMAP({
        "7": _message([("../../../etc/passwd.pdf", "application/pdf", "attachment",
                        b"%PDF-1.4 x")]),
    })
    files = _connector(fake).fetch(*AUGUST)
    assert files[0].suggested_name == "passwd.pdf"
    assert "/" not in files[0].suggested_name
    assert ".." not in files[0].suggested_name


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd.pdf", "passwd.pdf"),
        (r"..\..\windows\system32\cfg.csv", "cfg.csv"),
        ("statement aug 2026.pdf", "statementaug2026.pdf"),
        ("....pdf", "pdf"),
        ("!!!.pdf", "pdf"),
        ("///", "statement-9.pdf"),
        ("", "statement-9.pdf"),
    ],
)
def test_sanitise_filename_cannot_produce_a_path_or_an_empty_name(raw, expected):
    assert sanitise_filename(raw, "statement-9.pdf") == expected


# --- encrypted statements -----------------------------------------------------


def _encrypted_pdf(password: str) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _is_encrypted(payload: bytes) -> bool:
    from pypdf import PdfReader

    return PdfReader(io.BytesIO(payload)).is_encrypted


def test_an_encrypted_pdf_is_decrypted_when_the_password_is_configured():
    """Indian bank statement PDFs arrive password-protected as a matter of
    course. `slice-pdf-v1` reads a text layer it cannot reach through the
    encryption, so the decryption has to happen before the adapter sees it."""
    payload = _encrypted_pdf("secretpw")
    assert _is_encrypted(payload), "the fixture would be vacuous unencrypted"
    fake = FakeIMAP({"7": _message([("aug.pdf", "application/pdf", "attachment", payload)])})

    files = _connector(fake, pdf_password="secretpw").fetch(*AUGUST)
    assert len(files) == 1
    assert not _is_encrypted(files[0].content)


def test_an_encrypted_pdf_with_no_password_is_still_delivered_for_quarantine():
    """The load-bearing one. Dropping it would make a statement disappear with
    no record that it ever arrived; raising would abandon every other message
    in the same window. It goes through as-is and the adapter layer refuses it
    where a human can see the refusal."""
    payload = _encrypted_pdf("secretpw")
    fake = FakeIMAP({"7": _message([("aug.pdf", "application/pdf", "attachment", payload)])})

    files = _connector(fake, pdf_password=None).fetch(*AUGUST)
    assert len(files) == 1
    assert files[0].content == payload
    assert _is_encrypted(files[0].content)


def test_an_encrypted_pdf_with_the_wrong_password_is_delivered_rather_than_dropped():
    payload = _encrypted_pdf("secretpw")
    fake = FakeIMAP({"7": _message([("aug.pdf", "application/pdf", "attachment", payload)])})

    files = _connector(fake, pdf_password="not-the-password").fetch(*AUGUST)
    assert len(files) == 1
    assert files[0].content == payload


def test_a_password_never_reaches_an_exception_message():
    """A wrong password is a refusal to shout about; its value is not."""
    payload = b"%PDF-1.4 not really a pdf at all"
    fake = FakeIMAP({"7": _message([("aug.pdf", "application/pdf", "attachment", payload)])})
    files = _connector(fake, pdf_password="hunter2").fetch(*AUGUST)
    assert files[0].content == payload


# --- what is never fetched at all ---------------------------------------------
#
# 2026-09-02, on a real mailbox: `sliceCIBILReportLatest.pdf` -- 326 KB -- came
# down with the statements, because the sender filter matches everything from
# the bank's address and the bank sends credit reports from that same address.
# It failed to decrypt and landed in quarantine, so nothing leaked into a run.
#
# Quarantining it is still too late. By then it has been read out of the
# mailbox, decrypted in memory and written to the blob store. A CIBIL report --
# score, loan history, enquiries -- has no business entering a reconciliation
# process at all, so the filter belongs at fetch. Sender filtering alone is too
# coarse a control for a personal mailbox.

CIBIL = "sliceCIBILReportLatest.pdf"
STATEMENT = "slicebanksavingsstatement-Jul2026.pdf"


def _both_attachments() -> bytes:
    return _message([
        (STATEMENT, "application/pdf", "attachment", b"%PDF-1.4 statement"),
        (CIBIL, "application/pdf", "attachment", b"%PDF-1.4 credit report"),
    ])


def test_a_credit_report_is_never_fetched_even_from_a_matching_sender():
    fake = FakeIMAP({"7": _both_attachments()})
    connector = _connector(fake)
    files = connector.fetch(*AUGUST)
    assert [f.suggested_name for f in files] == [STATEMENT]
    assert b"credit report" not in b"".join(f.content for f in files)


@pytest.mark.parametrize(
    "filename",
    [
        "sliceCIBILReportLatest.pdf",
        "cibil.pdf",
        "CIBIL-2026.pdf",
        "my_creditreport.pdf",
        "credit_report_jul.pdf",
        "credit-report-jul.pdf",
        "Experian-Score.pdf",
        "equifax_summary.pdf",
        "CRIF-highmark.pdf",
    ],
)
def test_the_deny_list_is_case_insensitive_and_matches_anywhere_in_the_name(filename):
    """India-specific by design: this is an India-first product and these are
    the four bureaus a merchant's bank actually mails them reports from."""
    fake = FakeIMAP({
        "7": _message([(filename, "application/pdf", "attachment", b"%PDF-1.4 x")]),
    })
    connector = _connector(fake)
    assert connector.fetch(*AUGUST) == []
    assert len(connector.last_skipped) == 1


def test_a_skipped_attachment_is_counted_and_named_rather_than_dropped():
    """The same argument as the silent zero: an attachment the fetcher declined
    is a fact the merchant is entitled to. A file that vanishes with no record
    is indistinguishable from one the bank never sent."""
    fake = FakeIMAP({"7": _both_attachments()})
    connector = _connector(fake)
    connector.fetch(*AUGUST)
    assert connector.last_skipped == [CIBIL]


def test_the_skipped_list_is_per_fetch_and_not_cumulative():
    fake = FakeIMAP({"7": _both_attachments()})
    connector = _connector(fake)
    connector.fetch(*AUGUST)
    connector.fetch(*AUGUST)
    assert connector.last_skipped == [CIBIL]


def test_a_skipped_name_is_sanitised_like_every_other_attachment_name():
    """It is still a sender-controlled string and it still reaches a screen."""
    fake = FakeIMAP({
        "7": _message([
            ("../../etc/cibil.pdf", "application/pdf", "attachment", b"%PDF-1.4 x"),
        ]),
    })
    connector = _connector(fake)
    connector.fetch(*AUGUST)
    assert connector.last_skipped == ["cibil.pdf"]


def test_a_denied_attachment_is_never_decrypted_or_copied(monkeypatch):
    """"Skipped" has to mean skipped. The deny check runs before the payload is
    decoded, so the bytes of a credit report never reach `decrypt_pdf`, the PDF
    password is never applied to them, and no copy of them is made."""
    from core.connectors import imap_mailbox

    seen: list[bytes] = []

    def spy(payload, password):
        seen.append(payload)
        return payload

    monkeypatch.setattr(imap_mailbox, "decrypt_pdf", spy)
    fake = FakeIMAP({"7": _both_attachments()})
    _connector(fake, pdf_password="statement-pw").fetch(*AUGUST)

    assert seen == [b"%PDF-1.4 statement"]


# --- the optional filename pattern --------------------------------------------


def test_no_pattern_admits_everything_the_deny_list_allows():
    fake = FakeIMAP({
        "7": _message([("anything-at-all.pdf", "application/pdf", "attachment", b"%PDF-x")]),
    })
    connector = _connector(fake, filename_pattern=None)
    assert [f.suggested_name for f in connector.fetch(*AUGUST)] == ["anything-at-all.pdf"]
    assert connector.last_skipped == []


def test_a_statement_pattern_admits_the_statement_and_rejects_the_credit_report():
    """`statement` is the obvious value, and this is what it buys."""
    fake = FakeIMAP({"7": _both_attachments()})
    connector = _connector(fake, filename_pattern="statement")
    assert [f.suggested_name for f in connector.fetch(*AUGUST)] == [STATEMENT]
    assert connector.last_skipped == [CIBIL]


def test_the_pattern_is_a_regex_and_not_a_glob():
    fake = FakeIMAP({
        "7": _message([
            ("aug-2026.pdf", "application/pdf", "attachment", b"%PDF-a"),
            ("summary.pdf", "application/pdf", "attachment", b"%PDF-b"),
        ]),
    })
    connector = _connector(fake, filename_pattern=r"^\w+-\d{4}\.pdf$")
    assert [f.suggested_name for f in connector.fetch(*AUGUST)] == ["aug-2026.pdf"]
    assert connector.last_skipped == ["summary.pdf"]


def test_the_pattern_is_matched_case_insensitively():
    fake = FakeIMAP({
        "7": _message([(STATEMENT.upper(), "application/pdf", "attachment", b"%PDF-x")]),
    })
    connector = _connector(fake, filename_pattern="statement")
    assert len(connector.fetch(*AUGUST)) == 1


def test_the_deny_list_wins_over_a_pattern_that_would_admit_a_credit_report():
    """The deny list is not configuration. A pattern is the merchant narrowing
    what they want; it is not a way to opt back in to a credit report."""
    fake = FakeIMAP({"7": _both_attachments()})
    # A pattern that admits both attachments, so the deny list is the only
    # thing that can separate them.
    connector = _connector(fake, filename_pattern=r"\.pdf$")
    assert [f.suggested_name for f in connector.fetch(*AUGUST)] == [STATEMENT]
    assert connector.last_skipped == [CIBIL]


def test_a_pattern_that_is_not_a_regex_names_the_variable_and_refuses():
    """A mailbox is not searched on a pattern nobody could compile, and the
    message names the variable to fix -- never a credential, and never the
    mailbox."""
    from core.connectors.base import ConnectorUnconfigured

    connector = _connector(FakeIMAP({}), filename_pattern="statement(")
    with pytest.raises(ConnectorUnconfigured) as exc:
        connector.fetch(*AUGUST)
    message = str(exc.value)
    assert "RECON_IMAP_FILENAME_PATTERN" in message
    assert "app-password" not in message


def test_the_pattern_is_wired_from_the_environment_into_the_connector(monkeypatch):
    """The seam between `api/settings.py` and this constructor.

    A settings dict that drifted from the connector's keywords would not fail
    here in a test, it would fail at a merchant's first sync -- so the dict is
    handed to the real registry rather than inspected.
    """
    from api import settings
    from core.connectors.registry import default_connectors

    monkeypatch.setenv(settings.IMAP_FILENAME_PATTERN_ENV, "statement")
    options = settings.imap_settings()
    assert options["filename_pattern"] == "statement"

    built = default_connectors(imap=options)["imap-mailbox"]
    assert built.name == "imap-mailbox"
    assert built.last_skipped == []
