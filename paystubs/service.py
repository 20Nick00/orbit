"""App-owned watcher and persisted, retryable monthly mailbox job."""

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from core.db import get_engine
from core.models import PaystubJobState

from .config import ROOT, Settings, load_env_file
from .gmail_fetch import fetch_once
from .schedule import MonthlyJob, is_due
from .store import ingest_file, known_sha
from .watcher import PaystubWatcher

LOG = logging.getLogger(__name__)
JOB_NAME = "paystub-imap"
SAFE_ERRORS = {
    "IMAP login failed",
    "IMAP mailbox could not be opened",
    "IMAP search failed",
    "IMAP message fetch failed",
    "IMAP message was empty",
    "IMAP message could not be processed",
    "IMAP connection or fetch failed",
    "IMAP logout failed",
}


class MailboxJob:
    def __init__(self, settings):
        self.settings = settings

    def check(self, today=None):
        with Session(get_engine()) as session:
            state = session.get(PaystubJobState, JOB_NAME)
            return is_due(
                today or date.today(),
                self.settings.imap_day_of_month,
                state.last_success_month if state else None,
                state.last_attempt_date if state else None,
            )

    def run(self, today=None):
        today = today or date.today()
        with Session(get_engine()) as session:
            state = session.get(PaystubJobState, JOB_NAME)
            if state is None:
                state = PaystubJobState(name=JOB_NAME)
                session.add(state)
            state.last_attempt_date = today
            session.commit()
            try:
                since = today - timedelta(days=self.settings.imap_lookback_days)
                if state.last_success_month:
                    since = min(
                        since,
                        date.fromisoformat(state.last_success_month + "-01") - timedelta(days=7),
                    )
                result = fetch_once(self.settings, known_sha, today=today, since=since)
                # Only fixed, known messages can reach storage; exceptions may contain credentials.
                errors = [
                    error if error in SAFE_ERRORS else "IMAP connection or fetch failed"
                    for error in result.errors
                ]
                state.last_saved_count = len(result.saved)
                state.last_error = "; ".join(dict.fromkeys(errors)) or None
                if not errors:
                    state.last_success_month = today.strftime("%Y-%m")
            except Exception:
                state.last_error = "IMAP connection or fetch failed"
                state.last_saved_count = 0
            session.commit()


@dataclass
class BackgroundHandle:
    watcher: PaystubWatcher | None = None
    monthly: MonthlyJob | None = None

    def stop(self):
        try:
            if self.monthly is not None:
                self.monthly.stop()
        finally:
            if self.watcher is not None:
                self.watcher.stop()


def start_background(settings=None):
    load_env_file(ROOT / ".env")
    settings = settings or Settings.from_env()
    handle = BackgroundHandle()
    if not settings.background or os.environ.get("PAYSTUB_BACKGROUND") == "0":
        return handle
    handle.watcher = PaystubWatcher(
        settings.inbox_dir,
        on_file=lambda path: ingest_file(path, settings),
        polling=settings.watch_polling,
        extra_dirs=[settings.data_dir / "paystubs" / "uploads"],
    )
    try:
        handle.watcher.start()
        if settings.imap_enabled:
            job = MailboxJob(settings)
            handle.monthly = MonthlyJob(job.check, job.run)
            handle.monthly.start()
    except Exception:
        handle.stop()
        raise
    return handle


def stop(handle):
    handle.stop()
