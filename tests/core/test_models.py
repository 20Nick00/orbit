import datetime as dt
from decimal import Decimal
from io import StringIO

import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from core import db
from core.config import get_settings
from core.models import (
    Account,
    AccountFact,
    BalanceSnapshot,
    Base,
    DecimalText,
    Holding,
    ImportRule,
    Institution,
    Money,
    MonthEndSnapshot,
    Paystub,
    PaystubLine,
    Transaction,
    TypeOverride,
    Valuation,
)


def test_upgrade_downgrade_and_reupgrade(database, migration_config):
    assert set(inspect(database).get_table_names()) == set(Base.metadata.tables) | {
        "alembic_version"
    }
    command.downgrade(migration_config, "base")
    assert inspect(database).get_table_names() == ["alembic_version"]
    with database.connect() as connection:
        assert (
            connection.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'")).all()
            == []
        )
    command.upgrade(migration_config, "head")
    assert set(inspect(database).get_table_names()) == set(Base.metadata.tables) | {
        "alembic_version"
    }


def test_alembic_check(database, migration_config):
    output = StringIO()
    migration_config.stdout = output
    command.check(migration_config)
    assert "No new upgrade operations detected" in output.getvalue()


def test_offline_migration_includes_replace_trigger(migration_config):
    migration_config.output_buffer = StringIO()
    command.upgrade(migration_config, "head", sql=True)
    assert "month_end_snapshots_no_replace" in migration_config.output_buffer.getvalue()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE month_end_snapshots SET balance=999 WHERE account_id=:id",
        "DELETE FROM month_end_snapshots WHERE account_id=:id",
    ],
)
def test_month_end_database_rejects_changes(session, account_factory, statement):
    account = account_factory()
    account_id = account.id
    session.add(MonthEndSnapshot(account_id=account_id, month="2024-01", balance=Decimal("10.00")))
    session.commit()
    with pytest.raises(IntegrityError, match="month_end_snapshots are immutable"):
        session.execute(text(statement), {"id": account_id})
    session.rollback()
    assert session.get(MonthEndSnapshot, (account_id, "2024-01")).balance == Decimal("10.00")


def test_month_end_database_rejects_replace_but_accepts_new_month(session, account_factory):
    account = account_factory()
    account_id = account.id
    session.add(MonthEndSnapshot(account_id=account_id, month="2024-01", balance=Decimal("10.00")))
    session.commit()
    session.execute(text("PRAGMA recursive_triggers = OFF"))
    statement = text(
        "INSERT OR REPLACE INTO month_end_snapshots (account_id, month, balance) "
        "VALUES (:id, :month, 999)"
    )
    with pytest.raises(IntegrityError, match="immutable"):
        session.execute(statement, {"id": account_id, "month": "2024-01"})
    session.rollback()
    assert session.get(MonthEndSnapshot, (account_id, "2024-01")).balance == Decimal("10.00")
    session.execute(statement, {"id": account_id, "month": "2024-02"})
    assert session.get(MonthEndSnapshot, (account_id, "2024-02")).balance == Decimal("9.99")


def test_account_defaults_and_bad_type(session, account_factory):
    account = account_factory()
    assert account.currency == "USD"
    assert (account.is_liability, account.is_manual, account.hidden) == (False, False, False)
    assert isinstance(account.created_at, dt.datetime)
    with pytest.raises(IntegrityError, match="valid_type"):
        session.execute(text("INSERT INTO accounts (name, type) VALUES ('Bad', 'unknown')"))


@pytest.mark.parametrize(
    "account_type,expected", [("cards", True), ("loans", True), ("mortgage", True), ("cash", False)]
)
def test_account_liability_default_matches_type(session, account_type, expected):
    account = Account(name="Example", type=account_type)
    session.add(account)
    session.flush()
    session.refresh(account)
    assert account.is_liability is expected


@pytest.mark.parametrize("account_type,is_liability", [("cards", False), ("cash", True)])
def test_account_rejects_mismatched_liability(session, account_type, is_liability):
    session.add(Account(name="Example", type=account_type, is_liability=is_liability))
    with pytest.raises(IntegrityError, match="liability_matches_type"):
        session.flush()


@pytest.mark.parametrize("is_liability", [False, True])
def test_other_account_accepts_either_liability_flag(session, is_liability):
    account = Account(name="Example", type="other", is_liability=is_liability)
    session.add(account)
    session.flush()
    session.refresh(account)
    assert account.is_liability is is_liability


def test_raw_sql_card_requires_liability_flag(session):
    with pytest.raises(IntegrityError, match="liability_matches_type"):
        session.execute(text("INSERT INTO accounts (name, type) VALUES ('Visa', 'cards')"))


@pytest.mark.parametrize(
    "value,error",
    [
        (0.1, TypeError),
        (True, TypeError),
        (False, TypeError),
        (Decimal("NaN"), ValueError),
        (Decimal("sNaN"), ValueError),
        ("Infinity", ValueError),
        ("-Infinity", ValueError),
        ("oops", ValueError),
        ("92233720368547758.08", ValueError),
    ],
)
def test_money_rejects_bad_values(value, error):
    with pytest.raises(error):
        Money().process_bind_param(value, None)


def test_money_float_rejected_on_database_write(session, account_factory):
    account = account_factory()
    session.add(
        BalanceSnapshot(account_id=account.id, date=dt.date(2024, 1, 1), balance=0.3, source="test")
    )
    with pytest.raises(StatementError) as exc:
        session.flush()
    assert isinstance(exc.value.orig, TypeError)


@pytest.mark.parametrize(
    "value,expected,cents",
    [
        (Decimal("0.1") + Decimal("0.2"), Decimal("0.30"), 30),
        ("1.005", Decimal("1.01"), 101),
        ("-1.005", Decimal("-1.01"), -101),
        (2, Decimal("2.00"), 200),
        ("92233720368547758.07", Decimal("92233720368547758.07"), 9223372036854775807),
        ("-92233720368547758.08", Decimal("-92233720368547758.08"), -9223372036854775808),
    ],
)
def test_money_round_trip_exact_cents(session, account_factory, value, expected, cents):
    account = account_factory()
    row = BalanceSnapshot(
        account_id=account.id, date=dt.date(2024, 1, 1), balance=value, source="test"
    )
    session.add(row)
    session.flush()
    session.refresh(row)
    assert row.balance == expected
    assert isinstance(row.balance, Decimal)
    assert session.execute(
        text("SELECT balance, typeof(balance) FROM balance_snapshots")
    ).one() == (cents, "integer")


def test_quantity_is_exact_and_missing_amounts_stay_missing(session, account_factory):
    account = account_factory()
    qty = Decimal("0.123456789012345678901234567890123456789")
    holding = Holding(account_id=account.id, date=dt.date(2024, 1, 1), qty=qty)
    session.add(holding)
    session.flush()
    session.refresh(holding)
    assert holding.qty == qty
    assert holding.value is None and holding.cost_basis is None
    assert session.execute(text("SELECT qty, typeof(qty) FROM holdings")).one() == (
        str(qty),
        "text",
    )


@pytest.mark.parametrize("value,error", [(0.5, TypeError), (True, TypeError), ("NaN", ValueError)])
def test_quantity_rejects_bad_values(value, error):
    with pytest.raises(error):
        DecimalText().process_bind_param(value, None)


def test_foreign_keys_and_wal_on_every_connection(database):
    with database.connect() as first, database.connect() as second:
        for connection in (first, second):
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
    with database.begin() as connection:
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                text(
                    "INSERT INTO balance_snapshots (account_id, date, balance, source) "
                    "VALUES (987654, '2024-01-01', 100, 'test')"
                )
            )


def test_deleting_account_does_not_cascade(session, account_factory):
    account = account_factory()
    account_id = account.id
    session.add(
        BalanceSnapshot(account_id=account_id, date=dt.date(2024, 1, 1), balance=1, source="test")
    )
    session.commit()
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.delete(account)
        session.flush()
    session.rollback()
    assert session.get(Account, account_id) is not None
    assert session.get(BalanceSnapshot, (account_id, dt.date(2024, 1, 1))).balance == Decimal(
        "1.00"
    )


@pytest.mark.parametrize(
    "column_map,line_map,valid",
    [
        ({}, None, True),
        (None, {}, True),
        (None, None, False),
        ({}, {}, False),
    ],
)
def test_import_rule_requires_exactly_one_map(session, column_map, line_map, valid):
    session.add(
        ImportRule(source_kind="csv", matcher="header", column_map=column_map, line_map=line_map)
    )
    if valid:
        session.flush()
    else:
        with pytest.raises(IntegrityError, match="exactly_one_map"):
            session.flush()


@pytest.mark.parametrize(
    "values",
    [
        {"mapped_type": "cash"},
        {"mapped_type": "nonsense", "aggregator": "plaid"},
    ],
)
def test_override_database_checks(session, values):
    session.add(TypeOverride(**values))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("month", ["2024-1", "2024-13", "2024-00", "abcd-01"])
def test_month_format_check_rejects_invalid_month(session, account_factory, month):
    account = account_factory()
    session.add(MonthEndSnapshot(account_id=account.id, month=month, balance=1))
    with pytest.raises(IntegrityError, match="month_format"):
        session.flush()


def test_month_format_check_accepts_december(session, account_factory):
    account = account_factory()
    session.add(MonthEndSnapshot(account_id=account.id, month="2024-12", balance=1))
    session.flush()
    assert session.get(MonthEndSnapshot, (account.id, "2024-12")).balance == Decimal("1.00")


def test_paystub_groups_and_repeated_labels(session):
    stub = Paystub(employer="Example", pay_date=dt.date(2024, 1, 1))
    session.add(stub)
    session.flush()
    for group in ("tax", "pretax", "posttax", "benefit", "employer"):
        session.add(PaystubLine(paystub_id=stub.id, label="Repeated", group_=group, amount="1.005"))
    session.flush()
    session.expire_all()
    lines = list(session.scalars(select(PaystubLine)))
    assert len({line.id for line in lines}) == 5
    assert all(line.amount == Decimal("1.01") and line.ytd_amount is None for line in lines)
    session.add(PaystubLine(paystub_id=stub.id, label="Invalid", group_="other"))
    with pytest.raises(IntegrityError, match="valid_group"):
        session.flush()


def test_unique_institution_external_id(session):
    session.add_all(
        [Institution(name="Bank", aggregator="plaid", external_id="same") for _ in range(2)]
    )
    with pytest.raises(IntegrityError, match="UNIQUE"):
        session.flush()


def test_unique_transaction_external_id(session, account_factory):
    account = account_factory()
    session.add_all(
        [
            Transaction(
                account_id=account.id, date=dt.date(2024, 1, 1), amount=1, external_id="same"
            )
            for _ in range(2)
        ]
    )
    with pytest.raises(IntegrityError, match="UNIQUE"):
        session.flush()


def test_nullable_tickers_and_composite_keys(session, account_factory):
    account = account_factory()
    date = dt.date(2024, 1, 1)
    session.add_all([Holding(account_id=account.id, date=date) for _ in range(2)])
    session.add_all(
        [
            Valuation(account_id=account.id, date=date, value=100, source=source)
            for source in ("manual", "avm")
        ]
    )
    session.add(AccountFact(account_id=account.id, key="note", value="No estimate yet"))
    session.flush()
    assert len(list(session.scalars(select(Holding)))) == 2
    assert len(list(session.scalars(select(Valuation)))) == 2
    assert session.get(AccountFact, (account.id, "note")).value == "No estimate yet"


def test_url_precedence_and_default_directory(monkeypatch, tmp_path):
    db.configure(None)
    try:
        monkeypatch.delenv("ORBIT_DATABASE_URL", raising=False)
        monkeypatch.chdir(tmp_path)
        get_settings.cache_clear()
        assert db.get_url() == "sqlite:///data/orbit.sqlite"
        engine = db.get_engine()
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
        assert (tmp_path / "data" / "orbit.sqlite").is_file()
        monkeypatch.setenv("ORBIT_DATABASE_URL", "sqlite:///:memory:")
        get_settings.cache_clear()
        assert db.get_url() == "sqlite:///:memory:"
        db.configure("sqlite:///explicit.sqlite")
        assert db.get_url() == "sqlite:///explicit.sqlite"
        assert db.get_engine() is not engine
        db.configure(None)
        assert db.get_url() == "sqlite:///:memory:"
    finally:
        db.configure(None)
        get_settings.cache_clear()


def test_session_scope_commit_rollback_and_close(database):
    with db.session_scope() as active:
        account = Account(name="Committed", type="cash")
        active.add(account)
        active.flush()
        account_id = account.id
    assert not active.in_transaction()
    with pytest.raises(RuntimeError, match="cancel"):
        with db.session_scope() as failed:
            failed.add(Account(name="Rolled back", type="cash"))
            failed.flush()
            raise RuntimeError("cancel")
    assert not failed.in_transaction()
    with db.session_scope() as check:
        assert check.get(Account, account_id).name == "Committed"
        assert list(check.scalars(select(Account.name))) == ["Committed"]


def test_migration_uses_configured_url_when_ini_has_no_url(migration_config):
    url = migration_config.get_main_option("sqlalchemy.url")
    migration_config.remove_main_option("sqlalchemy.url")
    db.configure(url)
    try:
        command.upgrade(migration_config, "head")
        assert "accounts" in inspect(db.get_engine()).get_table_names()
    finally:
        db.configure(None)
