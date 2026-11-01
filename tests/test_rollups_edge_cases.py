"""Figures on the page, and rollups when balances offset or writers run alongside."""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select, text

from app.main import create_app
from app.routers.dashboard import get_today
from core import rollups
from core.db import get_session, make_engine
from core.demo import DemoRefusedError, seed_demo
from core.models import Account, BalanceSnapshot, RollupCache
from core.rollups import SchemaOutOfDateError, delta, read_dashboard
from core.snapshots import MixedCurrencyError, record_daily
from tests.csrf_client import TestClient

TODAY = date(2026, 10, 31)


def add_account(session, kind="cash", balance="100", **kwargs):
    account = Account(name=kind, type=kind, **kwargs)
    session.add(account)
    session.flush()
    record_daily(account.id, TODAY, balance, session=session)
    return account


def second_writer(session):
    """A separate connection that gives up quickly when the database is locked."""
    engine = make_engine(str(session.get_bind().url))
    connection = engine.connect()
    connection.exec_driver_sql("PRAGMA busy_timeout=50")
    return engine, connection


def write_snapshot(connection, account_id, balance_cents):
    connection.execute(
        text(
            "INSERT OR REPLACE INTO balance_snapshots (account_id, date, balance, source) "
            "VALUES (:id, :day, :balance, 'test')"
        ),
        {"id": account_id, "day": TODAY.isoformat(), "balance": balance_cents},
    )
    connection.commit()


def test_hero_deltas_and_facts_match_the_prototype(demo_session):
    dashboard = read_dashboard(demo_session, TODAY)
    assert dashboard["net_worth"] == 50450000
    # Base dates and values come from running the prototype's own code; cents may round.
    for label, expected in {
        "Today": 504500_00 - 504371_26,
        "1M": 504500_00 - 503566_12,
        "YTD": 504500_00 - 403665_37,
        "1Y": 504500_00 - 387075_28,
    }.items():
        assert abs(dashboard["deltas"][label]["d"] - expected) <= 10, label
    assert dashboard["deltas"]["1Y"]["p"] == pytest.approx(0.3034, abs=0.0001)
    assert dashboard["deltas"]["YTD"]["cls"] == "pos"
    facts = dashboard["facts"]
    assert facts["Assets"] == 104502000
    assert facts["Liabilities"] == -54052000
    assert facts["Liquid net worth"] == 19930000
    assert facts["Debt to assets"] == pytest.approx(0.5172, abs=0.0001)
    assert facts["Home equity"] == 17300000
    assert facts["1-yr change"] == dashboard["deltas"]["1Y"]["d"]


def test_change_from_zero_is_a_change_with_no_percentage():
    assert delta(100, 0) == {"d": 100, "p": None, "cls": "pos"}
    assert delta(-100, 0) == {"d": -100, "p": None, "cls": "neg"}
    assert delta(0, 0) == {"d": 0, "p": 0, "cls": "flat"}


def test_overdrawn_account_keeps_shares_within_100_percent(rollup_session):
    add_account(rollup_session, "cash", "-100")
    add_account(rollup_session, "invest", "1000")
    groups = read_dashboard(rollup_session, TODAY)["assets_groups"]
    shares = {group["type"]: group["share"] for group in groups}
    assert shares["invest"] == pytest.approx(1000 / 1100)
    assert sum(shares.values()) == pytest.approx(1)
    assert groups[1]["accounts"][0]["bar_pct"] == pytest.approx(100 * 1000 / 1100)


def test_offsetting_liabilities_do_not_read_as_zero_share(rollup_session):
    add_account(rollup_session, "cards", "-100")
    add_account(rollup_session, "loans", "100")
    dashboard = read_dashboard(rollup_session, TODAY)
    assert dashboard["liabilities"] == 0
    assert [g["share"] for g in dashboard["liabilities_groups"]] == [0.5, 0.5]


def test_all_zero_side_has_no_share(rollup_session):
    add_account(rollup_session, "cash", "0")
    group = read_dashboard(rollup_session, TODAY)["assets_groups"][0]
    assert group["share"] is None
    assert group["accounts"][0]["bar_pct"] is None


def test_only_overdrawn_assets_have_no_debt_ratio(rollup_session):
    add_account(rollup_session, "cash", "-100")
    add_account(rollup_session, "cards", "50")
    assert read_dashboard(rollup_session, TODAY)["facts"]["Debt to assets"] is None


def test_rebuild_blocks_writers_until_it_has_stored_its_figures(rollup_session, monkeypatch):
    account = add_account(rollup_session, "cash", "100")
    rollup_session.commit()
    engine, writer = second_writer(rollup_session)
    original = rollups.net_worth_series
    attempts = []

    def write_during_rebuild(*args, **kwargs):
        # Without the lock this write lands after the balances were read, and the
        # rebuild then stores figures that miss it as a warm cache until the next write.
        if not attempts:
            try:
                write_snapshot(writer, account.id, 99900)
                attempts.append("written")
            except Exception as exc:
                writer.rollback()
                attempts.append(str(exc))
        return original(*args, **kwargs)

    monkeypatch.setattr(rollups, "net_worth_series", write_during_rebuild)
    try:
        read_dashboard(rollup_session, TODAY)
    finally:
        writer.close()
        engine.dispose()
    assert "database is locked" in attempts[0]


def test_writer_emptying_the_cache_right_after_rebuild_does_not_crash(rollup_session, monkeypatch):
    account = add_account(rollup_session, "cash", "100")
    rollup_session.commit()
    engine, writer = second_writer(rollup_session)
    original = rollup_session.commit

    def commit_then_write():
        original()
        monkeypatch.setattr(rollup_session, "commit", original)
        write_snapshot(writer, account.id, 99900)

    monkeypatch.setattr(rollup_session, "commit", commit_then_write)
    try:
        assert read_dashboard(rollup_session, TODAY)["net_worth"] == 10000
    finally:
        writer.close()
        engine.dispose()


def test_locked_database_still_serves_figures_without_caching(rollup_session):
    add_account(rollup_session, "cash", "100")
    rollup_session.commit()
    rollup_session.connection().exec_driver_sql("PRAGMA busy_timeout=50")
    rollup_session.commit()
    engine = make_engine(str(rollup_session.get_bind().url))
    holder = engine.connect()
    holder.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        assert read_dashboard(rollup_session, TODAY)["net_worth"] == 10000
    finally:
        holder.exec_driver_sql("ROLLBACK")
        holder.close()
        engine.dispose()
    assert rollup_session.scalar(select(func.count()).select_from(RollupCache)) == 0


def test_mixed_currencies_raise_without_leaving_the_session_broken(rollup_session):
    add_account(rollup_session, "cash", "100")
    add_account(rollup_session, "invest", "100", currency="EUR")
    rollup_session.commit()
    with pytest.raises(MixedCurrencyError):
        read_dashboard(rollup_session, TODAY)
    assert rollup_session.scalar(select(func.count()).select_from(Account)) == 2


def test_missing_cache_table_with_real_accounts_is_not_reported_as_empty(rollup_session):
    add_account(rollup_session, "cash", "100")
    rollup_session.commit()
    RollupCache.__table__.drop(rollup_session.connection())
    rollup_session.commit()
    with pytest.raises(SchemaOutOfDateError):
        read_dashboard(rollup_session, TODAY)


def page(session, path="/dashboard"):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_today] = lambda: TODAY
    return TestClient(app).get(path)


def test_page_explains_mixed_currencies_instead_of_failing(rollup_session):
    add_account(rollup_session, "cash", "100")
    add_account(rollup_session, "invest", "100", currency="EUR")
    rollup_session.commit()
    response = page(rollup_session)
    assert response.status_code == 200
    assert "EUR, USD" in response.text
    assert "No accounts yet." not in response.text
    chart = page(rollup_session, "/dashboard/chart?range=1Y")
    assert chart.status_code == 200
    assert "&#34;series&#34;: []" in chart.text or '"series": []' in chart.text


def test_page_asks_for_migration_instead_of_claiming_no_accounts(rollup_session):
    add_account(rollup_session, "cash", "100")
    rollup_session.commit()
    RollupCache.__table__.drop(rollup_session.connection())
    rollup_session.commit()
    response = page(rollup_session)
    assert response.status_code == 200
    assert "No accounts yet." not in response.text
    assert "alembic upgrade head" in response.text


def test_page_renders_a_change_from_zero(rollup_session):
    account = add_account(rollup_session, "cash", "0")
    record_daily(account.id, TODAY - timedelta(days=30), "0", session=rollup_session)
    record_daily(account.id, TODAY, "500", session=rollup_session)
    rollup_session.commit()
    response = page(rollup_session)
    assert response.status_code == 200
    assert "+$500 30d" in response.text


def test_demo_refuses_when_a_demo_account_holds_a_real_balance(demo_session):
    account_id = demo_session.scalar(select(Account.id).limit(1))
    record_daily(account_id, TODAY, "123", source="manual", session=demo_session)
    demo_session.commit()
    with pytest.raises(DemoRefusedError, match="did not come from the demo"):
        seed_demo(demo_session, TODAY)
    assert demo_session.scalar(
        select(func.count()).select_from(BalanceSnapshot).where(BalanceSnapshot.source == "manual")
    )
