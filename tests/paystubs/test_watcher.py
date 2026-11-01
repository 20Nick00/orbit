import threading
import time

import pytest

from paystubs.watcher import PaystubWatcher


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), "Timed out waiting for file processing"


@pytest.mark.parametrize("polling", [False, True])
def test_real_observer_sweep_moves_slow_write_and_exception(tmp_path, caplog, polling):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "old.PDF").write_bytes(b"%PDF-old")
    (inbox / "ignore.pdf.part").write_bytes(b"unfinished")
    (inbox / "directory.pdf").mkdir()
    processed = []
    contents = {}
    thread_ids = set()

    def on_file(path):
        thread_ids.add(threading.get_ident())
        contents[path.name] = path.read_bytes()
        processed.append(path.name)
        if path.name == "bad.pdf":
            raise RuntimeError("private pay text 1234.56")

    with PaystubWatcher(inbox, on_file, polling=polling) as watcher:
        wait_until(lambda: "old.PDF" in processed)
        with (inbox / "slow.PDF").open("wb") as stream:
            for chunk in (b"%PDF-", b"first", b"second", b"last"):
                stream.write(chunk)
                stream.flush()
                time.sleep(0.3)
        (inbox / "bad.pdf").write_bytes(b"%PDF-bad")
        part = inbox / "moved.pdf.part"
        part.write_bytes(b"%PDF-complete")
        part.rename(inbox / "moved.pdf")
        wait_until(lambda: len(processed) >= 4)
        (inbox / "after.pdf").write_bytes(b"%PDF-after")
        wait_until(lambda: "after.pdf" in processed)
        assert watcher._worker.is_alive()
    assert sorted(processed) == ["after.pdf", "bad.pdf", "moved.pdf", "old.PDF", "slow.PDF"]
    assert contents["slow.PDF"] == b"%PDF-firstsecondlast"
    assert len(thread_ids) == 1
    assert "private pay" not in caplog.text and "1234.56" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_duplicate_events_do_not_requeue_running_file(tmp_path):
    path = tmp_path / "single.pdf"
    path.write_bytes(b"%PDF-fake")
    running, release = threading.Event(), threading.Event()
    calls = []

    def on_file(item):
        calls.append(item)
        running.set()
        release.wait(5)

    watcher = PaystubWatcher(tmp_path, on_file)
    try:
        watcher.start()
        assert running.wait(5)
        for _ in range(5):
            watcher._enqueue(str(path))
        assert watcher._queue.empty()
    finally:
        release.set()
        watcher.stop()
    assert calls == [path]


def test_missing_inbox_created_and_stop_interrupts_readiness(tmp_path):
    inbox = tmp_path / "new"
    watcher = PaystubWatcher(inbox, lambda _: None)
    watcher.start()
    assert inbox.is_dir()
    (inbox / "copy.pdf").write_bytes(b"%PDF-")
    watcher.stop()
    watcher.stop()
    assert watcher._worker is None


def test_readiness_timeout_does_not_log_filename(tmp_path, monkeypatch, caplog):
    watcher = PaystubWatcher(tmp_path, lambda _: None)
    times = iter([0, 1, 61])
    monkeypatch.setattr("paystubs.watcher.time.monotonic", lambda: next(times))
    monkeypatch.setattr(watcher._stop, "wait", lambda _: False)
    assert not watcher._ready(tmp_path / "missing.pdf")
    assert "FileReadyTimeout" in caplog.text
    assert "missing.pdf" not in caplog.text
