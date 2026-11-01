import threading
from datetime import date

import pytest

from paystubs.schedule import MonthlyJob, is_due


@pytest.mark.parametrize(
    "today,day,success,attempt,expected",
    [
        ("2026-01-01", 1, None, None, True),
        ("2026-01-10", 1, "2025-12", None, True),
        ("2026-01-10", 1, "2026-01", None, False),
        ("2026-01-10", 1, None, "2026-01-10", False),
        ("2026-01-11", 1, None, "2026-01-10", True),
        ("2026-01-10", 15, None, None, False),
        ("2026-01-15", 15, None, None, True),
        ("2026-02-27", 31, None, None, False),
        ("2026-02-28", 31, None, None, True),
        ("2024-02-28", 31, None, None, False),
        ("2024-02-29", 31, None, None, True),
        ("2026-04-30", 31, None, None, True),
        ("2026-02-01", 1, "2026-01", "2026-01-31", True),
    ],
)
def test_due_table(today, day, success, attempt, expected):
    assert (
        is_due(
            date.fromisoformat(today),
            day,
            success,
            date.fromisoformat(attempt) if attempt else None,
        )
        is expected
    )


def test_job_survives_failure_and_stops_promptly(caplog):
    ran = threading.Event()
    calls = []

    def run():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("private content")
        ran.set()

    job = MonthlyJob(lambda: True, run, interval_s=0.02)
    try:
        job.start()
        worker = job._thread
        job.start()
        assert job._thread is worker
        assert worker.daemon
        assert ran.wait(3)
    finally:
        job.stop()
    assert not worker.is_alive()
    assert "private content" not in caplog.text
    assert "ValueError" in caplog.text
    job.stop()


def test_job_checks_before_running():
    checked = threading.Event()
    ran = threading.Event()

    def check():
        checked.set()
        return False

    job = MonthlyJob(check, ran.set, interval_s=3600)
    try:
        job.start()
        assert checked.wait(3)
    finally:
        job.stop()
    assert not ran.is_set()
