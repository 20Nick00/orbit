import datetime as dt
from decimal import Decimal

import pytest

from core.models import AccountFact
from core.rollups import read_dashboard
from core.snapshots import MixedCurrencyError, record_daily
from property.valuation import home_equity, home_equity_summary

TODAY = dt.date(2026, 4, 12)


def link(session, home, value):
    session.add(AccountFact(account_id=home.id, key="linked_liability", value=value))
    session.flush()


def test_linked_equity_subtracts_positive_debt(session, account_factory):
    home = account_factory(type="property")
    mortgage = account_factory(type="mortgage")
    link(session, home, str(mortgage.id))
    record_daily(home.id, TODAY, 500000, session=session)
    record_daily(mortgage.id, TODAY, 300000, session=session)
    row = home_equity(home.id, session)
    assert row["status"] == "ok" and row["equity"] == Decimal(200000)
    assert home_equity_summary(session, TODAY)["total"] == Decimal(200000)
    assert home_equity_summary(session, TODAY - dt.timedelta(days=1))["total"] is None


@pytest.mark.parametrize("case", ["text", "missing_account", "hidden", "asset", "currency", "self"])
def test_invalid_links(session, account_factory, case):
    home = account_factory(type="property")
    debt = account_factory(
        type="cash" if case == "asset" else "mortgage",
        hidden=case == "hidden",
        currency="EUR" if case == "currency" else "USD",
    )
    value = {"text": "abc", "missing_account": "99999", "self": str(home.id)}.get(
        case, str(debt.id)
    )
    link(session, home, value)
    row = home_equity(home.id, session)
    assert row["status"] == "invalid" and row["equity"] is None


@pytest.mark.parametrize("side", ["home", "debt", "both"])
def test_missing_balance_is_not_zero(session, account_factory, side):
    home = account_factory(type="property")
    debt = account_factory(type="mortgage")
    link(session, home, str(debt.id))
    if side == "home":
        record_daily(debt.id, TODAY, 100, session=session)
    if side == "debt":
        record_daily(home.id, TODAY, 500, session=session)
    assert home_equity(home.id, session)["status"] == "missing"
    assert home_equity_summary(session)["total"] is None


def test_summary_counts_and_hidden_exclusion(session, account_factory):
    home = account_factory(type="property")
    broken = account_factory(type="property")
    account_factory(type="property", hidden=True)
    link(session, broken, "bad")
    assert home_equity(home.id, session)["status"] == "unlinked"
    result = home_equity_summary(session)
    assert result["total"] is None
    assert len(result["rows"]) == 2
    assert result["unlinked_count"] == result["problem_count"] == 1


def test_equity_never_adds_different_currencies(session, account_factory):
    for currency in ("USD", "EUR"):
        home = account_factory(type="property", currency=currency)
        debt = account_factory(type="mortgage", currency=currency)
        link(session, home, str(debt.id))
        record_daily(home.id, TODAY, 500, session=session)
        record_daily(debt.id, TODAY, 100, session=session)
    with pytest.raises(MixedCurrencyError):
        home_equity_summary(session)


def test_multiple_loans_paid_off_shared_debt_and_partial_total_agree(session, account_factory):
    homes = [account_factory(type="property", name=f"Home {i}") for i in range(4)]
    mortgage = account_factory(type="mortgage")
    heloc = account_factory(type="loans")
    link(session, homes[0], f"{mortgage.id},{heloc.id}")
    link(session, homes[1], str(mortgage.id))
    link(session, homes[2], "none")
    for home in homes:
        record_daily(home.id, TODAY, 500, session=session)
    record_daily(mortgage.id, TODAY, 200, session=session)
    record_daily(heloc.id, TODAY, 50, session=session)
    summary = home_equity_summary(session, TODAY)
    assert [r["equity"] for r in summary["rows"]] == [250, 300, 500, None]
    assert summary["total"] == 1250  # Three known homes, shared mortgage subtracted once.
    assert summary["unlinked_count"] == 1 and summary["problem_count"] == 0
    dashboard = read_dashboard(session, TODAY)
    assert dashboard["facts"]["Home equity"] == summary["total"] * 100
    assert dashboard["home_equity_note"] == "1 home not linked"


@pytest.mark.parametrize("link_value", ["1,bad", "1,", "none,1", "99999,1", "1.0", "-1"])
def test_any_bad_link_invalidates_entire_home(session, account_factory, link_value):
    mortgage = account_factory(type="mortgage")
    home = account_factory(type="property")
    assert mortgage.id == 1
    link(session, home, link_value)
    record_daily(home.id, TODAY, 500, session=session)
    record_daily(mortgage.id, TODAY, 100, session=session)
    summary = home_equity_summary(session, TODAY)
    assert summary["rows"][0]["status"] == "invalid"
    assert summary["total"] is None and summary["problem_count"] == 1
    dashboard = read_dashboard(session, TODAY)
    assert dashboard["facts"]["Home equity"] is None
    assert dashboard["home_equity_note"] == "1 home needs attention"


@pytest.mark.parametrize("word", ["Vehicle", "CAR", "Truck", "Auto", "Motorcycle", "Boat", "RV"])
@pytest.mark.parametrize("field", ["name", "subtype"])
def test_vehicles_are_excluded_in_both_equity_readers(session, account_factory, word, field):
    vehicle = account_factory(type="property", **{field: f"My {word} account"})
    home = account_factory(type="property", name="Carriage house")
    link(session, vehicle, "none")
    link(session, home, "none")
    record_daily(vehicle.id, TODAY, 100, session=session)
    record_daily(home.id, TODAY, 500, session=session)
    summary = home_equity_summary(session, TODAY)
    assert summary["total"] == 500 and len(summary["rows"]) == 1
    assert summary["rows"][0]["account_id"] == home.id
    assert read_dashboard(session, TODAY)["facts"]["Home equity"] == 50000


def test_both_equity_readers_ignore_future_home_and_debt_balances(
    session, account_factory, monkeypatch
):
    from property import valuation

    home = account_factory(type="property")
    mortgage = account_factory(type="mortgage")
    link(session, home, str(mortgage.id))
    record_daily(home.id, TODAY, 500, session=session)
    record_daily(mortgage.id, TODAY, 200, session=session)
    future = TODAY + dt.timedelta(days=1)
    record_daily(home.id, future, 900, session=session)
    record_daily(mortgage.id, future, 0, session=session)
    monkeypatch.setattr(valuation, "local_today", lambda: TODAY)
    assert home_equity(home.id, session)["equity"] == 300
    assert home_equity_summary(session)["total"] == 300
    assert read_dashboard(session, TODAY)["facts"]["Home equity"] == 30000
