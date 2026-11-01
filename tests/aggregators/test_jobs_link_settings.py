import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from aggregators.plaid import PlaidAggregator, PlaidError
from core.config import get_settings
from core.db import session_scope
from core.models import Account, BalanceSnapshot, PlaidItem, PlaidLinkSession, Transaction
from jobs import nightly
from jobs.scheduler import build_scheduler
from tests.csrf_client import TestClient

from .fake import account, page, transaction
from .test_sync import rows


def complete(fake):
    fake.link_data = {
        "link_sessions": [
            {
                "results": {"item_add_results": [{"public_token": "private-public-token"}]},
                "finished_at": dt.datetime.now(dt.UTC),
            }
        ]
    }


def test_nightly_snapshots_idempotence_skip_and_no_secrets(item_id, fake, caplog, monkeypatch):
    recorded = []
    original = nightly.record_daily

    def recording(*args, **kwargs):
        recorded.append(kwargs["source"])
        return original(*args, **kwargs)

    monkeypatch.setattr(nightly, "record_daily", recording)
    caplog.set_level("INFO")
    today = dt.date(2026, 10, 31)
    assert nightly.run(today=today, client=fake).succeeded == 1
    assert nightly.run(today=today, client=fake).succeeded == 1
    assert recorded == ["plaid", "plaid"]
    assert len(rows(BalanceSnapshot)) == 1
    assert rows(BalanceSnapshot)[0].source == "plaid"
    count = len(fake.calls)
    assert nightly.run(today=today, client=fake, skip_synced_today=True).skipped == 1
    assert len(fake.calls) == count
    assert rows(PlaidItem)[0].last_synced_at.date() == today
    assert "private" not in caplog.text


def test_one_failure_does_not_stop_next_item(item_id, item_factory, fake, monkeypatch, caplog):
    with session_scope() as session:
        institution_id = session.get(PlaidItem, item_id).institution_id
    second = item_factory("second-item", token="second-token", institution_id=institution_id)
    original = fake.get_accounts

    def accounts(token):
        if token == "private-access-token":
            raise PlaidError("ITEM_LOGIN_REQUIRED", "ITEM_ERROR", "safe-request")
        return original(token)

    monkeypatch.setattr(fake, "get_accounts", accounts)
    outcome = nightly.run(client=fake)
    assert (outcome.succeeded, outcome.failed) == (1, 1)
    data = {row.id: row for row in rows(PlaidItem)}
    assert data[item_id].status == "login_required"
    assert data[item_id].last_error_code == "ITEM_LOGIN_REQUIRED"
    assert data[second].status == "ok"
    assert "private-access-token" not in caplog.text
    assert "second-token" not in caplog.text


def test_later_failure_keeps_earlier_committed_data(item_id, fake):
    fake.errors["get_holdings"] = "INTERNAL_SERVER_ERROR"
    fake.pages = [page(added=[transaction()])]
    assert nightly.sync_item(item_id, client=fake) is False
    assert len(rows(Account)) == len(rows(BalanceSnapshot)) == len(rows(Transaction)) == 1
    assert rows(PlaidItem)[0].status == "error"
    assert rows(PlaidItem)[0].last_synced_at is None


def test_scheduler_configuration(configured):
    jobs = {job.id: job for job in build_scheduler(get_settings()).get_jobs()}
    job = jobs["plaid_nightly"]
    assert job.func is nightly.run
    assert job.coalesce and job.max_instances == 1
    assert job.misfire_grace_time is None
    assert job.kwargs == {"skip_synced_today": True}
    assert job.next_run_time is not None
    fields = {field.name: str(field) for field in job.trigger.fields}
    assert fields["hour"] == "2" and fields["minute"] == "0"


def test_scheduler_has_no_plaid_job_without_keys():
    settings = SimpleNamespace(plaid_configured=False, timezone="UTC")
    jobs = {job.id for job in build_scheduler(settings).get_jobs()}
    assert "plaid_nightly" not in jobs


def test_scheduler_lifespan_starts_and_stops(database, configured, monkeypatch):
    import app.main as main

    events = []

    class Scheduler:
        running = False

        def __init__(self, settings):
            pass

        def start(self):
            self.running = True
            events.append("start")

        def shutdown(self, wait):
            assert wait is True
            events.append("stop")

    monkeypatch.setenv("ORBIT_SCHEDULER_ENABLED", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(main, "build_scheduler", Scheduler)
    with TestClient(main.create_app()):
        assert events == ["start"]
    assert events == ["start", "stop"]


@pytest.mark.parametrize(
    "product_set,primary,optional",
    [
        ("banking", ["transactions"], ["investments", "liabilities"]),
        ("investments", ["investments"], ["transactions", "liabilities"]),
    ],
)
def test_hosted_link_parameters_and_encryption(
    database, configured, fake, product_set, primary, optional
):
    result = PlaidAggregator(fake).link("connect", product_set, "http://localhost/callback")
    assert result.url == "https://secure.plaid.com/link/example"
    values = fake.calls[0][1][0]
    assert values["products"] == primary
    assert values["optional_products"] == optional
    assert values["transactions"] == {"days_requested": 730}
    assert values["user"] == {"client_user_id": "orbit-owner"}
    assert (
        values["hosted_link"]["completion_redirect_uri"]
        == "http://localhost/callback?state=" + result.state
    )
    assert "webhook" not in values
    link = rows(PlaidLinkSession)[0]
    assert len(link.state) >= 32
    assert "private-link-token" not in link.link_token_encrypted


def test_finished_connection_and_duplicate_guard(database, configured, fake):
    aggregator = PlaidAggregator(fake)
    start = aggregator.link("connect", "banking", "http://localhost/callback")
    assert aggregator.finish(start.state).message == "pending"
    assert len(rows(PlaidLinkSession)) == 1
    complete(fake)
    connected = aggregator.finish(start.state)
    assert connected.message == "connected"
    assert len(connected.item_ids) == 1
    assert not rows(PlaidLinkSession)
    assert len(rows(PlaidItem)) == len(rows(Account)) == 1
    fake.exchange = {"item_id": "duplicate-item", "access_token": "new-private-token"}
    fake.accounts[0]["account_id"] = "new-account-id"
    start = aggregator.link("connect", "banking", "http://localhost/callback")
    assert aggregator.finish(start.state).message == "duplicate"
    assert len(rows(PlaidItem)) == len(rows(Account)) == 1
    assert ("remove_item", ("new-private-token",)) in fake.calls


def test_all_link_item_results_processed(database, configured, fake, monkeypatch):
    aggregator = PlaidAggregator(fake)
    start = aggregator.link("connect", "banking", "http://localhost/callback")
    complete(fake)
    fake.link_data["link_sessions"][0]["results"]["item_add_results"].append(
        {"public_token": "second"}
    )

    def exchange(token):
        fake.exchange = {"item_id": token, "access_token": token + "-access"}
        fake.accounts = [account(token, name=token)]
        return fake.exchange

    monkeypatch.setattr(fake, "exchange_public_token", exchange)
    result = aggregator.finish(start.state)
    assert len(result.item_ids) == len(rows(PlaidItem)) == 2


def test_reauth_update_mode_then_full_sync(item_id, fake):
    aggregator = PlaidAggregator(fake)
    with session_scope() as session:
        session.get(PlaidItem, item_id).status = "login_required"
    result = aggregator.link("reauth", "banking", "http://localhost/callback", item_id)
    values = fake.calls[0][1][0]
    assert "access_token" in values
    assert "products" not in values and "transactions" not in values
    assert aggregator.finish(result.state).message == "pending"
    complete(fake)
    assert aggregator.finish(result.state).message == "reauthenticated"
    assert rows(PlaidItem)[0].status == "ok"
    assert not [call for call in fake.calls if call[0] == "exchange_public_token"]
    assert len(rows(BalanceSnapshot)) == 1


@pytest.mark.parametrize("code", [None, "ITEM_NOT_FOUND", "INVALID_ACCESS_TOKEN"])
def test_remove_keeps_accounts_history(item_id, fake, code):
    assert nightly.sync_item(item_id, client=fake)
    if code:
        fake.errors["remove_item"] = code
    PlaidAggregator(fake).remove(item_id)
    assert not rows(PlaidItem)
    assert len(rows(Account)) == len(rows(BalanceSnapshot)) == 1


def test_remove_failure_keeps_encrypted_token(item_id, fake):
    fake.errors["remove_item"] = "INTERNAL_SERVER_ERROR"
    token = rows(PlaidItem)[0].access_token_encrypted
    with pytest.raises(PlaidError):
        PlaidAggregator(fake).remove(item_id)
    assert rows(PlaidItem)[0].access_token_encrypted == token


def test_metadata_outage_keeps_token_for_later_sync(database, configured, fake):
    aggregator = PlaidAggregator(fake)
    start = aggregator.link("connect", "banking", "http://localhost/callback")
    complete(fake)
    fake.errors["get_item"] = "INTERNAL_SERVER_ERROR"
    assert aggregator.finish(start.state).message == "connected"
    assert not rows(PlaidLinkSession)
    item = rows(PlaidItem)[0]
    assert "private-access-token" not in item.access_token_encrypted
    assert not rows(Account)
    fake.errors.clear()
    assert nightly.sync_item(item.id, client=fake)
    assert len(rows(Account)) == 1


def test_failed_duplicate_removal_cannot_double_count(database, configured, fake):
    aggregator = PlaidAggregator(fake)
    complete(fake)
    first = aggregator.link("connect", "banking", "http://localhost/callback")
    first_id = aggregator.finish(first.state).item_ids[0]
    assert nightly.sync_item(first_id, client=fake)
    fake.exchange = {"item_id": "duplicate-item", "access_token": "new-private-token"}
    fake.accounts[0]["account_id"] = "new-account-id"
    fake.errors["remove_item"] = "INTERNAL_SERVER_ERROR"
    second = aggregator.link("connect", "banking", "http://localhost/callback")
    with pytest.raises(PlaidError):
        aggregator.finish(second.state)
    duplicate = next(item for item in rows(PlaidItem) if item.id != first_id)
    assert nightly.sync_item(duplicate.id, client=fake) is False
    assert len(rows(Account)) == len(rows(BalanceSnapshot)) == 1
    assert len(rows(PlaidItem)) == 2


def test_settings_list_and_nav_without_tokens(web_client, item_id, fake):
    PlaidAggregator(fake).sync_accounts(item_id)
    response = web_client.get("/settings")
    assert response.status_code == 200
    assert "Example Bank" in response.text and "1 account" in response.text
    assert 'href="/settings" aria-current="page"' in response.text
    assert "private" not in response.text
    assert rows(PlaidItem)[0].access_token_encrypted not in response.text


def test_settings_missing_config(web_client, monkeypatch):
    for key in ("PLAID_CLIENT_ID", "PLAID_SECRET", "FERNET_KEY"):
        monkeypatch.setenv(key, "")
    get_settings.cache_clear()
    response = web_client.get("/settings")
    for key in ("PLAID_CLIENT_ID", "PLAID_SECRET", "FERNET_KEY"):
        assert key in response.text
    assert "disabled" in response.text


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "https://evil.example"},
        {"Origin": "https://testserver:8443"},
        {"Origin": "null"},
    ],
)
@pytest.mark.parametrize(
    "path", ["connect", "items/1/reauth", "items/1/remove", "sessions/1/check"]
)
def test_all_settings_posts_reject_cross_origin(web_client, headers, path):
    assert web_client.post("/settings/plaid/" + path, headers=headers).status_code == 403


def test_connect_redirect_validation_and_check_again(web_client, fake, monkeypatch):
    queued = []
    monkeypatch.setattr("app.routers.settings.sync_item", lambda item_id: queued.append(item_id))
    response = web_client.post(
        "/settings/plaid/connect",
        data={"product_set": "banking"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "https://secure.plaid.com/link/example"
    assert (
        web_client.post("/settings/plaid/connect", data={"product_set": "invalid"}).status_code
        == 422
    )
    complete(fake)
    link_id = rows(PlaidLinkSession)[0].id
    response = web_client.post(f"/settings/plaid/sessions/{link_id}/check", follow_redirects=False)
    assert response.headers["location"] == "/settings?message=connected"
    assert queued == [rows(PlaidItem)[0].id]


def test_expired_unknown_callback_harmless(web_client, fake):
    result = PlaidAggregator(fake).link("connect", "banking", "http://localhost/callback")
    with session_scope() as session:
        session.scalar(select(PlaidLinkSession)).expires_at = dt.datetime(2000, 1, 1)
    fake.calls.clear()
    for state in (result.state, "unknown"):
        response = web_client.get(
            "/settings/plaid/callback", params={"state": state}, follow_redirects=False
        )
        assert response.headers["location"] == "/settings?message=expired"
    assert not fake.calls and not rows(PlaidLinkSession)


def test_remove_form_failure_and_success(web_client, item_id, fake):
    assert nightly.sync_item(item_id, client=fake)
    fake.errors["remove_item"] = "INTERNAL_SERVER_ERROR"
    response = web_client.post(f"/settings/plaid/items/{item_id}/remove", follow_redirects=False)
    assert response.headers["location"] == "/settings?message=error"
    assert len(rows(PlaidItem)) == 1
    fake.errors.clear()
    response = web_client.post(f"/settings/plaid/items/{item_id}/remove", follow_redirects=False)
    assert response.headers["location"] == "/settings?message=removed"
    assert not rows(PlaidItem)
    assert len(rows(Account)) == len(rows(BalanceSnapshot)) == 1


def test_public_url_sets_the_plaid_return_address(monkeypatch):
    from types import SimpleNamespace

    from app.routers.settings import _callback_url
    from core.config import get_settings

    request = SimpleNamespace(url_for=lambda name: "http://orbit.example/settings/plaid/callback")
    get_settings.cache_clear()
    assert _callback_url(request).startswith("http://orbit.example/")
    monkeypatch.setenv("ORBIT_PUBLIC_URL", "https://orbit.example/")
    get_settings.cache_clear()
    assert _callback_url(request) == "https://orbit.example/settings/plaid/callback"
    get_settings.cache_clear()
