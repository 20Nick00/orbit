import datetime as dt
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

from aggregators.plaid import PlaidAggregator
from aggregators.plaid_link import _accept
from core.clock import local_today
from core.config import get_settings
from core.crypto import decrypt
from core.db import get_engine, session_scope
from core.models import (
    Account,
    AccountFact,
    BalanceSnapshot,
    Institution,
    PlaidAccount,
    PlaidItem,
    PlaidLinkSession,
    Transaction,
)
from core.snapshots import net_worth_series, record_daily
from jobs import nightly

from .fake import account, page, transaction
from .test_jobs_link_settings import complete
from .test_sync import rows


def reconnect(item_id, item_factory, fake):
    aggregator = PlaidAggregator(fake)
    institution_id = rows(PlaidItem)[0].institution_id
    aggregator.remove(item_id)
    new_id = item_factory("reconnected", institution_id=institution_id)
    fake.accounts[0]["account_id"] = "new-account"
    aggregator.sync_accounts(new_id)
    return aggregator, new_id


def test_reconnect_matches_partial_history_one_for_one(item_id, item_factory, fake):
    aggregator = PlaidAggregator(fake)
    aid = aggregator.sync_accounts(item_id)[0].account_id
    fake.pages = [
        page(
            added=[
                transaction("old-1"),
                transaction("old-2"),
                transaction("older", date="2020-01-01"),
                transaction("pending", pending=True),
            ]
        )
    ]
    aggregator.sync_transactions(item_id)
    with session_scope() as session:
        session.add(
            Transaction(
                account_id=aid,
                date=dt.date(2026, 10, 1),
                amount=Decimal("-0.30"),
                external_id=None,
                pending=True,
            )
        )
        session.get(Account, aid).hidden = True
    aggregator, new_id = reconnect(item_id, item_factory, fake)
    assert not rows(Account)[0].hidden
    assert {row.external_id for row in rows(Transaction)} == {
        "old-1",
        "old-2",
        "older",
        "pending",
        None,
    }
    watermark = rows(PlaidAccount)[0].legacy_transaction_max_id
    for index in (1, 2):
        fake.pages = [
            page(
                added=[transaction(f"new-{index}", account_id="new-account")],
                next_cursor=f"partial-{index}",
            )
        ]
        aggregator.sync_transactions(new_id)
        assert len(rows(Transaction)) == 5
        # Re-delivery of an already inserted ID must not consume another legacy match.
        aggregator.sync_transactions(new_id)
        assert len(rows(Transaction)) == 5
    result = {row.external_id: row for row in rows(Transaction)}
    assert set(result) == {"new-1", "new-2", "older", "pending", None}
    assert result["new-1"].id > watermark and result["new-2"].id > watermark
    assert len(rows(Account)) == 1
    assert rows(PlaidItem)[0].transactions_cursor == "partial-2"


def test_reconnect_does_not_reuse_deleted_legacy_ids(item_id, item_factory, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    fake.pages = [page(added=[transaction("old")])]
    aggregator.sync_transactions(item_id)
    aggregator, new_id = reconnect(item_id, item_factory, fake)
    for external_id in ("new-1", "new-2"):
        fake.pages = [page(added=[transaction(external_id, account_id="new-account")])]
        aggregator.sync_transactions(new_id)
    assert {row.external_id for row in rows(Transaction)} == {"new-1", "new-2"}


def test_legacy_deletion_and_cursor_roll_back_together(item_id, item_factory, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    fake.pages = [page(added=[transaction("old-1"), transaction("old-2")])]
    aggregator.sync_transactions(item_id)
    aggregator, new_id = reconnect(item_id, item_factory, fake)
    watermark = rows(PlaidAccount)[0].legacy_transaction_max_id
    fake.pages = [
        page(
            added=[
                transaction("new-1", account_id="new-account"),
                transaction("broken", account_id="new-account", date="invalid-date"),
            ]
        )
        | {"transactions_update_status": "HISTORICAL_UPDATE_COMPLETE"}
    ]
    with pytest.raises(ValueError):
        aggregator.sync_transactions(new_id)
    assert {row.external_id for row in rows(Transaction)} == {"old-1", "old-2"}
    assert rows(PlaidItem)[0].transactions_cursor is None
    assert rows(PlaidAccount)[0].legacy_transaction_max_id == watermark
    fake.pages = [page(added=[transaction("new-1", account_id="new-account")])]
    aggregator.sync_transactions(new_id)
    assert {row.external_id for row in rows(Transaction)} == {"new-1", "old-2"}


def test_new_account_id_keeps_account_history_and_balance(item_id, fake):
    aggregator = PlaidAggregator(fake)
    aid = aggregator.sync_accounts(item_id)[0].account_id
    fake.pages = [page(added=[transaction("old")])]
    aggregator.sync_transactions(item_id)
    fake.accounts = [account("renamed", name="Renamed Checking")]
    assert aggregator.sync_accounts(item_id)[0].account_id == aid
    assert len(rows(Account)) == len(rows(BalanceSnapshot)) == 1
    assert rows(Account)[0].name == "Renamed Checking"
    mapping = rows(PlaidAccount)[0]
    assert mapping.plaid_account_id == "renamed" and mapping.missing_since is None
    assert mapping.legacy_transaction_max_id == rows(Transaction)[0].id
    assert rows(PlaidItem)[0].transactions_cursor is None
    fake.pages = [page(added=[transaction("new", account_id="renamed")])]
    aggregator.sync_transactions(item_id)
    assert [row.external_id for row in rows(Transaction)] == ["new"]


@pytest.mark.parametrize("mask,count", [("1234", 2), (None, 1)])
def test_changed_id_ambiguous_or_null_mask_does_not_guess(item_id, fake, mask, count):
    aggregator = PlaidAggregator(fake)
    fake.accounts = [account(f"old-{i}", mask=mask) for i in range(count)]
    aggregator.sync_accounts(item_id)
    fake.accounts = [account("new", name="Renamed", mask=mask)]
    aggregator.sync_accounts(item_id)
    assert len(rows(Account)) == count + 1
    assert len([row for row in rows(PlaidAccount) if row.missing_since]) == count


def test_missing_since_keeps_first_date_and_clears_on_return(item_id, fake, monkeypatch):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    first = dt.date(2026, 10, 1)
    monkeypatch.setattr("aggregators.plaid.local_today", lambda: first)
    fake.accounts = []
    aggregator.sync_accounts(item_id)
    assert rows(PlaidAccount)[0].missing_since == first
    monkeypatch.setattr("aggregators.plaid.local_today", lambda: dt.date(2026, 10, 2))
    aggregator.sync_accounts(item_id)
    assert rows(PlaidAccount)[0].missing_since == first
    fake.accounts = [account()]
    aggregator.sync_accounts(item_id)
    assert rows(PlaidAccount)[0].missing_since is None


def test_connect_records_balance_before_background_sync(web_client, fake):
    item_id, duplicate = _accept(PlaidAggregator(fake), "public")
    assert item_id and not duplicate
    assert rows(BalanceSnapshot)[0].date == local_today()
    response = web_client.get("/dashboard")
    assert response.status_code == 200
    assert '<div class="big num" id="nwBig">$123</div>' in response.text


@pytest.mark.parametrize(
    "kind,subtype,expected",
    [
        ("depository", "checking", Decimal("24.50")),
        ("credit", "credit card", None),
        ("loan", "mortgage", None),
    ],
)
def test_available_balance_is_only_used_for_depository(item_id, fake, kind, subtype, expected):
    fake.accounts = [
        account(
            type=kind,
            subtype=subtype,
            balances={
                "current": None,
                "available": "24.50",
                "iso_currency_code": "USD",
            },
        )
    ]
    PlaidAggregator(fake).sync_accounts(item_id)
    snapshots = rows(BalanceSnapshot)
    assert ([row.balance for row in snapshots] or [None]) == [expected]


def test_optional_product_discovers_account_and_records_balance(item_id, fake):
    fake.liabilities["liabilities"]["credit"] = [
        {"account_id": None, "minimum_payment_amount": 99},
        {"account_id": "a1", "minimum_payment_amount": 10},
    ]
    PlaidAggregator(fake).sync_liabilities(item_id)
    assert len(rows(BalanceSnapshot)) == 1
    assert [(row.key, row.value) for row in rows(AccountFact)] == [("minimum payment", "10")]


@pytest.mark.parametrize(
    "currency,unofficial,expected",
    [
        (None, "BTC", "BTC"),
        ("CAD", None, "CAD"),
        (None, None, "XXX"),
    ],
)
def test_non_usd_hidden_and_cannot_unhide(
    web_client, item_id, fake, currency, unofficial, expected
):
    fake.accounts.append(
        account(
            "foreign",
            name="Foreign",
            balances={
                "current": "0.50",
                "iso_currency_code": currency,
                "unofficial_currency_code": unofficial,
            },
        )
    )
    PlaidAggregator(fake).sync_accounts(item_id)
    foreign = next(row for row in rows(Account) if row.name == "Foreign")
    assert foreign.hidden and foreign.currency == expected
    assert len(rows(BalanceSnapshot)) == 2
    assert web_client.get("/dashboard").status_code == 200
    response = web_client.post(
        f"/settings/accounts/{foreign.id}/visibility", data={"hidden": "false"}
    )
    assert "must stay hidden" in response.text
    assert "Not in USD, so hidden from totals" in response.text
    assert next(row for row in rows(Account) if row.id == foreign.id).hidden


def test_existing_currency_and_visibility_are_preserved(item_id, item_factory, fake):
    aggregator = PlaidAggregator(fake)
    aid = aggregator.sync_accounts(item_id)[0].account_id
    fake.accounts[0]["balances"]["iso_currency_code"] = "CAD"
    aggregator.sync_accounts(item_id)
    assert rows(Account)[0].currency == "USD" and not rows(Account)[0].hidden
    with session_scope() as session:
        session.get(Account, aid).currency = "CAD"
        session.get(Account, aid).hidden = True
    reconnect(item_id, item_factory, fake)
    assert rows(Account)[0].hidden


def test_settings_visibility_missing_and_disconnected(web_client, item_id, fake):
    fake.accounts = [account(balances={"current": None, "iso_currency_code": "USD"})]
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    aid = rows(Account)[0].id
    fake.accounts = []
    aggregator.sync_accounts(item_id)
    text = web_client.get("/settings").text
    assert all(
        value in text
        for value in (
            "Checking",
            "1234",
            "No balance reported",
            "No longer reported by Plaid",
            "Sandbox mode:",
        )
    )
    for hidden in ("true", "false"):
        response = web_client.post(
            f"/settings/accounts/{aid}/visibility",
            data={"hidden": hidden},
            follow_redirects=False,
        )
        assert response.status_code == 303 and response.headers["location"] == "/settings"
        assert rows(Account)[0].hidden == (hidden == "true")
    aggregator.remove(item_id)
    assert "Disconnected accounts" in web_client.get("/settings").text
    assert (
        web_client.post(
            f"/settings/accounts/{aid}/visibility",
            data={"hidden": "true"},
        ).status_code
        == 200
    )
    assert rows(Account)[0].hidden
    assert "Hidden" in web_client.get("/settings").text


def test_visibility_only_allows_plaid_accounts(web_client):
    with session_scope() as session:
        institution = Institution(name="File import", aggregator="csv")
        session.add(institution)
        session.flush()
        other = Account(name="Other", type="cash", institution_id=institution.id)
        session.add(other)
        session.flush()
        aid = other.id
    for account_id in (aid, 999):
        assert (
            web_client.post(
                f"/settings/accounts/{account_id}/visibility",
                data={"hidden": "true"},
            ).status_code
            == 404
        )


@pytest.mark.parametrize(
    "path",
    [
        "/settings/accounts/1/visibility",
        "/settings/accounts/1/closed",
        "/settings/plaid/items/1/forget",
    ],
)
def test_new_forms_require_same_origin(web_client, path):
    assert (
        web_client.post(
            path,
            data={"hidden": "true"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize("endpoint", ["get_accounts", "get_institution"])
def test_initial_outage_completes_link_and_retries(database, configured, fake, endpoint, caplog):
    aggregator = PlaidAggregator(fake)
    link = aggregator.link("connect", "banking", "http://localhost/callback")
    complete(fake)
    fake.errors[endpoint] = "INTERNAL_SERVER_ERROR"
    result = aggregator.finish(link.state)
    assert result.message == "connected" and len(result.item_ids) == 1
    assert not rows(PlaidLinkSession)
    saved = rows(PlaidItem)[0]
    assert decrypt(saved.access_token_encrypted) == "private-access-token"
    assert saved.last_error_code == "INTERNAL_SERVER_ERROR"
    assert "INTERNAL_SERVER_ERROR" in caplog.text and "private" not in caplog.text
    fake.errors.clear()
    assert nightly.sync_item(saved.id, client=fake)
    assert rows(PlaidItem)[0].status == "ok" and len(rows(BalanceSnapshot)) == 1


def test_unexpected_initial_failure_also_keeps_connection(database, configured, fake, monkeypatch):
    aggregator = PlaidAggregator(fake)
    link = aggregator.link("connect", "banking", "http://localhost/callback")
    complete(fake)

    def unavailable(token):
        raise ValueError("private-response-must-not-be-logged")

    monkeypatch.setattr(fake, "get_accounts", unavailable)
    result = aggregator.finish(link.state)
    assert result.message == "connected" and not rows(PlaidLinkSession)
    assert rows(PlaidItem)[0].last_error_code == "SYNC_FAILED"


@pytest.mark.parametrize("mask,subtype", [(None, "checking"), ("1234", "savings")])
def test_second_login_null_mask_or_different_subtype_allowed(
    item_id, item_factory, fake, mask, subtype
):
    aggregator = PlaidAggregator(fake)
    fake.accounts = [account(mask=mask)]
    aggregator.sync_accounts(item_id)
    second = item_factory("second", institution_id=rows(PlaidItem)[0].institution_id)
    fake.accounts = [account("second-account", mask=mask, subtype=subtype)]
    aggregator.sync_accounts(second)
    assert len(rows(Account)) == 2


def test_existing_item_renamed_into_collision_keeps_syncing(item_id, item_factory, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    second = item_factory("second", institution_id=rows(PlaidItem)[0].institution_id)
    fake.accounts = [account("second-account", name="Other")]
    aggregator.sync_accounts(second)
    fake.accounts[0]["name"] = "Checking"
    assert nightly.sync_item(second, client=fake)
    assert len(rows(Account)) == 2


def test_nightly_first_sync_duplicate_kept_for_removal(web_client, item_id, item_factory, fake):
    PlaidAggregator(fake).sync_accounts(item_id)
    second = item_factory("second", institution_id=rows(PlaidItem)[0].institution_id)
    fake.accounts = [account("second-account")]
    assert not nightly.sync_item(second, client=fake)
    duplicate = next(row for row in rows(PlaidItem) if row.id == second)
    assert duplicate.last_error_code == "DUPLICATE_CONNECTION"
    assert not any(name == "remove_item" for name, _ in fake.calls)
    text = web_client.get("/settings").text
    assert "This duplicates another connection. Remove it." in text
    assert "Orbit will retry" not in text


def test_lost_key_forget_then_reconnect_adopts_history(web_client, item_id, fake, monkeypatch):
    aggregator = PlaidAggregator(fake)
    fake.pages = [page(added=[transaction("old")])]
    assert nightly.sync_item(item_id, client=fake)
    aid = rows(Account)[0].id
    assert web_client.post(f"/settings/plaid/items/{item_id}/forget").status_code == 409
    aggregator.link("reauth", "banking", "http://localhost/callback", item_id)
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    assert not nightly.sync_item(item_id, client=fake)
    assert rows(PlaidItem)[0].last_error_code == "TOKEN_UNREADABLE"
    text = web_client.get("/settings").text
    assert "Forget this connection" in text and "may keep being billed" in text
    assert "Orbit will retry" not in text
    assert f'action="/settings/plaid/items/{item_id}/remove"' not in text
    fake.calls.clear()
    response = web_client.post(f"/settings/plaid/items/{item_id}/forget", follow_redirects=False)
    assert response.status_code == 303
    assert not fake.calls
    assert not rows(PlaidItem) and not rows(PlaidAccount) and not rows(PlaidLinkSession)
    assert len(rows(Transaction)) == len(rows(BalanceSnapshot)) == 1
    fake.exchange["item_id"] = "reconnected"
    fake.accounts[0]["account_id"] = "new-account"
    new_id, duplicate = _accept(aggregator, "public")
    assert not duplicate and rows(Account)[0].id == aid and len(rows(Account)) == 1
    fake.pages = [page(added=[transaction("new", account_id="new-account")])]
    assert nightly.sync_item(new_id, client=fake)
    assert [row.external_id for row in rows(Transaction)] == ["new"]


@pytest.mark.parametrize(
    "code",
    [
        "ITEM_LOGIN_REQUIRED",
        "ITEM_LOCKED",
        "INVALID_CREDENTIALS",
        "INVALID_MFA",
        "USER_PERMISSION_REVOKED",
        "ACCESS_NOT_GRANTED",
    ],
)
def test_authentication_failures_need_login(item_id, fake, code):
    fake.errors["get_accounts"] = code
    assert not nightly.sync_item(item_id, client=fake)
    assert rows(PlaidItem)[0].status == "login_required"


@pytest.mark.parametrize("exit_key", ["exit", "on_exit"])
def test_cancelled_reauth_stays_pending(item_id, fake, exit_key):
    aggregator = PlaidAggregator(fake)
    link = aggregator.link("reauth", "banking", "http://localhost/callback", item_id)
    fake.link_data = {"link_sessions": [{"finished_at": dt.datetime.now(dt.UTC), exit_key: {}}]}
    assert aggregator.finish(link.state).message == "pending"
    assert len(rows(PlaidLinkSession)) == 1
    assert not any(name == "get_accounts" for name, _ in fake.calls)
    fake.link_data = {"link_sessions": [{"on_success": {}}]}
    assert aggregator.finish(link.state).message == "reauthenticated"


def test_link_and_account_network_calls_do_not_hold_write_lock(
    database, configured, fake, monkeypatch
):
    # A separate writer must work while each mocked remote call is in flight.
    for method in ("get_link_token", "get_accounts", "get_item", "get_institution"):
        original = getattr(fake, method)

        def unlocked(*args, original=original):
            with get_engine().connect() as connection:
                connection.exec_driver_sql("PRAGMA busy_timeout=0")
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                connection.rollback()
            return original(*args)

        monkeypatch.setattr(fake, method, unlocked)
    aggregator = PlaidAggregator(fake)
    link = aggregator.link("connect", "banking", "http://localhost/callback")
    complete(fake)
    result = aggregator.finish(link.state)
    assert result.message == "connected"
    assert len(rows(Account)) == len(rows(BalanceSnapshot)) == 1


@pytest.mark.parametrize("key", ["", "mistyped-key"])
def test_missing_or_invalid_key_never_offers_forget(web_client, item_id, monkeypatch, key):
    original = rows(PlaidItem)[0].access_token_encrypted
    monkeypatch.setenv("FERNET_KEY", key)
    get_settings.cache_clear()
    text = web_client.get("/settings").text
    assert "FERNET_KEY" in text and "Forget this connection" not in text
    assert web_client.post(f"/settings/plaid/items/{item_id}/forget").status_code == 409
    assert rows(PlaidItem)[0].access_token_encrypted == original


@pytest.mark.parametrize("disconnected", [False, True])
def test_mark_closed_preserves_history_and_return_restores_balance(
    web_client, item_id, fake, disconnected
):
    aggregator = PlaidAggregator(fake)
    aid = aggregator.sync_accounts(item_id)[0].account_id
    today = local_today()
    yesterday = today - dt.timedelta(days=1)
    balance = Decimal("123.45")
    record_daily(aid, yesterday, balance, source="plaid")
    if disconnected:
        aggregator.remove(item_id)
    else:
        fake.accounts = []
        aggregator.sync_accounts(item_id)
    assert f'action="/settings/accounts/{aid}/closed"' in web_client.get("/settings").text
    before = net_worth_series(yesterday, today)
    response = web_client.post(f"/settings/accounts/{aid}/closed", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/settings"
    after = net_worth_series(yesterday, today)
    assert after[0] == before[0]
    assert after[-1].net_worth == before[-1].net_worth - balance
    assert not rows(Account)[0].hidden
    assert '<span class="demo">Closed</span>' in web_client.get("/settings").text
    if not disconnected:
        fake.accounts = [account()]
        aggregator.sync_accounts(item_id)
        assert net_worth_series(yesterday, today)[-1].net_worth == balance
        assert '<span class="demo">Closed</span>' not in web_client.get("/settings").text


def test_mark_closed_refuses_healthy_account(web_client, item_id, fake):
    aid = PlaidAggregator(fake).sync_accounts(item_id)[0].account_id
    assert f'action="/settings/accounts/{aid}/closed"' not in web_client.get("/settings").text
    assert web_client.post(f"/settings/accounts/{aid}/closed").status_code == 409
    assert rows(BalanceSnapshot)[0].balance == Decimal("123.45")


def test_old_missing_account_does_not_repoint(item_id, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    with session_scope() as session:
        session.get(PlaidAccount, "a1").missing_since = local_today() - dt.timedelta(days=1)
    fake.accounts = [account("new")]
    aggregator.sync_accounts(item_id)
    assert len(rows(Account)) == 2
    assert {row.plaid_account_id for row in rows(PlaidAccount)} == {"a1", "new"}


def test_two_new_remotes_for_one_missing_mapping_do_not_repoint(item_id, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    fake.accounts = [account("new-1"), account("new-2")]
    aggregator.sync_accounts(item_id)
    assert len(rows(Account)) == 3
    assert {row.plaid_account_id for row in rows(PlaidAccount)} == {"a1", "new-1", "new-2"}


@pytest.mark.parametrize(
    "status",
    [
        None,
        "TRANSACTIONS_UPDATE_STATUS_UNKNOWN",
        "NOT_READY",
        "INITIAL_UPDATE_COMPLETE",
        "HISTORICAL_UPDATE_COMPLETE",
    ],
)
def test_history_matching_stops_only_after_historical_completion(
    item_id, item_factory, fake, status
):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    fake.pages = [page(added=[transaction("old", amount="2.75")])]
    aggregator.sync_transactions(item_id)
    aggregator, new_id = reconnect(item_id, item_factory, fake)
    fake.pages = [page() | {"transactions_update_status": status}]
    aggregator.sync_transactions(new_id)
    complete = status == "HISTORICAL_UPDATE_COMPLETE"
    assert (rows(PlaidAccount)[0].legacy_transaction_max_id is None) == complete
    fake.pages = [page(added=[transaction("new", account_id="new-account", amount="2.75")])]
    aggregator.sync_transactions(new_id)
    assert {row.external_id for row in rows(Transaction)} == (
        {"old", "new"} if complete else {"new"}
    )


@pytest.mark.parametrize("terminal_complete", [False, True])
def test_multi_page_history_clears_only_after_terminal_completion(
    item_id, item_factory, fake, terminal_complete
):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    fake.pages = [page(added=[transaction("old-1"), transaction("old-2")])]
    aggregator.sync_transactions(item_id)
    aggregator, new_id = reconnect(item_id, item_factory, fake)
    fake.pages = [
        page(
            added=[transaction("new-1", account_id="new-account")], has_more=True, next_cursor="p1"
        )
        | {"transactions_update_status": "HISTORICAL_UPDATE_COMPLETE"},
        page(added=[transaction("new-2", account_id="new-account")], next_cursor="p2")
        | {
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE"
            if terminal_complete
            else None
        },
    ]
    aggregator.sync_transactions(new_id)
    assert {row.external_id for row in rows(Transaction)} == {"new-1", "new-2"}
    assert (rows(PlaidAccount)[0].legacy_transaction_max_id is None) == terminal_complete


def test_old_pending_row_that_posted_is_removed_when_history_completes(item_id, item_factory, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    fake.pages = [page(added=[transaction("old-pending", pending=True)])]
    aggregator.sync_transactions(item_id)
    aggregator, new_id = reconnect(item_id, item_factory, fake)
    posted = page(added=[transaction("posted", account_id="new-account")])
    fake.pages = [posted | {"transactions_update_status": "INITIAL_UPDATE_COMPLETE"}]
    aggregator.sync_transactions(new_id)
    assert {row.external_id for row in rows(Transaction)} == {"old-pending", "posted"}
    fake.pages = [page() | {"transactions_update_status": "HISTORICAL_UPDATE_COMPLETE"}]
    aggregator.sync_transactions(new_id)
    assert [row.external_id for row in rows(Transaction)] == ["posted"]


def test_history_watermark_clear_rolls_back_with_transaction(item_id, item_factory, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    fake.pages = [page(added=[transaction("old")])]
    aggregator.sync_transactions(item_id)
    aggregator, new_id = reconnect(item_id, item_factory, fake)
    watermark = rows(PlaidAccount)[0].legacy_transaction_max_id
    fake.pages = [page() | {"transactions_update_status": "HISTORICAL_UPDATE_COMPLETE"}]
    with pytest.raises(RuntimeError):
        with session_scope() as session:
            aggregator.sync_transactions(new_id, session=session)
            assert session.get(PlaidAccount, "new-account").legacy_transaction_max_id is None
            raise RuntimeError("Abort this pull")
    assert rows(PlaidAccount)[0].legacy_transaction_max_id == watermark
    assert rows(PlaidItem)[0].transactions_cursor is None


def test_repoint_keeps_pending_rows_and_same_id_keeps_cursor(item_id, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    fake.pages = [page(added=[transaction("pending", pending=True)], next_cursor="old-cursor")]
    aggregator.sync_transactions(item_id)
    aggregator.sync_accounts(item_id)
    assert rows(PlaidItem)[0].transactions_cursor == "old-cursor"
    fake.accounts = [account("new-id")]
    aggregator.sync_accounts(item_id)
    assert rows(PlaidItem)[0].transactions_cursor is None
    assert [row.external_id for row in rows(Transaction)] == ["pending"]


def test_repoint_discovered_during_transaction_pull_restarts_history(item_id, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    fake.pages = [page(added=[transaction("old")], next_cursor="old-cursor")]
    aggregator.sync_transactions(item_id)
    fake.accounts = [account("new-id")]
    fake.pages = [
        page(added=[transaction("delta", account_id="new-id")], next_cursor="delta-cursor"),
        page(added=[transaction("history", account_id="new-id")], next_cursor="history-cursor")
        | {"transactions_update_status": "HISTORICAL_UPDATE_COMPLETE"},
    ]
    fake.calls.clear()
    aggregator.sync_transactions(item_id)
    cursors = [args[1] for name, args in fake.calls if name == "sync_transactions"]
    assert cursors == ["old-cursor", None]
    assert rows(PlaidItem)[0].transactions_cursor == "history-cursor"
    assert {row.external_id for row in rows(Transaction)} == {"history"}
