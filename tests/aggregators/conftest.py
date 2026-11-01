from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

from app.main import create_app
from core import db
from core.config import Settings, get_settings
from core.crypto import encrypt
from core.models import Institution, PlaidItem
from tests.csrf_client import TestClient

from .fake import FakePlaidClient


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("PLAID_CLIENT_ID", "test-client")
    monkeypatch.setenv("PLAID_SECRET", "private-plaid-secret")
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    monkeypatch.setenv("ORBIT_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("PLAID_WEBHOOK_URL", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def database(tmp_path):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    url = "sqlite:///" + (tmp_path / "orbit.sqlite").as_posix()
    config.set_main_option("sqlalchemy.url", url)
    db.configure(url)
    try:
        command.upgrade(config, "head")
        yield db.get_engine()
    finally:
        db.configure(None)


@pytest.fixture
def fake():
    return FakePlaidClient()


@pytest.fixture
def item_factory(database, configured):
    def create(external_id="remote-item", token="private-access-token", institution_id=None):
        with db.session_scope() as session:
            if institution_id is None:
                institution = Institution(
                    name="Example Bank", aggregator="plaid", external_id="ins_example"
                )
                session.add(institution)
                session.flush()
                institution_id = institution.id
            item = PlaidItem(
                item_id=external_id,
                institution_id=institution_id,
                access_token_encrypted=encrypt(token),
            )
            session.add(item)
            session.flush()
            return item.id

    return create


@pytest.fixture
def item_id(item_factory):
    return item_factory()


@pytest.fixture
def web_client(database, configured, fake, monkeypatch):
    monkeypatch.setattr("aggregators.plaid.PlaidClient", lambda: fake)
    monkeypatch.setattr("app.routers.plaid_webhook.PlaidClient", lambda: fake)
    with TestClient(create_app()) as client:
        yield client
