import os
import sqlite3
from contextlib import closing
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.config import Settings, get_settings
from jobs import backup
from jobs.scheduler import build_scheduler

TODAY = date(2026, 10, 31)


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / "orbit.sqlite"
    monkeypatch.setenv("ORBIT_DATABASE_URL", f"sqlite:///{path.as_posix()}")
    monkeypatch.setenv("ORBIT_BACKUP_KEEP_DAYS", "30")
    get_settings.cache_clear()
    monkeypatch.setattr(backup, "local_today", lambda: TODAY)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('committed')")
        connection.commit()
        yield path, connection


def test_online_copy_includes_wal_commits_and_is_readable(source):
    path, connection = source
    assert path.with_name(path.name + "-wal").stat().st_size > 0
    connection.execute("INSERT INTO sample VALUES ('not committed')")
    result = backup.run()
    assert result == path.parent / "backups" / "orbit-2026-10-31.sqlite"
    if os.name == "posix":
        assert result.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(result)) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert copy.execute("SELECT value FROM sample").fetchall() == [("committed",)]


def test_same_day_skip_and_cli_filename(source, capsys):
    _, connection = source
    first = backup.run()
    original = first.read_bytes()
    connection.execute("INSERT INTO sample VALUES ('later')")
    connection.commit()
    assert backup.run() == first
    assert first.read_bytes() == original
    backup.main()
    assert capsys.readouterr().out == "orbit-2026-10-31.sqlite\n"


def test_retention_only_removes_exact_old_backup_names(source, monkeypatch):
    path, _ = source
    # Today's new backup, 2027-01-01 and 2026-10-02 stay; 2026-10-01 is the fourth newest.
    monkeypatch.setenv("ORBIT_BACKUP_KEEP_DAYS", "3")
    get_settings.cache_clear()
    folder = path.parent / "backups"
    folder.mkdir()
    keep = [
        "orbit-2026-10-02.sqlite",
        "orbit-2027-01-01.sqlite",
        "orbit-2026-09-01.sqlite.tmp",
        "other-2026-09-01.sqlite",
        "orbit-2026-99-01.sqlite",
        "orbit-2026-9-01.sqlite",
        "notes.txt",
        "orbit.sqlite",
    ]
    old = "orbit-2026-10-01.sqlite"
    for name in [*keep, old]:
        (folder / name).write_bytes(b"preserve")
    (folder / "orbit-2020-01-01.sqlite").mkdir()
    assert backup.run() is not None
    assert not (folder / old).exists()
    assert all((folder / name).read_bytes() == b"preserve" for name in keep)
    assert (folder / "orbit-2020-01-01.sqlite").is_dir()


@pytest.mark.parametrize("failure", ["backup", "integrity"])
def test_failure_cleans_temp_and_preserves_older_backups(source, monkeypatch, caplog, failure):
    path, _ = source
    folder = path.parent / "backups"
    folder.mkdir()
    older = folder / "orbit-2020-01-01.sqlite"
    older.write_bytes(b"old verified copy")
    real_connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def backup(self, *args, **kwargs):
            if failure == "backup":
                raise RuntimeError("sentinel-private-database")
            return super().backup(*args, **kwargs)

        def execute(self, statement, *args, **kwargs):
            if statement == "PRAGMA integrity_check":
                return SimpleNamespace(fetchall=lambda: [("corrupt",)])
            return super().execute(statement, *args, **kwargs)

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=FailingConnection)

    monkeypatch.setattr(backup.sqlite3, "connect", connect)
    assert backup.run() is None
    assert list(folder.iterdir()) == [older]
    assert older.read_bytes() == b"old verified copy"
    assert "sentinel-private-database" not in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "sqlite://",
        "sqlite:///:memory:",
        "postgresql://host/database",
        "sqlite:///file:memdb?mode=memory&uri=true",
    ],
)
def test_non_file_databases_skip(url, tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORBIT_DATABASE_URL", url)
    get_settings.cache_clear()
    assert backup.run() is None
    assert "skipped" in caplog.text and not list(tmp_path.iterdir())


def test_missing_database_is_not_created(tmp_path, monkeypatch):
    path = tmp_path / "missing.sqlite"
    monkeypatch.setenv("ORBIT_DATABASE_URL", f"sqlite:///{path.as_posix()}")
    get_settings.cache_clear()
    assert backup.run() is None and not path.exists()


def test_setting_minimum_and_default():
    assert Settings(_env_file=None).backup_keep_days == 30
    with pytest.raises(ValidationError):
        Settings(_env_file=None, backup_keep_days=0)


def test_scheduler_backup_runs_without_plaid_and_catches_up():
    settings = Settings(_env_file=None, timezone="America/New_York")
    scheduler = build_scheduler(settings)
    job = scheduler.get_job("backup")
    assert job.func is backup.run
    assert job.coalesce and job.max_instances == 1 and job.misfire_grace_time is None
    assert job.next_run_time is not None
    assert str(job.trigger.timezone) == "America/New_York"
    fields = {field.name: str(field) for field in job.trigger.fields}
    assert fields["hour"] == "1" and fields["minute"] == "30" and fields["day"] == "*"


def test_long_shutdown_does_not_empty_the_folder(source):
    path, _ = source
    folder = path.parent / "backups"
    folder.mkdir()
    old = folder / "orbit-2001-01-01.sqlite"
    old.write_bytes(b"preserve")
    assert backup.run() is not None
    assert old.read_bytes() == b"preserve"


def test_only_old_temp_files_are_cleared(source):
    path, _ = source
    folder = path.parent / "backups"
    folder.mkdir()
    stale, recent = folder / ".orbit-stale.tmp", folder / ".orbit-recent.tmp"
    stale.write_bytes(b"x")
    recent.write_bytes(b"x")
    os.utime(stale, (1, 1))
    assert backup.run() is not None
    assert not stale.exists() and recent.exists()


def test_latest_reports_newest_backup(source):
    assert backup.latest() is None
    created = backup.run()
    assert backup.latest().isoformat() in created.name
