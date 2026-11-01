import datetime as dt

import httpx
from sqlalchemy import select

from core.db import get_engine, session_scope
from core.models import Account, AccountFact, MonthEndSnapshot, RentcastUsage
from core.snapshots import record_daily, reopen_month
from jobs.month_end import close_due_months
from property import valuation
from property.rentcast import RentCastClient, address_key, usage_total

TODAY = dt.date(2026, 3, 15)


def _home_with_two_loans():
    with session_scope() as active:
        home = Account(name="Home", type="property")
        mortgage = Account(name="Mortgage", type="mortgage")
        heloc = Account(name="HELOC", type="loans", hidden=True)
        active.add_all([home, mortgage, heloc])
        active.flush()
        link = f"{mortgage.id},{heloc.id}"
        active.add(AccountFact(account_id=home.id, key="linked_liability", value=link))
        return home.id, mortgage.id, heloc.id


def _link(home):
    with session_scope() as active:
        return active.get(AccountFact, (home, "linked_liability")).value


def test_hidden_linked_loan_stays_listed_and_survives_a_resave(client):
    home, mortgage, heloc = _home_with_two_loans()
    page = client.get(f"/account/{home}/valuation").text
    assert f'<option value="{heloc}" selected>HELOC (hidden)</option>' in page
    data = {"address": "1 Test St", "linked_liability": [str(mortgage), str(heloc)]}
    client.post(f"/account/{home}/property-details", data=data)
    assert _link(home) == f"{mortgage},{heloc}"


def test_hidden_loan_can_be_dropped_on_purpose_but_not_newly_linked(client):
    home, mortgage, heloc = _home_with_two_loans()
    url = f"/account/{home}/property-details"
    client.post(url, data={"linked_liability": [str(mortgage)]})
    assert _link(home) == str(mortgage)
    client.post(url, data={"linked_liability": [str(mortgage), str(heloc)]})
    assert _link(home) == str(mortgage)


def test_note_box_has_no_browser_limit_that_would_silently_cut_text(client):
    assert "maxlength" not in client.get("/almanac").text


def test_engine_hides_sql_values_from_logs_and_errors(client):
    assert get_engine().hide_parameters is True


def test_job_keeps_its_attempts_while_the_ceiling_blocks_sending(client, monkeypatch):
    with session_scope() as active:
        home = Account(name="Home", type="property")
        active.add(home)
        active.flush()
        home_id = home.id
        active.add(AccountFact(account_id=home_id, key="address", value="1 Test St"))
        record_daily(home_id, dt.date(2026, 3, 1), 100, session=active)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"price": 500})

    rentcast = RentCastClient("test", transport=httpx.MockTransport(respond))
    monkeypatch.setenv("ORBIT_RENTCAST_MONTHLY_LIMIT", "0")
    from core.config import get_settings

    get_settings.cache_clear()
    for offset in range(5):
        day = TODAY + dt.timedelta(days=offset)
        assert valuation.refresh_due(day, client=rentcast).stopped == "quota"
    assert calls == []
    with session_scope() as active:
        from core.models import RentcastAttempts

        assert active.get(RentcastAttempts, (address_key("1 Test St"), "2026-03")) is None
    monkeypatch.setenv("ORBIT_RENTCAST_MONTHLY_LIMIT", "45")
    get_settings.cache_clear()
    assert valuation.refresh_due(TODAY + dt.timedelta(days=5), client=rentcast).refreshed == [
        home_id
    ]
    assert len(calls) == 1


def test_job_does_not_reclose_a_month_someone_reopened(client):
    with session_scope() as active:
        bank = Account(name="Bank", type="cash")
        active.add(bank)
        active.flush()
        record_daily(bank.id, dt.date(2026, 1, 20), 100, session=active)
    assert close_due_months(TODAY) == ["2026-01"]
    reopen_month("2026-01", "correcting a balance")
    assert close_due_months(TODAY) == []
    with session_scope() as active:
        assert active.scalars(select(MonthEndSnapshot)).first() is None


def test_account_page_shows_valuation_source_date_and_equity(client):
    with session_scope() as active:
        home = Account(name="Home", type="property")
        mortgage = Account(name="Mortgage", type="mortgage")
        active.add_all([home, mortgage])
        active.flush()
        home_id = home.id
        active.add(AccountFact(account_id=home_id, key="linked_liability", value=str(mortgage.id)))
        record_daily(mortgage.id, dt.date(2026, 3, 1), 300000, session=active)
    valuation.set_override(home_id, 500000, today=dt.date(2026, 3, 2))
    page = client.get(f"/account/{home_id}").text
    assert "Manual override" in page and "wins until you refresh" in page
    assert "$200,000" in page
    assert f'href="/account/{home_id}/valuation"' in page


def test_requests_logged_under_a_later_date_still_count(client):
    with session_scope() as active:
        active.add(RentcastUsage(day=TODAY + dt.timedelta(days=1), requests=7))
        active.flush()
        assert active.scalar(usage_total(TODAY)) == 7
