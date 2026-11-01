from collections.abc import Generator, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from core.config import get_settings

_configured_url: str | None = None


def configure(url: str | None) -> None:
    """Switch database and dispose the old engine; None returns to settings."""
    global _configured_url
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    _configured_url = url


def get_url() -> str:
    return _configured_url or get_settings().database_url


def make_engine(url: str) -> Engine:
    database_url = make_url(url)
    is_sqlite = database_url.get_backend_name() == "sqlite"
    if (
        is_sqlite
        and database_url.database not in (None, "", ":memory:")
        and database_url.query.get("mode") != "memory"
    ):
        Path(database_url.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(database_url, hide_parameters=True)
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, connection_record):
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

    return engine


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_url())


def get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Commit on success, roll back on any error, and always close."""
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def write_savepoint(session):
    """Keep savepoints inside the caller's transaction with sqlite3 legacy mode.

    SQLite's Python driver does not BEGIN on SELECT or SAVEPOINT in legacy mode.
    Without an explicit BEGIN, releasing the first savepoint would commit writes
    even if the caller subsequently rolled back its SQLAlchemy session.
    """
    connection = session.connection()
    if connection.dialect.name == "sqlite":
        driver = connection.connection.driver_connection
        if not driver.in_transaction:
            connection.exec_driver_sql("BEGIN")
    with session.begin_nested():
        yield
