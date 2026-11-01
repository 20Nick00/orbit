import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from aggregators.base import Aggregator
from aggregators.plaid import OPTIONAL_UNAVAILABLE, PlaidAggregator, PlaidError, category
from core.db import session_scope
from core.models import (
    Account,
    AccountFact,
    BalanceSnapshot,
    Holding,
    PlaidAccount,
    PlaidItem,
    Transaction,
)
from core.snapshots import record_daily
from core.types import add_override

from .fake import account, page, transaction


def rows(model):
    with session_scope() as session:
        result = list(session.scalars(select(model)))
        for row in result:
            session.expunge(row)
        return result


def test_balances_types_overrides_and_nulls(item_id, fake):
    fake.accounts = [
        account(),
        account("card", type="credit", subtype="credit card", name="Card"),
        account("loan", type="loan", subtype="mortgage", name="Mortgage"),
        account("ira", type="investment", subtype="ira", name="IRA"),
        account("unknown", balances={"current": None, "limit": None}),
    ]
    with session_scope() as session:
        add_override("health", aggregator="plaid", subtype="ira", session=session)
    aggregator = PlaidAggregator(fake)
    assert isinstance(aggregator, Aggregator)
    balances = aggregator.sync_accounts(item_id)
    assert len(balances) == 4
    assert all(isinstance(value.balance, Decimal) for value in balances)
    accounts = rows(Account)
    assert [(a.type, a.is_liability) for a in accounts] == [
        ("cash", False),
        ("cards", True),
        ("mortgage", True),
        ("health", False),
        ("cash", False),
    ]
    with session_scope() as session:
        session.get(Account, accounts[0].id).type = "invest"
        add_override("retire", aggregator="plaid", subtype="ira", session=session)
    fake.accounts[0]["name"] = "Renamed"
    fake.accounts[0]["mask"] = "5678"
    aggregator.sync_accounts(item_id)
    assert [(row.type, row.name, row.mask) for row in rows(Account)][:1] == [
        ("invest", "Renamed", "5678")
    ]
    assert rows(Account)[3].type == "health"


def test_adopts_disconnected_nonmanual_account_keeps_history(item_id, item_factory, fake):
    aggregator = PlaidAggregator(fake)
    account_id = aggregator.sync_accounts(item_id)[0].account_id
    with session_scope() as session:
        institution_id = session.get(PlaidItem, item_id).institution_id
        record_daily(account_id, dt.date(2025, 12, 31), Decimal("99"), session=session)
        session.add(
            Account(
                name="Checking",
                type="cash",
                subtype="checking",
                mask="1234",
                institution_id=institution_id,
                is_manual=True,
            )
        )
    aggregator.remove(item_id)
    assert len(rows(Account)) == 2
    assert any(
        row.date == dt.date(2025, 12, 31) and row.balance == Decimal("99")
        for row in rows(BalanceSnapshot)
    )
    new_id = item_factory("new-remote-item", institution_id=institution_id)
    fake.accounts[0]["account_id"] = "new-account-id"
    assert aggregator.sync_accounts(new_id)[0].account_id == account_id
    assert len(rows(Account)) == 2


def test_encrypted_row_and_limit(item_id, fake):
    assert "private-access-token" not in rows(PlaidItem)[0].access_token_encrypted
    fake.accounts[0]["balances"]["limit"] = 1000.1
    PlaidAggregator(fake).sync_accounts(item_id)
    assert rows(AccountFact)[0].key == "credit limit"
    assert rows(AccountFact)[0].value == "1000.1"


def test_transaction_pages_sign_rounding_modifications_and_removals(item_id, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    fake.pages = [
        page(
            added=[transaction(), transaction("pending", pending=True)],
            next_cursor="middle",
            has_more=True,
        ),
        page(
            added=[transaction("posted", pending_transaction_id="pending")],
            modified=[transaction(amount=5.25, merchant_name="Cafe")],
            next_cursor="end",
        ),
    ]
    aggregator.sync_transactions(item_id)
    assert rows(PlaidItem)[0].transactions_cursor == "end"
    txs = {row.external_id: row for row in rows(Transaction)}
    assert set(txs) == {"t1", "posted"}
    assert txs["t1"].amount == Decimal("-5.25")
    assert txs["t1"].merchant == "Cafe"
    assert txs["posted"].amount == Decimal("-0.30")
    assert txs["posted"].category == "Food And Drink"
    aggregator.sync_transactions(item_id)
    assert len(rows(Transaction)) == 2
    fake.pages = [page(removed=[{"transaction_id": "t1"}], next_cursor="")]
    aggregator.sync_transactions(item_id)
    assert [row.external_id for row in rows(Transaction)] == ["posted"]
    assert rows(PlaidItem)[0].transactions_cursor == "end"
    calls = [args for name, args in fake.calls if name == "sync_transactions"]
    assert calls[0][1:] == (None, 500)
    assert calls[1][1:] == ("middle", 500)


def test_pagination_failure_does_not_write_or_advance(item_id, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    with session_scope() as session:
        session.get(PlaidItem, item_id).transactions_cursor = "stored"
    fake.pages = [
        page(added=[transaction()], next_cursor="middle", has_more=True),
        PlaidError("INTERNAL_SERVER_ERROR"),
    ]
    with pytest.raises(PlaidError):
        aggregator.sync_transactions(item_id)
    assert not rows(Transaction)
    assert rows(PlaidItem)[0].transactions_cursor == "stored"


def test_mutation_restarts_from_stored_cursor_and_limits_attempts(item_id, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    with session_scope() as session:
        session.get(PlaidItem, item_id).transactions_cursor = "stored"
    mutation = PlaidError("TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION")
    fake.pages = [
        page(added=[transaction("discarded")], next_cursor="mid", has_more=True),
        mutation,
        page(added=[transaction("kept")]),
    ]
    aggregator.sync_transactions(item_id)
    assert [row.external_id for row in rows(Transaction)] == ["kept"]
    calls = [args[1] for name, args in fake.calls if name == "sync_transactions"]
    assert calls == ["stored", "mid", "stored"]
    fake.calls.clear()
    fake.pages = [mutation]
    with pytest.raises(PlaidError):
        aggregator.sync_transactions(item_id)
    assert len([name for name, _ in fake.calls if name == "sync_transactions"]) == 3
    assert rows(PlaidItem)[0].transactions_cursor == "cursor-1"


def test_unknown_account_syncs_once_then_refuses_cursor(item_id, fake):
    fake.pages = [page(added=[transaction(account_id="missing")])]
    with pytest.raises(PlaidError, match="UNMAPPED_ACCOUNT"):
        PlaidAggregator(fake).sync_transactions(item_id)
    assert [name for name, _ in fake.calls].count("get_accounts") == 1
    assert rows(PlaidItem)[0].transactions_cursor is None
    assert not rows(Transaction)
    fake.pages = [page(added=[transaction()])]
    PlaidAggregator(fake).sync_transactions(item_id)
    assert len(rows(Transaction)) == 1


def test_removed_transaction_scoped_to_item(item_id, item_factory, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    with session_scope() as session:
        institution_id = session.get(PlaidItem, item_id).institution_id
    second = item_factory("second", institution_id=institution_id)
    fake.accounts = [account("a2", name="Savings")]
    aggregator.sync_accounts(second)
    fake.pages = [page(added=[transaction(account_id="a2")])]
    aggregator.sync_transactions(second)
    fake.pages = [page(removed=[{"transaction_id": "t1"}])]
    aggregator.sync_transactions(item_id)
    assert len(rows(Transaction)) == 1


@pytest.mark.parametrize(
    "pfc,expected",
    [
        (None, None),
        ({}, None),
        ({"primary": "INCOME"}, "Income"),
        ({"primary": "TRANSFER_IN"}, "Transfer"),
        ({"primary": "TRANSFER_OUT"}, "Transfer"),
        ({"primary": "FOOD_AND_DRINK"}, "Food And Drink"),
        ({"primary": "LOAN_PAYMENTS", "detailed": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"}, "Transfer"),
    ],
)
def test_category_mapping(pfc, expected):
    assert category(pfc) == expected


def test_holdings_replace_only_today_preserve_nulls(item_id, fake):
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    fake.holdings = {
        "accounts": [{"account_id": "a1"}],
        "securities": [{"security_id": "s1", "ticker_symbol": "TEST", "name": "Test Fund"}],
        "holdings": [
            {
                "account_id": "a1",
                "security_id": "s1",
                "quantity": 0.1 + 0.2,
                "institution_value": 100.23,
                "cost_basis": None,
            }
        ],
    }
    yesterday, today = dt.date(2026, 10, 30), dt.date(2026, 10, 31)
    aggregator.sync_holdings(item_id, today=yesterday)
    aggregator.sync_holdings(item_id, today=today)
    aggregator.sync_holdings(item_id, today=today)
    holdings = rows(Holding)
    assert len(holdings) == 2
    assert holdings[0].qty == Decimal(str(0.1 + 0.2))
    assert holdings[0].value == Decimal("100.23")
    assert holdings[0].cost_basis is None
    assert holdings[0].ticker == "TEST"
    fake.holdings["holdings"] = []
    aggregator.sync_holdings(item_id, today=today)
    assert [row.date for row in rows(Holding)] == [yesterday]


@pytest.mark.parametrize("code", sorted(OPTIONAL_UNAVAILABLE))
@pytest.mark.parametrize(
    "method,endpoint",
    [
        ("sync_transactions", "sync_transactions"),
        ("sync_holdings", "get_holdings"),
        ("sync_liabilities", "get_liabilities"),
    ],
)
def test_optional_products_skip(item_id, fake, code, method, endpoint):
    fake.errors[endpoint] = code
    getattr(PlaidAggregator(fake), method)(item_id)
    assert rows(PlaidItem)[0].transactions_cursor is None


def test_liability_allowlist_rates_dates_nulls(item_id, fake):
    fake.accounts = [
        account(),
        account("mortgage", name="Home", type="loan", subtype="mortgage"),
        account("student", name="Student", type="loan", subtype="student"),
    ]
    aggregator = PlaidAggregator(fake)
    aggregator.sync_accounts(item_id)
    fake.liabilities = {
        "liabilities": {
            "credit": [
                {
                    "account_id": "a1",
                    "aprs": [
                        {"apr_type": "cash_apr", "apr_percentage": 30},
                        {"apr_type": "purchase_apr", "apr_percentage": 24.99},
                    ],
                    "minimum_payment_amount": 25.1,
                    "last_statement_balance": 1000,
                    "next_payment_due_date": dt.date(2026, 11, 1),
                    "account_number": "PRIVATE-NUMBER",
                }
            ],
            "mortgage": [
                {
                    "account_id": "mortgage",
                    "interest_rate": {"percentage": 6.5},
                    "next_monthly_payment": 1500,
                    "origination_principal_amount": 200000,
                    "ytd_principal_paid": 100,
                    "ytd_interest_paid": 900,
                    "next_payment_due_date": "2026-11-02",
                    "maturity_date": "2056-01-01",
                    "property_address": {"street": "PRIVATE-ADDRESS"},
                }
            ],
            "student": [
                {
                    "account_id": "student",
                    "interest_rate_percentage": 4.5,
                    "minimum_payment_amount": 55,
                    "origination_principal_amount": 20000,
                    "next_payment_due_date": "2026-11-03",
                    "expected_payoff_date": "2036-01-01",
                    "guarantor": "PRIVATE-GUARANTOR",
                }
            ],
        }
    }
    aggregator.sync_liabilities(item_id)
    mapping = {row.plaid_account_id: row.account_id for row in rows(PlaidAccount)}
    facts = {(row.account_id, row.key): row.value for row in rows(AccountFact)}
    assert {
        key: value for (account_id, key), value in facts.items() if account_id == mapping["a1"]
    } == {
        "rate": "24.99%",
        "minimum payment": "25.1",
        "statement balance": "1000",
        "due": "2026-11-01",
    }
    assert {
        key: value
        for (account_id, key), value in facts.items()
        if account_id == mapping["mortgage"]
    } == {
        "rate": "6.5%",
        "payment": "1500",
        "original amount": "200000",
        "principal ytd": "100",
        "interest ytd": "900",
        "due": "2026-11-02",
        "payoff": "2056-01-01",
    }
    assert {
        key: value for (account_id, key), value in facts.items() if account_id == mapping["student"]
    } == {
        "rate": "4.5%",
        "minimum payment": "55",
        "original amount": "20000",
        "due": "2026-11-03",
        "payoff": "2036-01-01",
    }
    assert "PRIVATE" not in repr(facts)
    fake.liabilities["liabilities"]["credit"][0]["minimum_payment_amount"] = None
    aggregator.sync_liabilities(item_id)
    with session_scope() as session:
        assert session.get(AccountFact, (mapping["a1"], "minimum payment")).value == "25.1"
        assert session.scalar(select(func.count()).select_from(AccountFact)) == 16
