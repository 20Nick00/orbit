"""Read-only, filtered IMAP PDF retrieval with injectable connection and dedupe."""

import hashlib
import imaplib
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from pathlib import Path

from .config import Settings

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class FetchResult:
    saved: list[Path] = field(default_factory=list)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _filename(value: str | None, digest: str) -> str:
    decoded = str(make_header(decode_header(value or "attachment.pdf")))
    base = decoded.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip(".") or "attachment"
    stem = base[:-4] if base.lower().endswith(".pdf") else base
    return f"{digest[:12]}_{stem[:100]}.pdf"


def _search(settings: Settings, today: date, since: date | None = None) -> str:
    since = since or today - timedelta(days=settings.imap_lookback_days)
    criteria = f"SINCE {since.day:02d}-{MONTHS[since.month - 1]}-{since.year}"
    senders = ['FROM "' + sender.replace("\\", "\\\\") + '"' for sender in settings.imap_sender]
    if senders:
        combined = senders[-1]
        for sender in reversed(senders[:-1]):
            combined = f"OR {sender} {combined}"
        criteria += f" ({combined})"
    return criteria


def fetch_once(
    settings: Settings,
    already_seen: Callable[[str], bool],
    today: date | None = None,
    imap_factory: Callable = imaplib.IMAP4_SSL,
    since: date | None = None,
) -> FetchResult:
    if not settings.imap_sender and not settings.imap_label:
        raise ValueError("Configure an IMAP sender or label before fetching")
    if any(
        any(c in value for c in ('"', "\r", "\n"))
        for value in (*settings.imap_sender, settings.imap_label)
    ):
        raise ValueError("IMAP sender and label cannot contain quotes or line breaks")
    if any(not sender.strip() for sender in settings.imap_sender):
        raise ValueError("IMAP sender cannot be empty")
    if not settings.imap_enabled:
        raise ValueError("IMAP host, user and password are required")
    result = FetchResult()
    client = None
    try:
        client = imap_factory(
            settings.imap_host,
            settings.imap_port,
            ssl_context=ssl.create_default_context(),
            timeout=30,
        )
        if client.login(settings.imap_user, settings.imap_password)[0] != "OK":
            result.errors.append("IMAP login failed")
            return result
        mailbox = (
            '"' + settings.imap_label.replace("\\", "\\\\") + '"'
            if settings.imap_label
            else "INBOX"
        )
        if client.select(mailbox, readonly=True)[0] != "OK":
            result.errors.append("IMAP mailbox could not be opened")
            return result
        status, data = client.uid("search", None, _search(settings, today or date.today(), since))
        if status != "OK":
            result.errors.append("IMAP search failed")
            return result
        settings.inbox_dir.mkdir(parents=True, exist_ok=True)
        for uid in b" ".join(part for part in data if isinstance(part, bytes)).split():
            try:
                status, fetched = client.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK":
                    result.errors.append("IMAP message fetch failed")
                    continue
                raw = next(
                    (
                        part[1]
                        for part in fetched
                        if isinstance(part, tuple) and isinstance(part[1], bytes)
                    ),
                    None,
                )
                if raw is None:
                    result.errors.append("IMAP message was empty")
                    continue
                message = BytesParser(policy=policy.default).parsebytes(raw)
                for part in message.walk():
                    if part.is_multipart():
                        continue
                    name = part.get_filename()
                    kind = part.get_content_type()
                    if kind != "application/pdf" and not (
                        kind == "application/octet-stream"
                        and name
                        and name.lower().endswith(".pdf")
                    ):
                        if name or part.get_content_disposition() == "attachment":
                            result.skipped += 1
                        continue
                    payload = part.get_payload(decode=True)
                    if (
                        not payload
                        or not payload.startswith(b"%PDF-")
                        or len(payload) > MAX_ATTACHMENT_BYTES
                    ):
                        result.skipped += 1
                        continue
                    digest = hashlib.sha256(payload).hexdigest()
                    target = settings.inbox_dir / _filename(name, digest)
                    if already_seen(digest) or target.exists():
                        result.skipped += 1
                        continue
                    temporary = target.with_suffix(target.suffix + ".part")
                    created = False
                    try:
                        with temporary.open("xb") as stream:
                            created = True
                            stream.write(payload)
                        temporary.rename(target)
                        result.saved.append(target)
                    finally:
                        if created and temporary.exists():
                            temporary.unlink()
            except Exception:
                result.errors.append("IMAP message could not be processed")
    except Exception:
        result.errors.append("IMAP connection or fetch failed")
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                result.errors.append("IMAP logout failed")
    return result


if __name__ == "__main__":
    import argparse

    from .config import ROOT, load_env_file
    from .store import known_sha

    parser = argparse.ArgumentParser(description="Fetch paystub PDF attachments once")
    parser.add_argument("--once", action="store_true", required=True)
    parser.parse_args()
    load_env_file(ROOT / ".env")
    try:
        result = fetch_once(Settings.from_env(), known_sha)
        print(
            f"Saved: {len(result.saved)}; skipped: {result.skipped}; errors: {len(result.errors)}"
        )
    except Exception:
        print("Saved: 0; skipped: 0; errors: 1")
