from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from app.routers import dashboard, paycheck
from core.db import get_engine
from paystubs.config import Settings


@pytest.fixture(autouse=True)
def import_today(monkeypatch):
    monkeypatch.setattr("aggregators.csv_import.local_today", lambda: date(2026, 10, 31))
    monkeypatch.setattr("aggregators.file_store.local_today", lambda: date(2026, 10, 31))


@pytest.fixture
def client(client, monkeypatch, tmp_path):
    """Use the same real-migration setup as tests/web/conftest.py."""
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    database = Path(get_engine().url.database).as_posix()
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        paycheck,
        "settings_for_request",
        lambda: Settings(data_dir=tmp_path / "data", background=False),
    )
    client.app.dependency_overrides[dashboard.get_today] = lambda: date(2026, 10, 31)
    return client


@pytest.fixture
def session(client):
    with Session(get_engine()) as session:
        yield session


@pytest.fixture
def fixture_bytes():
    folder = Path(__file__).resolve().parents[1] / "fixtures" / "imports"
    return lambda name: (folder / name).read_bytes()
