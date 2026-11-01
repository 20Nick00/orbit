from core.db import make_engine


def test_make_engine_creates_parent_folder(tmp_path):
    database = tmp_path / "nested" / "data" / "orbit.sqlite"
    assert not database.parent.exists()

    engine = make_engine(f"sqlite:///{database}")
    try:
        assert database.parent.is_dir()
    finally:
        engine.dispose()


def test_sqlite_pragmas(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'orbit.sqlite'}")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
    finally:
        engine.dispose()
