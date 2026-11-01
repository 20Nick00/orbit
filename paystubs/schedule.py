"""Monthly catch-up policy and an interruptible background runner."""

import calendar
import logging
import threading
from collections.abc import Callable
from datetime import date

LOG = logging.getLogger(__name__)


def is_due(
    today: date, day_of_month: int, last_success_month: str | None, last_attempt_date: date | None
) -> bool:
    """last_success_month uses YYYY-MM; persistence belongs to the caller."""
    if not 1 <= day_of_month <= 31:
        raise ValueError("Day of month must be between 1 and 31")
    due_day = min(day_of_month, calendar.monthrange(today.year, today.month)[1])
    return (
        today.day >= due_day
        and last_success_month != today.strftime("%Y-%m")
        and last_attempt_date != today
    )


class MonthlyJob:
    def __init__(
        self, check: Callable[[], bool], run: Callable[[], object], interval_s: float = 3600
    ):
        if interval_s <= 0:
            raise ValueError("Job interval must be positive")
        self.check = check
        self.run = run
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                if self.check():
                    self.run()
            except Exception as exc:
                LOG.warning("%s", type(exc).__name__)
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._work, name="paystub-monthly", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=5)
            self._thread = None
