"""Verified online SQLite backups, independent of optional provider configuration."""

import logging
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import date
from pathlib import Path
from threading import Lock

from sqlalchemy.engine import make_url

from core.clock import local_today
from core.config import get_settings

logger = logging.getLogger(__name__)
BACKUP_NAME = re.compile(r"orbit-(\d{4}-\d{2}-\d{2})\.sqlite\Z")
LOCK = Lock()
TEMP_MAX_AGE_SECONDS = 24 * 60 * 60


def _private(path):
    if os.name == "posix":
        try:
            path.chmod(0o600)
        except OSError as exc:
            logger.warning("Backup permissions failed (%s).", type(exc).__name__)


def _backups(folder):
    """Valid backups, newest first."""
    found = []
    for path in folder.iterdir():
        match = BACKUP_NAME.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            continue
        try:
            found.append((date.fromisoformat(match[1]), path))
        except ValueError:
            continue
    return sorted(found, key=lambda item: item[0], reverse=True)


def _prune(folder, keep):
    # Keep a count, not a date window: a long shutdown or clock jump must not empty the folder.
    for _, path in _backups(folder)[keep:]:
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Backup pruning failed (%s).", type(exc).__name__)


def _clear_old_temp_files(folder):
    # A power cut can leave a full database copy behind; a recent one may still be in use.
    for path in folder.glob(".orbit-*.tmp"):
        try:
            if time.time() - path.stat().st_mtime > TEMP_MAX_AGE_SECONDS:
                path.unlink()
        except OSError as exc:
            logger.warning("Backup cleanup failed (%s).", type(exc).__name__)


def _locations(settings):
    url = make_url(settings.database_url)
    if (
        url.get_backend_name() != "sqlite"
        or url.database in {None, "", ":memory:"}
        or url.database.startswith("file:")
        or url.query.get("mode") == "memory"
    ):
        return None, None
    database = Path(url.database).resolve()
    return database, database.parent / "backups"


def latest():
    """The date of the newest backup, or None; for showing on the Settings page."""
    _, folder = _locations(get_settings())
    if folder is None or not folder.is_dir():
        return None
    found = _backups(folder)
    return found[0][0] if found else None


def _backup(settings, today):
    database, folder = _locations(settings)
    if folder is None:
        logger.warning("Backup skipped: a file-backed SQLite database URL is required.")
        return None
    target = folder / f"orbit-{today.isoformat()}.sqlite"
    if target.is_file():
        return target
    # Never accidentally create an empty source when its configured path is wrong.
    if not database.is_file():
        raise FileNotFoundError
    folder.mkdir(parents=True, exist_ok=True)
    _clear_old_temp_files(folder)
    descriptor, name = tempfile.mkstemp(prefix=".orbit-", suffix=".tmp", dir=folder)
    temporary = Path(name)
    os.close(descriptor)
    try:
        _private(temporary)
        with (
            closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(temporary)) as destination,
        ):
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError("Backup integrity check failed")
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    _prune(folder, settings.backup_keep_days)
    return target


def run():
    try:
        with LOCK:
            return _backup(get_settings(), local_today())
    except Exception as exc:
        logger.warning("SQLite backup failed (%s).", type(exc).__name__)
        return None


def main():
    path = run()
    if path is None:
        raise SystemExit(1)
    print(path.name)


if __name__ == "__main__":
    main()
