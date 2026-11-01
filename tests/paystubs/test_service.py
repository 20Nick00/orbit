from dataclasses import replace
from datetime import date

from sqlalchemy.orm import Session

from core.db import get_engine
from core.models import PaystubJobState
from paystubs.gmail_fetch import FetchResult
from paystubs.service import JOB_NAME, MailboxJob, start_background


def test_background_disabled_starts_nothing(paystub_env, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Background work was started")

    monkeypatch.setattr("paystubs.service.PaystubWatcher", forbidden)
    monkeypatch.setattr("paystubs.service.MonthlyJob", forbidden)
    handle = start_background(replace(paystub_env, background=True))
    assert handle.watcher is None and handle.monthly is None
    handle.stop()


def test_background_owns_and_stops_only_enabled_jobs(paystub_env, monkeypatch):
    monkeypatch.setenv("PAYSTUB_BACKGROUND", "1")
    created = []

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            self.started = False
            self.stopped = False
            created.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    monkeypatch.setattr("paystubs.service.PaystubWatcher", FakeWorker)
    monkeypatch.setattr("paystubs.service.MonthlyJob", FakeWorker)
    handle = start_background(replace(paystub_env, background=True))
    assert len(created) == 1 and created[0].started
    handle.stop()
    settings = replace(
        paystub_env,
        background=True,
        imap_host="test.invalid",
        imap_user="fake",
        imap_password="fake-secret",
        imap_sender=("payroll@test.invalid",),
    )
    handle = start_background(settings)
    assert len(created) == 3 and all(worker.started for worker in created)
    handle.stop()
    assert all(worker.stopped for worker in created)


def test_job_persists_attempt_success_and_retry_policy(paystub_env, monkeypatch):
    job = MailboxJob(replace(paystub_env, imap_day_of_month=15))
    assert not job.check(date(2026, 10, 14))
    assert job.check(date(2026, 10, 15))
    monkeypatch.setattr(
        "paystubs.service.fetch_once",
        lambda settings, seen, **kwargs: FetchResult(saved=[settings.inbox_dir / "fake.pdf"]),
    )
    job.run(date(2026, 10, 15))
    with Session(get_engine()) as session:
        state = session.get(PaystubJobState, JOB_NAME)
        assert state.last_attempt_date == date(2026, 10, 15)
        assert state.last_success_month == "2026-10"
        assert state.last_saved_count == 1 and state.last_error is None
    assert not job.check(date(2026, 10, 31))
    monkeypatch.setattr(
        "paystubs.service.fetch_once",
        lambda settings, seen, **kwargs: FetchResult(errors=["IMAP login failed"]),
    )
    job.run(date(2026, 11, 15))
    assert not job.check(date(2026, 11, 15))
    assert job.check(date(2026, 11, 16))
    with Session(get_engine()) as session:
        state = session.get(PaystubJobState, JOB_NAME)
        assert state.last_success_month == "2026-10"
        assert state.last_error == "IMAP login failed"
        assert state.last_saved_count == 0


def test_job_redacts_unexpected_error_and_exception(paystub_env, monkeypatch):
    job = MailboxJob(replace(paystub_env, imap_password="fake-secret"))

    def fail(settings, seen, **kwargs):
        raise RuntimeError("Rejected fake-secret")

    for fetch in (
        fail,
        lambda settings, seen, **kwargs: FetchResult(errors=["Rejected fake-secret"]),
    ):
        monkeypatch.setattr("paystubs.service.fetch_once", fetch)
        job.run(date(2026, 10, 31))
        with Session(get_engine()) as session:
            state = session.get(PaystubJobState, JOB_NAME)
            assert state.last_success_month is None
            assert state.last_error == "IMAP connection or fetch failed"
            assert "fake-secret" not in state.last_error
        assert not job.check(date(2026, 10, 31))
        assert job.check(date(2026, 11, 1))


def test_failed_start_stops_started_watcher(paystub_env, monkeypatch):
    monkeypatch.setenv("PAYSTUB_BACKGROUND", "1")
    stopped = []

    class BrokenWatcher:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise OSError("simulated startup failure")

        def stop(self):
            stopped.append(True)

    monkeypatch.setattr("paystubs.service.PaystubWatcher", BrokenWatcher)
    from app.main import create_app
    from tests.csrf_client import TestClient

    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/dashboard").status_code == 200
    assert stopped == [True]
