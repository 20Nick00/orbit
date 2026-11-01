import datetime as dt
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from core.config import get_settings
from core.models import (
    AccountFact,
    BalanceSnapshot,
    RentcastAttempts,
    RentcastCache,
    RentcastUsage,
    Valuation,
)
from core.snapshots import record_daily
from property import valuation
from property.rentcast import RentCastClient, RentCastError, address_key

TODAY = dt.date(2026, 4, 12)


@pytest.fixture
def home(session, account_factory):
    account = account_factory(type="property")
    session.add(AccountFact(account_id=account.id, key="address", value="123 Private St."))
    record_daily(account.id, dt.date(2026, 3, 1), 100, session=session)
    account_id = account.id
    session.commit()  # A quota reservation needs its own committed connection.
    return account_id


@pytest.fixture
def rentcast():
    return RentCastClient(
        "test-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"price": 200, "priceRangeLow": 180, "priceRangeHigh": 220}
            )
        ),
    )


def test_job_initial_manual_balance_and_monthly_idempotence(home, rentcast, session):
    assert valuation.valuation_status(home, session)["label"] == "Manual value"
    assert valuation.refresh_due(TODAY, client=rentcast).refreshed == [home]
    session.expire_all()
    assert session.get(Valuation, (home, TODAY, "rentcast")).value == Decimal(200)
    assert session.get(BalanceSnapshot, (home, TODAY)).source == "rentcast"
    assert valuation.refresh_due(TODAY, client=rentcast).skipped == [home]
    assert session.get(RentcastUsage, TODAY).requests == 1
    status = valuation.valuation_status(home, session)
    assert status["label"] == "RentCast estimate"
    assert (status["low"], status["high"]) == (Decimal(180), Decimal(220))


def test_override_survives_job_explicit_refresh_ends_it(home, rentcast, session):
    valuation.set_override(home, 300, today=TODAY)
    assert valuation.refresh_due(TODAY, client=rentcast).skipped == [home]
    assert valuation.valuation_status(home)["is_override"]
    valuation.refresh(home, today=TODAY, client=rentcast)
    status = valuation.valuation_status(home)
    assert not status["is_override"] and status["value"] == 200
    valuation.set_override(home, 400, today=TODAY)
    status = valuation.valuation_status(home)
    assert status["is_override"] and status["value"] == 400
    assert status["low"] is None
    # Both sources stay in history; the daily snapshot resolves same-day order.
    assert session.scalar(select(func.count()).select_from(Valuation)) == 2


def test_failed_refresh_keeps_override_and_writes_nothing(home, session):
    valuation.set_override(home, 300, today=TODAY)
    client = RentCastClient(
        "test", transport=httpx.MockTransport(lambda request: httpx.Response(500))
    )
    with pytest.raises(RentCastError):
        valuation.refresh(home, today=TODAY, client=client, session=session)
    session.commit()
    status = valuation.valuation_status(home)
    assert status["is_override"] and status["value"] == 300
    assert session.get(Valuation, (home, TODAY, "rentcast")) is None
    assert session.scalar(select(func.count()).select_from(RentcastCache)) == 0
    assert session.get(RentcastUsage, TODAY).requests == 1


def test_snapshot_failure_rolls_back_valuation_and_cache(home, rentcast, session, monkeypatch):
    valuation.set_override(home, 300, today=TODAY)

    def fail(*args, **kwargs):
        raise RuntimeError("Simulated snapshot write failure")

    monkeypatch.setattr(valuation, "record_daily", fail)
    with pytest.raises(RuntimeError):
        valuation.refresh(home, today=TODAY, client=rentcast, session=session)
    session.commit()
    assert valuation.valuation_status(home)["value"] == 300
    assert valuation.valuation_status(home)["is_override"]
    assert session.get(Valuation, (home, TODAY, "rentcast")) is None
    assert session.scalar(select(func.count()).select_from(RentcastCache)) == 0
    assert session.get(RentcastUsage, TODAY).requests == 1


def test_later_manual_snapshot_overrides(home, rentcast):
    valuation.refresh(home, today=TODAY, client=rentcast)
    record_daily(home, dt.date(2026, 5, 1), 450, source="manual")
    assert valuation.valuation_status(home)["is_override"]
    assert valuation.refresh_due(dt.date(2026, 5, 2), client=rentcast).skipped == [home]


def test_job_continues_after_failure_stops_at_quota_and_skips_hidden(
    home,
    session,
    account_factory,
    monkeypatch,
):
    second = account_factory(type="property", name="Second")
    hidden = account_factory(type="property", hidden=True)
    for account in (second, hidden):
        session.add(AccountFact(account_id=account.id, key="address", value=f"{account.id} Street"))
    second_id, hidden_id = second.id, hidden.id
    session.commit()
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(500 if len(calls) == 1 else 200, json={"price": 700})

    client = RentCastClient("test", transport=httpx.MockTransport(handle))
    result = valuation.refresh_due(TODAY, client=client)
    assert result.failed == [home]
    assert result.refreshed == [second_id]
    assert session.get(Valuation, (hidden_id, TODAY, "rentcast")) is None
    monkeypatch.setenv("ORBIT_RENTCAST_MONTHLY_LIMIT", "0")
    get_settings.cache_clear()
    result = valuation.refresh_due(dt.date(2026, 5, 1), client=client)
    assert result.stopped == "quota"
    assert len(calls) == 2


def test_no_key_writes_nothing(home, session, caplog, monkeypatch):
    monkeypatch.setattr(valuation.logger, "disabled", False)
    result = valuation.refresh_due(TODAY)
    assert result.stopped == "not_configured"
    assert len(caplog.records) == 1
    assert session.scalar(select(func.count()).select_from(Valuation)) == 0
    assert session.scalar(select(func.count()).select_from(RentcastUsage)) == 0


@pytest.mark.parametrize("value", [0, -1, "NaN", "Infinity", "junk"])
def test_override_rejects_invalid_values(home, value):
    with pytest.raises(ValueError):
        valuation.set_override(home, value, today=TODAY)


def test_supplied_session_rollback_keeps_caller_in_control(home, session):
    valuation.set_override(home, 300, today=TODAY, session=session)
    session.rollback()
    assert valuation.valuation_status(home)["value"] == 100
    assert not valuation.valuation_status(home)["is_override"]


def test_same_source_upsert_refreshes_loaded_valuation(home, session):
    valuation.set_override(home, 300, today=TODAY, session=session)
    loaded = session.get(Valuation, (home, TODAY, "manual"))
    valuation.set_override(home, 400, today=TODAY, session=session)
    assert loaded.value == 400


@pytest.mark.parametrize("job", [True, False])
def test_override_during_http_request_wins_only_against_job(home, session, job):
    def handle(request):
        valuation.set_override(home, 333, today=TODAY)
        return httpx.Response(200, json={"price": 222})

    client = RentCastClient("test", transport=httpx.MockTransport(handle))
    if job:
        result = valuation.refresh_due(TODAY, client=client)
        assert result.skipped == [home] and result.refreshed == [] and result.failed == []
    else:
        valuation.refresh(home, today=TODAY, client=client)
    status = valuation.valuation_status(home, today=TODAY)
    assert status["value"] == (333 if job else 222)
    assert status["is_override"] is job
    assert (session.get(Valuation, (home, TODAY, "rentcast")) is None) is job


def test_failed_job_attempts_are_daily_and_monthly_limited_but_explicit_is_not(home, session):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(500)

    client = RentCastClient("test", transport=httpx.MockTransport(handle))
    for offset in range(4):
        day = TODAY + dt.timedelta(days=offset)
        result = valuation.refresh_due(day, client=client)
        assert result.failed == ([home] if offset < 3 else [])
        for _ in range(2):
            assert valuation.refresh_due(day, client=client).skipped == [home]
        assert len(calls) == min(offset + 1, 3)
    attempt = session.get(RentcastAttempts, (address_key("123 Private St."), "2026-04"))
    assert attempt.attempts == 3 and attempt.last_attempt == TODAY + dt.timedelta(days=2)
    # Explicit refresh bypasses attempts, but still reserves the request budget.
    success = RentCastClient(
        "test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"price": 500})),
    )
    valuation.refresh(home, today=TODAY + dt.timedelta(days=3), client=success)
    assert valuation.valuation_status(home, today=TODAY + dt.timedelta(days=3))["value"] == 500
    session.expire_all()
    assert attempt.attempts == 3


def test_auth_failure_stops_after_first_request(home, session, account_factory):
    second = account_factory(type="property")
    session.add(AccountFact(account_id=second.id, key="address", value="Second address"))
    session.commit()
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(401)

    result = valuation.refresh_due(
        TODAY, client=RentCastClient("test", transport=httpx.MockTransport(handle))
    )
    assert result.stopped == "auth" and result.refreshed == [] and len(calls) == 1


def test_future_snapshot_and_valuation_cannot_hold_override(home, session, rentcast, monkeypatch):
    future = TODAY + dt.timedelta(days=10)
    valuation.set_override(home, 900, today=future)
    monkeypatch.setattr(valuation, "local_today", lambda: TODAY)
    assert not valuation.valuation_status(home)["is_override"]
    assert valuation.valuation_status(home)["value"] == 100
    assert valuation.refresh_due(TODAY, client=rentcast).refreshed == [home]
    # A future valuation alone cannot turn today's manual balance into an override.
    other_day = TODAY - dt.timedelta(days=1)
    assert not valuation.valuation_status(home, today=other_day)["is_override"]
    assert valuation.valuation_status(home)["label"] == "RentCast estimate"
