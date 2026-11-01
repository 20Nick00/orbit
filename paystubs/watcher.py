"""Serialize completed PDF drops onto one background worker."""

import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileSystemMovedEvent
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

LOG = logging.getLogger(__name__)


class _Handler(FileSystemEventHandler):
    def __init__(self, enqueue: Callable[[str], None]):
        self.enqueue = enqueue

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.enqueue(os.fsdecode(event.src_path))

    def on_moved(self, event: FileSystemMovedEvent) -> None:
        if not event.is_directory:
            self.enqueue(os.fsdecode(event.dest_path))


class PaystubWatcher:
    def __init__(
        self,
        inbox_dir: str | Path,
        on_file: Callable[[Path], None],
        polling: bool = False,
        sweep_interval: float = 300,
        extra_dirs: list[Path] | None = None,
    ):
        if sweep_interval <= 0:
            raise ValueError("Sweep interval must be positive")
        self.inbox_dir = Path(inbox_dir)
        self.directories = [self.inbox_dir, *(Path(path) for path in (extra_dirs or []))]
        self.sweep_interval = sweep_interval
        self.on_file = on_file
        self.polling = polling
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, Path]] = queue.Queue()
        self._pending: set[str] = set()
        self._observer = None
        self._worker: threading.Thread | None = None

    def _enqueue(self, name: str) -> None:
        path = Path(name)
        if path.suffix.lower() != ".pdf" or path.is_dir() or self._stop.is_set():
            return
        key = os.path.normcase(str(path.resolve()))
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)
            self._queue.put((key, path))

    def _ready(self, path: Path) -> bool:
        deadline = time.monotonic() + 60
        previous = None
        stable = 0
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                size = path.stat().st_size
                with path.open("rb") as stream:
                    stream.read(1)
                stable = stable + 1 if size == previous else 1
                previous = size
                if stable >= 3:
                    return True
            except OSError:
                stable, previous = 0, None
            self._stop.wait(0.5)
        if not self._stop.is_set():
            LOG.warning("Paystub: FileReadyTimeout")
        return False

    def _work(self) -> None:
        next_sweep = time.monotonic() + self.sweep_interval
        while not self._stop.is_set():
            if time.monotonic() >= next_sweep:
                self._sweep()
                next_sweep = time.monotonic() + self.sweep_interval
            try:
                key, path = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self._ready(path) and not self._stop.is_set():
                    self.on_file(path)
            except Exception as exc:
                LOG.warning("Paystub: %s", type(exc).__name__)
            finally:
                with self._lock:
                    self._pending.discard(key)
                self._queue.task_done()

    def _sweep(self) -> None:
        for directory in self.directories:
            try:
                for path in directory.iterdir():
                    self._enqueue(str(path))
            except OSError as exc:
                LOG.warning("Paystub sweep: %s", type(exc).__name__)

    def start(self) -> None:
        if self._worker is not None:
            return
        for directory in self.directories:
            directory.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._queue = queue.Queue()
        self._pending.clear()
        self._sweep()
        self._observer = (PollingObserver if self.polling else Observer)()
        for directory in self.directories:
            self._observer.schedule(_Handler(self._enqueue), str(directory), recursive=False)
        try:
            self._observer.start()
            # Catch files created between the first sweep and observer startup.
            self._sweep()
            self._worker = threading.Thread(target=self._work, name="paystub-worker", daemon=True)
            self._worker.start()
        except Exception:
            self._observer.stop()
            if self._observer.is_alive():
                self._observer.join(timeout=5)
            self._observer = None
            self._worker = None
            raise

    def stop(self) -> None:
        self._stop.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        if self._worker is not None:
            if self._worker is not threading.current_thread():
                self._worker.join(timeout=5)
            self._worker = None

    def __enter__(self) -> "PaystubWatcher":
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()


if __name__ == "__main__":
    from .config import ROOT, Settings, load_env_file
    from .store import ingest_file

    load_env_file(ROOT / ".env")
    settings = Settings.from_env()
    with PaystubWatcher(
        settings.inbox_dir,
        lambda path: ingest_file(path, settings),
        polling=settings.watch_polling,
        extra_dirs=[settings.data_dir / "paystubs" / "uploads"],
    ):
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
