import datetime as dt
from decimal import Decimal

import pytest

from core.models import AlmanacNote, MonthEndSnapshot, Paystub, PaystubLine, Transaction
from core.rollups import (
    almanac_records,
    category_spend,
    investments_vs_cash,
    monthly_rollups,
    yearly_rollups,
)
from core.snapshots import MixedCurrencyError, close_month, net_worth_series, record_daily

TODAY = dt.date(2026, 3, 15)


def end(session, account, month, value):
    session.add(MonthEndSnapshot(account_id=account.id, month=month, balance=Decimal(value)))
    session.flush()


def tx(session, account, amount, category=None, *, day=dt.date(2026, 2, 15), pending=False):
    session.add(
        Transaction(
            account_id=account.id,
            date=day,
            amount=Decimal(amount),
            category=category,
            pending=pending,
        )
    )
    session.flush()


def feb(session):
    return monthly_rollups("2026-02", "2026-02", today=TODAY, session=session)[0]


@pytest.mark.parametrize(
    "category",
    [
        "transfer",
        "TRANSFER_IN",
        "Transfer Out",
        "credit card payment",
        "loan_payments_credit_card_payment",
    ],
)
def test_own_transfers_do_not_change_flow_or_market_gain(session, account_factory, category):
    bank = account_factory()
    other = account_factory()
    for month, left, right in (("2026-01", 1000, 0), ("2026-02", 700, 300)):
        end(session, bank, month, left)
        end(session, other, month, right)
    tx(session, bank, -300, category)
    tx(session, other, 300, category)
    row = feb(session)
    assert row.income == row.spending == row.kept == row.market_gain == 0


def test_card_purchase_once_payment_excluded_refund_not_income(session, account_factory):
    bank = account_factory()
    card = account_factory(type="cards")
    tx(session, card, -100, "Shopping")
    tx(session, card, 20, "Shopping")
    tx(session, card, 80, "credit card payment")
    tx(session, bank, -80, "credit card payment")
    row = feb(session)
    assert row.spending == 80 and row.income == 0 and row.kept == -80
    assert row.savings_rate is None


def test_refunds_can_net_negative_spending(session, account_factory):
    account = account_factory()
    tx(session, account, -10, "Shopping")
    tx(session, account, 30, "Shopping")
    assert feb(session).spending == -20
    assert feb(session).income == 0
    assert category_spend(today=TODAY, session=session) == []


def test_hidden_pending_and_internal_investment_activity_excluded(session, account_factory):
    visible = account_factory()
    hidden = account_factory(hidden=True)
    tx(session, visible, 100, "salary", pending=True)
    tx(session, hidden, 100, "salary")
    record_daily(hidden.id, TODAY, 100000, session=session)
    end(session, hidden, "2026-02", 100000)
    for kind in ("invest", "retire", "health", "crypto", "insure", "property"):
        account = account_factory(type=kind)
        tx(session, account, 100, "dividends")
        tx(session, account, -50, "Purchase")
    row = feb(session)
    assert row.income == row.spending == 0
    assert row.net_worth_end is None
    assert category_spend(today=TODAY, session=session) == []


def test_income_categories_blank_and_reversals(session, account_factory):
    account = account_factory()
    for category in (
        "income",
        "paycheck",
        "salary",
        "wages",
        "bonus",
        "interest",
        "dividend",
        "dividends",
        "INCOME_WAGES",
        "",
        None,
    ):
        tx(session, account, 100, category)
    tx(session, account, -100, "salary")
    tx(session, account, -20)
    row = feb(session)
    assert row.income == 1000 and row.spending == 20
    assert row.savings_rate == Decimal("0.98")


def test_bank_to_brokerage_is_not_an_extra_contribution(session, account_factory):
    bank = account_factory()
    broker = account_factory(type="invest")
    end(session, bank, "2026-01", 1000)
    end(session, broker, "2026-01", 1000)
    end(session, bank, "2026-02", 1100)
    end(session, broker, "2026-02", 1300)
    tx(session, bank, 500, "paycheck")
    tx(session, bank, -100, "Groceries")
    tx(session, bank, -300, "Transfer Out")
    tx(session, broker, 300, "TRANSFER_IN")
    row = feb(session)
    assert row.kept == 400 and row.net_worth_change == 400
    assert row.contributions == 0 and row.market_gain == 0


def test_paystub_savings_and_match_but_not_health_premium(session, account_factory):
    retire = account_factory(type="retire")
    end(session, retire, "2026-01", 1000)
    end(session, retire, "2026-02", 1150)
    stub = Paystub(employer="Test", pay_date=dt.date(2026, 2, 15))
    session.add(stub)
    session.flush()
    for label, group, amount in (
        ("401(k)", "pretax", -100),
        ("401k match", "employer", 50),
        ("Health premium", "employer", 60),
        ("HSA", "benefit", 70),
    ):
        session.add(PaystubLine(paystub_id=stub.id, label=label, group_=group, amount=amount))
    row = feb(session)
    assert row.contributions == 150 and row.market_gain == 0


def test_exact_formula_and_new_account_growth(session, account_factory):
    bank = account_factory()
    new = account_factory(type="invest")
    end(session, bank, "2026-01", 1000)
    end(session, bank, "2026-02", 1100)
    end(session, new, "2026-02", 150)
    record_daily(new.id, dt.date(2026, 2, 1), 100, session=session)
    record_daily(new.id, dt.date(2026, 2, 28), 150, session=session)
    tx(session, bank, 200, "salary")
    tx(session, bank, -100, "Shopping")
    row = feb(session)
    assert row.net_worth_end == 1250 and row.net_worth_change == 250
    assert row.new_accounts == 100 and row.kept == 100
    assert row.market_gain == 50
    assert row.market_gain == row.net_worth_change - row.new_accounts - row.contributions - row.kept


def test_opening_uses_first_balance_and_sign_for_liability(session, account_factory):
    bank = account_factory()
    debt = account_factory(type="loans")
    end(session, bank, "2026-01", 1000)
    end(session, bank, "2026-02", 1000)
    end(session, debt, "2026-02", 190)
    record_daily(debt.id, dt.date(2026, 2, 5), 195, session=session)
    row = feb(session)
    assert row.new_accounts == -195
    assert row.net_worth_change == -190 and row.market_gain == 5


def test_late_debt_counted_in_closed_month_is_not_new_next_month(session, account_factory):
    bank = account_factory()
    debt = account_factory(type="loans")
    end(session, bank, "2026-01", 1000)
    end(session, bank, "2026-02", 1000)
    end(session, debt, "2026-02", 190)
    record_daily(debt.id, dt.date(2026, 1, 20), 200, session=session)
    record_daily(debt.id, dt.date(2026, 2, 5), 195, session=session)
    row = feb(session)
    assert row.new_accounts == 0
    assert row.net_worth_change == 10 and row.market_gain == 10


def test_month_closed_only_for_hidden_accounts_is_not_shown(session, account_factory):
    hidden = account_factory(hidden=True)
    visible = account_factory()
    record_daily(hidden.id, dt.date(2026, 1, 31), 500, session=session)
    close_month("2026-01", today=dt.date(2026, 3, 1), session=session)
    record_daily(visible.id, dt.date(2026, 2, 10), 100, session=session)
    points = net_worth_series(dt.date(2026, 1, 1), dt.date(2026, 2, 28), "month", session=session)
    assert points == []
    rows = monthly_rollups("2026-01", "2026-01", today=dt.date(2026, 3, 1), session=session)
    assert rows[0].net_worth_end is None and not rows[0].closed


def test_first_month_unclosed_past_month_and_live_month(session, account_factory):
    account = account_factory()
    record_daily(account.id, dt.date(2026, 1, 1), 100, session=session)
    record_daily(account.id, dt.date(2026, 2, 10), 200, session=session)
    record_daily(account.id, TODAY, 300, session=session)
    record_daily(account.id, TODAY + dt.timedelta(days=1), 999, session=session)
    end(session, account, "2026-01", 100)
    rows = monthly_rollups("2026-01", "2026-03", today=TODAY, session=session)
    assert rows[0].net_worth_end == 100 and rows[0].market_gain is None
    assert rows[0].closed and not rows[1].closed
    assert rows[1].net_worth_end is rows[1].net_worth_change is rows[1].market_gain is None
    assert rows[2].net_worth_end == 300 and not rows[2].closed
    assert rows[2].net_worth_change is None
    mix = investments_vs_cash("2026-01", "2026-03", today=TODAY, session=session)
    assert mix[0]["cash"] == 100
    assert mix[0]["closed"] and not mix[1]["closed"] and not mix[2]["closed"]
    assert mix[1]["cash"] is None and mix[1]["investments"] is None
    assert mix[2]["cash"] == 300


def test_yearly_identity_and_year_boundaries(session, account_factory):
    account = account_factory()
    end(session, account, "2024-12", 1000)
    for month in range(1, 13):
        end(session, account, f"2025-{month:02}", 1000 + month * 100)
        tx(session, account, 60, "salary", day=dt.date(2025, month, 15))
        tx(session, account, -10, "Food", day=dt.date(2025, month, 16))
    record_daily(account.id, dt.date(2026, 1, 1), 2300, session=session)
    session.add(AlmanacNote(year=2025, note="A year"))
    years = yearly_rollups(today=TODAY, session=session)
    assert [row.year for row in years] == [2024, 2025, 2026]
    prior = years[1]
    months = monthly_rollups("2025-01", "2025-12", today=TODAY, session=session)
    assert prior.market_gain == sum(row.market_gain for row in months) == 600
    assert prior.net_worth_change == 1200 and prior.income == 720 and prior.spending == 120
    assert prior.note == "A year" and not prior.ytd
    assert years[2].ytd and years[2].net_worth_change == 100


def test_earliest_paystub_year_and_missing_december(session, account_factory):
    account = account_factory()
    session.add(Paystub(employer="Test", pay_date=dt.date(2024, 12, 31)))
    record_daily(account.id, dt.date(2025, 12, 31), 500, session=session)
    years = yearly_rollups(today=TODAY, session=session)
    assert [r.year for r in years] == [2024, 2025, 2026]
    assert years[1].net_worth_end is None
    assert years[2].net_worth_end == 500 and years[2].net_worth_change is None


def test_category_90_day_window_refunds_sorting_and_blank(session, account_factory):
    account = account_factory()
    tx(session, account, -100, "Food", day=TODAY - dt.timedelta(days=89))
    tx(session, account, -1000, "Food", day=TODAY - dt.timedelta(days=90))
    tx(session, account, 20, "food", day=TODAY)
    tx(session, account, -10, "", day=TODAY)
    tx(session, account, 40, "", day=TODAY)
    tx(session, account, 300, "income", day=TODAY)
    tx(session, account, -999, "transfer", day=TODAY)
    tx(session, account, -999, "Future", day=TODAY + dt.timedelta(days=1))
    rows = category_spend(today=TODAY, session=session)
    assert rows == [
        {"category": "Food", "spending": Decimal(80)},
        {"category": "Uncategorized", "spending": Decimal(10)},
    ]


def test_records_ath_best_month_and_milestones(session, account_factory):
    bank = account_factory()
    new = account_factory(type="invest")
    # Starting above 100k is not a crossing. Later thresholds are crossed once.
    for day, value in (
        (dt.date(2026, 1, 1), 110000),
        (dt.date(2026, 1, 2), 260000),
        (dt.date(2026, 1, 3), 240000),
        (dt.date(2026, 1, 4), 510000),
    ):
        record_daily(bank.id, day, value, session=session)
    end(session, bank, "2026-01", 510000)
    end(session, bank, "2026-02", 510100)
    end(session, new, "2026-02", 1000000)
    record_daily(new.id, dt.date(2026, 2, 1), 1000000, session=session)
    record_daily(bank.id, dt.date(2026, 2, 28), 510100, session=session)
    records = almanac_records(today=TODAY, session=session)
    assert records["all_time_high"] == 1510100
    assert records["all_time_high_date"] == dt.date(2026, 2, 28)
    assert records["best_month"] == "2026-02" and records["best_month_change"] == 100
    crossed = {row["threshold"]: row["date"] for row in records["milestones"]}
    assert Decimal(100000) not in crossed
    assert crossed[Decimal(250000)] == dt.date(2026, 1, 2)
    assert crossed[Decimal(500000)] == dt.date(2026, 1, 4)
    assert len(crossed) == len(records["milestones"])
    assert records["years_tracked"] == 1
    assert records["average_savings_rate"] is None


def test_empty_records_and_mixed_currency(session, account_factory):
    records = almanac_records(today=TODAY, session=session)
    assert records["all_time_high"] is records["best_month"] is records["years_tracked"] is None
    for currency in ("USD", "EUR"):
        account = account_factory(currency=currency)
        record_daily(account.id, TODAY, 100, session=session)
    with pytest.raises(MixedCurrencyError):
        monthly_rollups("2026-01", "2026-03", today=TODAY, session=session)


def test_mix_includes_all_investment_types_and_excludes_hidden(session, account_factory):
    for kind, amount in (
        ("cash", 100),
        ("invest", 200),
        ("retire", 300),
        ("health", 400),
        ("crypto", 500),
        ("property", 900),
    ):
        account = account_factory(type=kind)
        end(session, account, "2026-02", amount)
        record_daily(account.id, TODAY, amount + 10, session=session)
    hidden = account_factory(type="invest", hidden=True)
    end(session, hidden, "2026-02", 10000)
    record_daily(hidden.id, TODAY, 10000, session=session)
    rows = investments_vs_cash("2026-02", "2026-03", today=TODAY, session=session)
    assert rows[0]["investments"] == 1400 and rows[0]["cash"] == 100
    assert rows[1]["investments"] == 1440 and rows[1]["cash"] == 110


def test_best_month_ranks_growth_without_openings_and_excludes_open_month(session, account_factory):
    bank = account_factory()
    new = account_factory(type="invest")
    end(session, bank, "2025-12", 100)
    end(session, bank, "2026-01", 200)
    end(session, bank, "2026-02", 250)
    end(session, new, "2026-02", 10000)
    record_daily(new.id, dt.date(2026, 2, 1), 10000, session=session)
    record_daily(bank.id, TODAY, 50000, session=session)
    records = almanac_records(today=TODAY, session=session)
    assert records["best_month"] == "2026-01" and records["best_month_change"] == 100


def test_records_average_yearly_rates(session, account_factory):
    bank = account_factory()
    tx(session, bank, 100, "salary", day=dt.date(2025, 1, 1))
    tx(session, bank, -50, "Food", day=dt.date(2025, 1, 1))
    tx(session, bank, 100, "salary", day=TODAY)
    tx(session, bank, -70, "Food", day=TODAY)
    records = almanac_records(today=TODAY, session=session)
    assert records["years_tracked"] == 2
    assert records["average_savings_rate"] == Decimal("0.4")


@pytest.mark.parametrize("category", ["Interest", "dividend", "DIVIDENDS"])
def test_negative_interest_is_card_spending(session, account_factory, category):
    card = account_factory(type="cards")
    tx(session, card, -30, category)
    row = feb(session)
    assert row.income == 0 and row.spending == 30 and row.kept == -30
    assert category_spend(today=TODAY, session=session) == [
        {"category": category, "spending": Decimal(30)}
    ]
