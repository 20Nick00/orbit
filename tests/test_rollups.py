from datetime import date, timedelta

import pytest
from sqlalchemy import delete, func, insert, select, update

from core.models import (
    Account,
    AccountFact,
    BalanceSnapshot,
    Institution,
    MonthEndSnapshot,
    RollupCache,
)
from core.rollups import RANGES, delta, read_chart, read_dashboard, rebuild
from core.snapshots import close_month, record_daily

TODAY = date(2026, 10, 31)


def add_account(session, kind="cash", balance="100", day=TODAY, **kwargs):
    account = Account(name=kind, type=kind, **kwargs)
    session.add(account)
    session.flush()
    if balance is not None:
        record_daily(account.id, day, balance, session=session)
    return account


@pytest.mark.parametrize(
    "key,count,start",
    [
        ("1M", 31, "2026-10-01"),
        ("3M", 91, "2026-08-02"),
        ("6M", 27, "2026-05-04"),
        ("1Y", 13, "2025-10-31"),
        ("All", 25, "2024-11-01"),
    ],
)
def test_chart_ranges(demo_session, key, count, start):
    chart = read_chart(demo_session, TODAY, key)
    assert len(chart["series"]) == len(chart["dates"]) == count
    assert chart["dates"][0] == start
    assert chart["dates"][-1] == TODAY.isoformat()
    assert chart["monthly"] == (key in ("1Y", "All"))
    if key == "6M":
        assert all((TODAY - date.fromisoformat(day)).days % 7 == 0 for day in chart["dates"][1:])


def test_monthly_midmonth_and_leap_year(rollup_session):
    add_account(rollup_session, day=date(2023, 1, 1))
    chart = read_chart(rollup_session, date(2024, 3, 15), "1Y")
    assert chart["dates"][0] == "2023-02-28"
    assert chart["dates"][-2:] == ["2024-02-29", "2024-03-15"]
    assert len(chart["dates"]) == 14


def test_closed_month_uses_frozen_value(rollup_session):
    account = add_account(rollup_session, balance="100", day=date(2026, 8, 15))
    close_month("2026-08", today=TODAY, session=rollup_session)
    record_daily(account.id, date(2026, 8, 31), "200", session=rollup_session)
    record_daily(account.id, date(2026, 9, 30), "300", session=rollup_session)
    chart = read_chart(rollup_session, date(2026, 10, 15), "1Y")
    assert chart["dates"] == ["2026-08-15", "2026-08-31", "2026-09-30", "2026-10-15"]
    assert chart["series"] == [100, 100, 300, 300]
    daily = read_chart(rollup_session, date(2026, 10, 15), "3M")
    assert daily["series"][daily["dates"].index("2026-08-31")] == 200


def test_short_history_and_missing_bases(rollup_session):
    add_account(rollup_session)
    dashboard = read_dashboard(rollup_session, TODAY)
    assert all(value is None for value in dashboard["deltas"].values())
    assert dashboard["assets_groups"][0]["accounts"][0]["delta"] is None
    for key in RANGES:
        chart = read_chart(rollup_session, TODAY, key)
        assert chart["dates"] == [TODAY.isoformat()]
        assert chart["series"] == [100]


def test_empty_and_unrecorded_accounts(rollup_session):
    assert read_dashboard(rollup_session, TODAY) is None
    add_account(rollup_session, balance=None)
    dashboard = read_dashboard(rollup_session, TODAY)
    assert dashboard["net_worth"] is None
    assert dashboard["assets"] is None
    assert dashboard["last_updated"] is None
    assert dashboard["chart"]["series"] == []
    row = dashboard["assets_groups"][0]["accounts"][0]
    assert row["balance"] is None
    assert row["last_snapshot"] is None


@pytest.mark.parametrize(
    "kind,balance,net,debt",
    [("cash", "100", 10000, 0), ("cards", "100", -10000, None), ("cash", "0", 0, None)],
)
def test_empty_sides_and_zero_balances(rollup_session, kind, balance, net, debt):
    add_account(rollup_session, kind, balance)
    dashboard = read_dashboard(rollup_session, TODAY)
    assert dashboard["net_worth"] == net
    assert dashboard["facts"]["Debt to assets"] == debt
    assert dashboard["facts"]["Home equity"] is None


def test_hidden_and_future_snapshots_excluded(rollup_session):
    visible = add_account(rollup_session, balance="10", day=TODAY - timedelta(days=40))
    hidden = add_account(rollup_session, balance="900", hidden=True)
    institution = Institution(name="Hidden demo", aggregator="demo")
    rollup_session.add(institution)
    rollup_session.flush()
    hidden.institution_id = institution.id
    record_daily(visible.id, TODAY + timedelta(days=1), "1000", session=rollup_session)
    dashboard = read_dashboard(rollup_session, TODAY)
    assert dashboard["net_worth"] == 1000
    assert dashboard["account_count"] == 1
    assert not dashboard["is_demo"]
    assert dashboard["chart"]["series"] == [10, 10, 10]
    row = dashboard["assets_groups"][0]["accounts"][0]
    assert row["last_snapshot"] == (TODAY - timedelta(days=40)).isoformat()
    assert row["delta"]["d"] == 0


def test_liability_signed_delta_and_subtitle(rollup_session):
    account = add_account(
        rollup_session,
        "cards",
        "100",
        day=TODAY - timedelta(days=30),
        subtype="Credit",
        mask="1234",
    )
    record_daily(account.id, TODAY, "50", session=rollup_session)
    dashboard = read_dashboard(rollup_session, TODAY)
    row = dashboard["liabilities_groups"][0]["accounts"][0]
    assert row["balance"] == -5000
    assert row["delta"] == {"d": 5000, "p": 0.5, "cls": "pos"}
    assert row["subtitle"] == "Credit ••1234"
    assert row["bar_pct"] == 100


def test_home_equity_links_and_other_sides(rollup_session):
    home = add_account(rollup_session, "property", "300")
    mortgage = add_account(rollup_session, "mortgage", "200")
    add_account(rollup_session, "other", "10", is_liability=True)
    add_account(rollup_session, "other", "20")
    assert read_dashboard(rollup_session, TODAY)["facts"]["Home equity"] is None
    fact = AccountFact(account_id=home.id, key="linked_liability", value=str(mortgage.id))
    rollup_session.add(fact)
    dashboard = read_dashboard(rollup_session, TODAY)
    assert dashboard["facts"]["Home equity"] == 10000
    assert [g["type"] for g in dashboard["liabilities_groups"]] == ["mortgage", "other"]
    assert [g["type"] for g in dashboard["assets_groups"]] == ["property", "other"]
    fact.value = str(home.id)
    assert read_dashboard(rollup_session, TODAY)["facts"]["Home equity"] is None
    fact.value = "missing"
    assert read_dashboard(rollup_session, TODAY)["facts"]["Home equity"] is None


def test_record_daily_invalidates_and_same_session_reloads(rollup_session):
    account = add_account(rollup_session)
    assert read_dashboard(rollup_session, TODAY)["net_worth"] == 10000
    # Keep an ORM cache object alive to catch accidentally using session.get.
    cached = rollup_session.get(RollupCache, "dashboard")
    record_daily(account.id, TODAY, "250", session=rollup_session)
    assert rollup_session.scalar(select(func.count()).select_from(RollupCache)) == 0
    assert cached is not None
    assert read_dashboard(rollup_session, TODAY)["net_worth"] == 25000


def test_next_day_rebuilds_once(demo_session, monkeypatch):
    from core import rollups

    calls = []
    original = rebuild

    def counted(session, as_of):
        calls.append(as_of)
        return original(session, as_of)

    monkeypatch.setattr(rollups, "rebuild", counted)
    tomorrow = TODAY + timedelta(days=1)
    read_dashboard(demo_session, tomorrow)
    read_chart(demo_session, tomorrow, "1M")
    read_dashboard(demo_session, tomorrow)
    assert calls == [tomorrow]
    assert set(demo_session.scalars(select(RollupCache.as_of))) == {tomorrow}


@pytest.mark.parametrize(
    "model", [Account, Institution, AccountFact, BalanceSnapshot, MonthEndSnapshot]
)
@pytest.mark.parametrize("action", ["insert", "update", "delete"])
def test_metadata_triggers_invalidate_every_source(rollup_session, model, action):
    account = add_account(rollup_session)
    values = {
        Account: {"name": "New", "type": "cash"},
        Institution: {"name": "New"},
        AccountFact: {"account_id": account.id, "key": "subtitle", "value": "New"},
        BalanceSnapshot: {
            "account_id": account.id,
            "date": TODAY - timedelta(days=1),
            "balance": "1",
            "source": "test",
        },
        MonthEndSnapshot: {"account_id": account.id, "month": "2026-09", "balance": "1"},
    }[model]
    if action != "insert":
        rollup_session.execute(insert(model), values)
    rollup_session.add(RollupCache(key="sentinel", as_of=TODAY, payload={}))
    rollup_session.flush()
    if action == "insert":
        rollup_session.execute(insert(model), values)
    elif action == "update":
        column = (
            "name"
            if model in (Account, Institution)
            else "value"
            if model == AccountFact
            else "balance"
        )
        rollup_session.execute(update(model).values({column: "2"}))
    else:
        # Account deletion needs its child snapshots removed first.
        statement = delete(model)
        if model == Account:
            statement = statement.where(Account.name == "New")
        rollup_session.execute(statement)
    assert rollup_session.scalar(select(func.count()).select_from(RollupCache)) == 0


def test_delta_flat_threshold():
    assert delta(10001, 10000)["cls"] == "flat"
    assert delta(10005, 10000)["cls"] == "pos"
    assert delta(9995, 10000)["cls"] == "neg"
