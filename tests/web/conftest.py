from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from core.db import get_engine


@pytest.fixture
def client(client):
    """The app on a database built by the real migrations."""
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    database = Path(get_engine().url.database).as_posix()
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")
    return client
