from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from core import db
from core.models import Account


@pytest.fixture
def migration_config(tmp_path):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", "sqlite:///" + (tmp_path / "orbit.sqlite").as_posix())
    return config


@pytest.fixture
def database(migration_config):
    db.configure(migration_config.get_main_option("sqlalchemy.url"))
    try:
        command.upgrade(migration_config, "head")
        yield db.get_engine()
    finally:
        # Release Windows file handles before pytest removes temporary files.
        db.configure(None)


@pytest.fixture
def session(database):
    with Session(database) as active:
        try:
            yield active
        finally:
            active.rollback()


@pytest.fixture
def account_factory(session):
    def make_account(**values):
        values.setdefault("name", "Example account")
        values.setdefault("type", "cash")
        account = Account(**values)
        session.add(account)
        session.flush()
        return account

    return make_account
