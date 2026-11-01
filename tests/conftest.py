import os

import pytest

from tests.csrf_client import TestClient

# These must be set before app.main creates its module-level app.
os.environ["PAYSTUB_BACKGROUND"] = "0"
os.environ["ORBIT_SCHEDULER_ENABLED"] = "0"
os.environ["ORBIT_ALLOWED_HOSTS"] = "127.0.0.1,localhost,testserver"

from app.main import create_app
from core.config import Settings, get_settings
from core.db import get_engine


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Never read a real .env or allow the background scheduler/network in tests."""
    import httpx

    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    monkeypatch.setenv("ORBIT_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("ORBIT_WEBHOOK_PORT", "")
    monkeypatch.setenv("ORBIT_WEBHOOK_BIND", "127.0.0.1")
    monkeypatch.setenv("RENTCAST_API_KEY", "")
    monkeypatch.setenv("ORBIT_RENTCAST_MONTHLY_LIMIT", "45")
    monkeypatch.setenv("ORBIT_BACKUP_KEEP_DAYS", "30")
    monkeypatch.setenv("ORBIT_TIMEZONE", "UTC")
    for key in ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_WEBHOOK_URL", "FERNET_KEY"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    get_settings.cache_clear()

    def deny_network(*args, **kwargs):
        raise AssertionError("Tests must use MockTransport, never the real network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_network)
    yield
    get_settings.cache_clear()


@pytest.fixture
def rollup_session(tmp_path):
    from sqlalchemy.orm import Session

    from core.db import make_engine
    from core.models import Base

    engine = make_engine(f"sqlite:///{tmp_path / 'rollups.sqlite'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


@pytest.fixture
def demo_session(rollup_session):
    from datetime import date

    from core.demo import seed_demo

    seed_demo(rollup_session, date(2026, 10, 31))
    rollup_session.commit()
    return rollup_session


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_DATABASE_URL", f"sqlite:///{tmp_path / 'orbit.sqlite'}")
    get_engine.cache_clear()
    get_settings.cache_clear()
    engine = get_engine()
    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        engine.dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()
