from pathlib import Path

from alembic import command
from alembic.config import Config

from core.db import make_engine


def test_migrations_have_no_model_drift(tmp_path):
    url = f"sqlite:///{tmp_path / 'orbit.sqlite'}"
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "head")

    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            revision = connection.exec_driver_sql("SELECT version_num FROM alembic_version")
            assert revision.scalar_one() == "0007"
            triggers = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).scalars()
            )
            assert {
                f"invalidate_rollups_{table}_{action}"
                for table in (
                    "balance_snapshots",
                    "month_end_snapshots",
                    "accounts",
                    "institutions",
                    "account_facts",
                )
                for action in ("insert", "update", "delete")
            } <= triggers
            assert {
                "month_end_snapshots_no_replace",
                "month_end_snapshots_no_update",
                "month_end_snapshots_no_delete",
            } <= triggers
    finally:
        engine.dispose()

    command.check(cfg)


def test_paystub_migration_preserves_existing_stub_and_lines(tmp_path):
    url = f"sqlite:///{tmp_path / 'existing.sqlite'}"
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0002")
    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO paystubs (id, employer, pay_date, gross, net) "
                "VALUES (1, 'Synthetic', '2026-01-15', 10000, 8000)"
            )
            connection.exec_driver_sql(
                'INSERT INTO paystub_lines (paystub_id, label, "group", amount) '
                "VALUES (1, 'Tax', 'tax', 2000)"
            )
        command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT gross, net, gross_ytd, net_ytd, file_sha256 FROM paystubs"
            ).one() == (10000, 8000, None, None, None)
            assert connection.exec_driver_sql(
                "SELECT amount, limit_bucket FROM paystub_lines"
            ).one() == (2000, None)
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
    command.check(cfg)
