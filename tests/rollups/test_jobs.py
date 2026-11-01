import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import select

from core import clock
from core.config import Settings
from core.models import MonthEndSnapshot
from core.snapshots import record_daily
from jobs import avm_refresh, month_end
from jobs.scheduler import build_scheduler


def test_closes_all_missed_months_and_never_rewrites(session, account_factory):
    account = account_factory()
    record_daily(account.id, dt.date(2025, 10, 1), 100, session=session)
    record_daily(account.id, dt.date(2025, 12, 1), 300, session=session)
    today = dt.date(2026, 1, 1)
    assert month_end.close_due_months(today, session) == ["2025-10", "2025-12"]
    assert month_end.close_due_months(today, session) == []
    # A late daily row makes the gap eligible, without changing closed months.
    record_daily(account.id, dt.date(2025, 11, 2), 200, session=session)
    record_daily(account.id, dt.date(2025, 12, 2), 999, session=session)
    assert month_end.close_due_months(today, session) == ["2025-11"]
    assert month_end.close_due_months(today, session) == []
    rows = list(session.scalars(select(MonthEndSnapshot)))
    assert {r.month: r.balance for r in rows} == {"2025-10": 100, "2025-11": 200, "2025-12": 300}
    assert session.get(MonthEndSnapshot, (account.id, "2026-01")) is None


def test_one_close_failure_does_not_stop_others(session, account_factory, monkeypatch):
    account = account_factory()
    record_daily(account.id, dt.date(2025, 10, 1), 100, session=session)
    record_daily(account.id, dt.date(2025, 11, 1), 100, session=session)
    record_daily(account.id, dt.date(2025, 12, 1), 100, session=session)
    real = month_end.close_month

    def sometimes(month, **kwargs):
        if month == "2025-11":
            raise ValueError("Test failure")
        return real(month, **kwargs)

    monkeypatch.setattr(month_end, "close_month", sometimes)
    assert month_end.close_due_months(dt.date(2026, 1, 1), session) == ["2025-10", "2025-12"]


def test_manual_july_balance_does_not_close_august_through_october(session, account_factory):
    from core.rollups import monthly_rollups

    account = account_factory(is_manual=True)
    record_daily(account.id, dt.date(2026, 7, 1), 100, session=session)
    today = dt.date(2026, 11, 1)
    assert month_end.close_due_months(today, session) == ["2026-07"]
    assert list(session.scalars(select(MonthEndSnapshot.month))) == ["2026-07"]
    rows = monthly_rollups("2026-08", "2026-10", today=today, session=session)
    assert all(row.net_worth_end is None and not row.closed for row in rows)


def test_real_month_close_without_session_carries_unchanged_accounts(session, account_factory):
    manual = account_factory(is_manual=True)
    bank = account_factory()
    record_daily(manual.id, dt.date(2026, 7, 1), 100, session=session)
    record_daily(bank.id, dt.date(2026, 9, 1), 200, session=session)
    ids = manual.id, bank.id
    session.commit()
    assert month_end.close_due_months(dt.date(2026, 11, 1)) == ["2026-07", "2026-09"]
    assert session.get(MonthEndSnapshot, (ids[0], "2026-09")).balance == 100
    assert session.get(MonthEndSnapshot, (ids[1], "2026-09")).balance == 200
    assert month_end.close_due_months(dt.date(2026, 11, 1)) == []


def test_month_end_run_closes_real_month(session, account_factory, monkeypatch):
    account = account_factory()
    account_id = account.id
    record_daily(account_id, dt.date(2026, 9, 15), 123, session=session)
    session.commit()
    monkeypatch.setattr(month_end, "local_today", lambda: dt.date(2026, 10, 1))
    month_end.run()
    assert session.get(MonthEndSnapshot, (account_id, "2026-09")).balance == 123


def test_avm_refresh_run_writes_real_valuation(session, account_factory, monkeypatch):
    import httpx

    from core.config import get_settings
    from core.models import AccountFact, BalanceSnapshot, Valuation
    from property import valuation
    from property.rentcast import RentCastClient

    account = account_factory(type="property")
    account_id = account.id
    session.add(AccountFact(account_id=account_id, key="address", value="123 Test Street"))
    session.commit()
    today = dt.date(2026, 10, 1)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"price": 123456})

    transport = httpx.MockTransport(handle)

    class MockedClient(RentCastClient):
        def __init__(self, key):
            super().__init__(key, transport=transport)

    monkeypatch.setenv("RENTCAST_API_KEY", "test-key")
    get_settings.cache_clear()
    monkeypatch.setattr(valuation, "RentCastClient", MockedClient)
    monkeypatch.setattr(avm_refresh, "local_today", lambda: today)
    assert avm_refresh.run().refreshed == [account_id]
    assert len(calls) == 1
    assert session.get(Valuation, (account_id, today, "rentcast")).value == 123456
    assert session.get(BalanceSnapshot, (account_id, today)).balance == 123456


def test_jobs_contain_exceptions(monkeypatch, caplog):
    # Alembic's fileConfig disables previously created application loggers.
    monkeypatch.setattr(month_end.logger, "disabled", False)
    monkeypatch.setattr(avm_refresh.logger, "disabled", False)

    def fail(*args, **kwargs):
        raise RuntimeError("Test private exception")

    monkeypatch.setattr(month_end, "close_due_months", fail)
    monkeypatch.setattr(avm_refresh, "refresh_due", fail)
    month_end.run()
    avm_refresh.run()
    assert "Test private exception" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_scheduler_crons_catchup_and_limits():
    settings = Settings(_env_file=None, timezone="America/New_York")
    scheduler = build_scheduler(settings)
    assert str(scheduler.timezone) == "America/New_York"
    for name, hour, minute in (("month_end", "2", "30"), ("avm_refresh", "3", "0")):
        job = scheduler.get_job(name)
        fields = {field.name: str(field) for field in job.trigger.fields}
        assert fields["day"] == "1" and fields["hour"] == hour and fields["minute"] == minute
        assert job.coalesce and job.max_instances == 1 and job.misfire_grace_time is None
        seconds = (job.next_run_time - dt.datetime.now(scheduler.timezone)).total_seconds()
        assert 0 < seconds <= 5
    assert len(scheduler.get_jobs()) == 3


def test_clock_uses_configured_timezone_and_local_fallback(monkeypatch):
    settings = Settings(_env_file=None, timezone="America/New_York")
    monkeypatch.setattr(clock, "get_settings", lambda: settings)

    class FixedDatetime:
        @staticmethod
        def now(zone):
            return dt.datetime(2026, 1, 1, 2, tzinfo=dt.UTC).astimezone(zone)

    monkeypatch.setattr(clock, "datetime", FixedDatetime)
    assert clock.local_today() == dt.date(2025, 12, 31)
    settings.timezone = None
    monkeypatch.setattr(clock, "get_localzone", lambda: ZoneInfo("Asia/Tokyo"))
    assert clock.local_today() == dt.date(2026, 1, 1)
