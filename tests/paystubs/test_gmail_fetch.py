import hashlib
import re
import ssl
from dataclasses import replace
from datetime import date
from email.message import EmailMessage

import pytest

from paystubs.config import Settings
from paystubs.gmail_fetch import MAX_ATTACHMENT_BYTES, fetch_once


def message_bytes(attachments):
    message = EmailMessage()
    message["From"] = "payroll@test.invalid"
    message["To"] = "fake@test.invalid"
    message["Subject"] = "Synthetic payroll only"
    message.set_content("Synthetic test message")
    for name, kind, payload in attachments:
        main, subtype = kind.split("/")
        message.add_attachment(payload, maintype=main, subtype=subtype, filename=name)
    return message.as_bytes()


class FakeImap:
    def __init__(self, raw=b"", fail=None):
        self.raw = raw
        self.fail = fail
        self.calls = []
        self.logged_out = False

    def login(self, user, password):
        self.calls.append(("login", user))
        if self.fail == "login":
            raise RuntimeError(f"Rejected password {password}")
        return "OK", []

    def select(self, mailbox, readonly=False):
        assert readonly is True
        self.calls.append(("select", mailbox, readonly))
        return ("NO" if self.fail == "select" else "OK"), []

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "search":
            if self.fail == "search":
                raise RuntimeError("fake-secret")
            return "OK", [b"17"]
        assert command == "fetch"
        assert args == (b"17", "(BODY.PEEK[])")
        if self.fail == "fetch":
            return "NO", []
        return "OK", [(b"17 (BODY[])", self.raw), b")"]

    def store(self, *args):
        pytest.fail("IMAP must never store flags")

    def expunge(self, *args):
        pytest.fail("IMAP must never expunge")

    def logout(self):
        self.logged_out = True
        if self.fail == "logout":
            raise RuntimeError("fake-secret")
        return "BYE", []


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        imap_host="imap.test.invalid",
        imap_user="fake-user",
        imap_password="fake-secret",
        imap_sender=("one@test.invalid", "two@test.invalid", "three@test.invalid"),
    )


def factory_for(client):
    def factory(host, port, ssl_context, timeout):
        assert timeout == 30
        assert host == "imap.test.invalid"
        assert port == 993
        assert isinstance(ssl_context, ssl.SSLContext)
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert ssl_context.check_hostname
        return client

    return factory


def test_readonly_tls_search_filters_and_hostile_filename(settings):
    payload = b"%PDF-synthetic"
    client = FakeImap(message_bytes([("../../..\\pay file?.PDF", "application/pdf", payload)]))
    result = fetch_once(settings, lambda _: False, date(2026, 10, 31), factory_for(client))
    assert result.errors == []
    assert result.skipped == 0
    assert len(result.saved) == 1
    path = result.saved[0]
    assert path.parent == settings.inbox_dir
    assert re.fullmatch(r"[a-f0-9]{12}_pay_file_\.pdf", path.name)
    assert path.name.startswith(hashlib.sha256(payload).hexdigest()[:12])
    assert path.read_bytes() == payload
    assert not list(settings.inbox_dir.glob("*.part"))
    assert ("select", "INBOX", True) in client.calls
    search = next(call for call in client.calls if call[0] == "search")
    assert search == (
        "search",
        None,
        'SINCE 16-Sep-2026 (OR FROM "one@test.invalid" OR FROM "two@test.invalid" '
        'FROM "three@test.invalid")',
    )
    assert client.logged_out


def test_label_only_and_encoded_filename(settings):
    client = FakeImap(message_bytes([("résumé.pdf", "application/octet-stream", b"%PDF-test")]))
    result = fetch_once(
        replace(settings, imap_sender=(), imap_label="Payroll Reports"),
        lambda _: False,
        date(2026, 10, 31),
        factory_for(client),
    )
    assert result.errors == []
    assert len(result.saved) == 1
    assert result.saved[0].name.endswith("_r_sum_.pdf")
    assert ("select", '"Payroll Reports"', True) in client.calls
    search = next(call for call in client.calls if call[0] == "search")
    assert search[2] == "SINCE 16-Sep-2026"


def test_non_pdf_type_bad_signature_and_octet_extension(settings):
    client = FakeImap(
        message_bytes(
            [
                ("text.pdf", "text/plain", b"%PDF-disguised"),
                ("bad.pdf", "application/pdf", b"not a PDF"),
                ("unknown.bin", "application/octet-stream", b"%PDF-fake"),
                ("valid.PDF", "application/octet-stream", b"%PDF-valid"),
            ]
        )
    )
    result = fetch_once(settings, lambda _: False, imap_factory=factory_for(client))
    assert len(result.saved) == 1
    assert result.skipped == 3
    assert result.errors == []


def test_seen_hash_and_existing_file_skipped(settings):
    payload = b"%PDF-dedupe"
    raw = message_bytes([("a.pdf", "application/pdf", payload)])
    seen = []

    def already_seen(digest):
        seen.append(digest)
        return True

    result = fetch_once(settings, already_seen, imap_factory=factory_for(FakeImap(raw)))
    assert seen == [hashlib.sha256(payload).hexdigest()]
    assert result.saved == [] and result.skipped == 1
    first = fetch_once(settings, lambda _: False, imap_factory=factory_for(FakeImap(raw)))
    second = fetch_once(settings, lambda _: False, imap_factory=factory_for(FakeImap(raw)))
    assert len(first.saved) == 1
    assert second.saved == [] and second.skipped == 1


def test_attachment_cap(settings, monkeypatch):
    assert MAX_ATTACHMENT_BYTES == 25 * 1024 * 1024
    monkeypatch.setattr("paystubs.gmail_fetch.MAX_ATTACHMENT_BYTES", 10)
    client = FakeImap(message_bytes([("large.pdf", "application/pdf", b"%PDF-" + b"x" * 20)]))
    result = fetch_once(settings, lambda _: False, imap_factory=factory_for(client))
    assert result.skipped == 1
    assert result.saved == []


@pytest.mark.parametrize(
    "sender,label",
    [
        ((), ""),
        (('bad"sender',), ""),
        (("bad\r\nsender",), ""),
        ((), 'bad"label'),
        ((), "bad\nlabel"),
        ((" ",), ""),
    ],
)
def test_refuses_unfiltered_or_injected_search(settings, sender, label):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid filters must be rejected before connection")

    with pytest.raises(ValueError):
        fetch_once(
            replace(settings, imap_sender=sender, imap_label=label),
            lambda _: False,
            imap_factory=forbidden,
        )


@pytest.mark.parametrize("failure", ["login", "select", "search", "fetch", "logout"])
def test_failures_are_redacted_and_always_logout(settings, failure, caplog):
    client = FakeImap(message_bytes([]), fail=failure)
    result = fetch_once(settings, lambda _: False, imap_factory=factory_for(client))
    assert result.errors
    assert "fake-secret" not in repr(result)
    assert "fake-secret" not in caplog.text
    assert client.logged_out


def test_connection_exception_redacted(settings):
    def broken(*args, **kwargs):
        raise RuntimeError("fake-secret")

    result = fetch_once(settings, lambda _: False, imap_factory=broken)
    assert result.errors == ["IMAP connection or fetch failed"]


def test_partial_file_collision_does_not_overwrite(settings):
    payload = b"%PDF-test"
    digest = hashlib.sha256(payload).hexdigest()
    settings.inbox_dir.mkdir(parents=True)
    partial = settings.inbox_dir / f"{digest[:12]}_a.pdf.part"
    partial.write_bytes(b"in-progress")
    client = FakeImap(message_bytes([("a.pdf", "application/pdf", payload)]))
    result = fetch_once(settings, lambda _: False, imap_factory=factory_for(client))
    assert result.errors
    assert result.saved == []
    assert partial.read_bytes() == b"in-progress"
