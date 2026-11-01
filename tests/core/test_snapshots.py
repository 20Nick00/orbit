import datetime as dt
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from sqlalchemy import select

from core.db import session_scope
from core.models import Account, BalanceSnapshot, MonthEndSnapshot, Transaction
from core.snapshots import (
    MixedCurrencyError,
    MonthEndConflictError,
    MonthNotOverError,
    NetWorthPoint,
    close_month,
    closed_month_balances,
    net_worth_series,
    record_daily,
)

D = Decimal
JAN_END = dt.date(2024, 1, 31)
FEB_END = dt.date(2024, 2, 29)
TODAY = dt.date(2024, 3, 1)


def test_record_daily_insert_replace_and_refresh(session, account_factory):
    account = account_factory()
    record_daily(account.id, JAN_END, "1.005", session=session)
    row = session.get(BalanceSnapshot, (account.id, JAN_END))
    assert (row.balance, row.source) == (D("1.01"), "manual")
    record_daily(account.id, JAN_END, D("2.675"), source="plaid", session=session)
    assert (row.balance, row.source) == (D("2.68"), "plaid")
    assert len(list(session.scalars(select(BalanceSnapshot)))) == 1


@pytest.mark.parametrize(
    "value,error",
    [
        (0.1 + 0.2, TypeError),
        (True, TypeError),
        (False, TypeError),
        (None, TypeError),
        ("oops", ValueError),
        ("", ValueError),
        ("NaN", ValueError),
        (D("sNaN"), ValueError),
        ("Infinity", ValueError),
        (D("-Infinity"), ValueError),
    ],
)
def test_record_daily_bad_input_writes_nothing(session, account_factory, value, error):
    account = account_factory()
    with pytest.raises(error):
        record_daily(account.id, JAN_END, value, session=session)
    assert list(session.scalars(select(BalanceSnapshot))) == []


def test_record_daily_unknown_account(session):
    with pytest.raises(ValueError, match="987654"):
        record_daily(987654, JAN_END, 100, session=session)


def test_close_uses_last_row_ignores_future_carries_forward_and_skips_missing(
    session, account_factory
):
    current = account_factory()
    old = account_factory(type="property")
    empty = account_factory()
    future_only = account_factory()
    for date, balance in [(dt.date(2024, 1, 2), 100), (JAN_END, 150), (dt.date(2024, 2, 1), 999)]:
        record_daily(current.id, date, balance, session=session)
    record_daily(old.id, dt.date(2023, 12, 5), 200000, session=session)
    record_daily(future_only.id, dt.date(2024, 2, 1), 999, session=session)
    assert close_month("2024-01", today=TODAY, session=session) == 2
    assert session.get(MonthEndSnapshot, (current.id, "2024-01")).balance == D("150.00")
    assert session.get(MonthEndSnapshot, (old.id, "2024-01")).balance == D("200000.00")
    assert session.get(MonthEndSnapshot, (empty.id, "2024-01")) is None
    assert session.get(MonthEndSnapshot, (future_only.id, "2024-01")) is None


def test_close_rerun_keeps_rows_but_counts_late_account_when_read(session, account_factory):
    account = account_factory()
    record_daily(account.id, JAN_END, 100, session=session)
    assert close_month("2024-01", today=TODAY, session=session) == 1
    january = net_worth_series(JAN_END, JAN_END, "month", session=session)
    assert january == [NetWorthPoint(JAN_END, D("100.00"), D("0.00"), D("100.00"))]
    before = session.execute(
        select(MonthEndSnapshot.account_id, MonthEndSnapshot.month, MonthEndSnapshot.balance)
    ).all()
    assert close_month(dt.date(2024, 1, 15), today=TODAY, session=session) == 0
    assert (
        session.execute(
            select(MonthEndSnapshot.account_id, MonthEndSnapshot.month, MonthEndSnapshot.balance)
        ).all()
        == before
    )
    late = account_factory()
    record_daily(late.id, JAN_END, 200, session=session)
    assert close_month("2024-01", today=TODAY, session=session) == 0
    assert session.get(MonthEndSnapshot, (account.id, "2024-01")).balance == D("100.00")
    assert session.get(MonthEndSnapshot, (late.id, "2024-01")) is None
    january = [NetWorthPoint(JAN_END, D("300.00"), D("0.00"), D("300.00"))]
    assert net_worth_series(JAN_END, JAN_END, "month", session=session) == january
    assert close_month("2024-02", today=TODAY, session=session) == 2
    assert session.get(MonthEndSnapshot, (late.id, "2024-02")).balance == D("200.00")
    assert net_worth_series(JAN_END, FEB_END, "month", session=session) == january + [
        NetWorthPoint(FEB_END, D("300.00"), D("0.00"), D("300.00"))
    ]


@pytest.mark.parametrize("flush_first", [False, True])
def test_close_reads_rounded_daily_values(session, account_factory, flush_first):
    account = account_factory()
    row = BalanceSnapshot(account_id=account.id, date=JAN_END, balance=D("1.005"), source="test")
    session.add(row)
    if flush_first:
        session.flush()
    assert close_month("2024-01", today=TODAY, session=session) == 1
    assert session.get(MonthEndSnapshot, (account.id, "2024-01")).balance == D("1.01")
    assert close_month("2024-01", today=TODAY, session=session) == 0
    for step in ("day", "month"):
        assert net_worth_series(JAN_END, JAN_END, step, session=session) == [
            NetWorthPoint(JAN_END, D("1.01"), D("0.00"), D("1.01"))
        ]


@pytest.mark.parametrize("step", ["day", "month"])
def test_series_reads_pending_string_balance_as_money(session, account_factory, step):
    account = account_factory()
    if step == "day":
        row = BalanceSnapshot(account_id=account.id, date=JAN_END, balance="1.005", source="test")
    else:
        row = MonthEndSnapshot(account_id=account.id, month="2024-01", balance="1.005")
    session.add(row)
    assert net_worth_series(JAN_END, JAN_END, step, session=session) == [
        NetWorthPoint(JAN_END, D("1.01"), D("0.00"), D("1.01"))
    ]


def test_close_reads_rounded_existing_month_end(session, account_factory):
    account = account_factory()
    record_daily(account.id, JAN_END, "1.005", session=session)
    row = MonthEndSnapshot(account_id=account.id, month="2024-01", balance=D("1.005"))
    session.add(row)
    session.flush()
    assert close_month("2024-01", today=TODAY, session=session) == 0


def test_changed_daily_balance_conflicts_without_rewriting(session, account_factory):
    account = account_factory()
    record_daily(account.id, JAN_END, 100, session=session)
    close_month("2024-01", today=TODAY, session=session)
    record_daily(account.id, JAN_END, 101, session=session)
    with pytest.raises(MonthEndConflictError) as exc:
        close_month("2024-01", today=TODAY, session=session)
    assert f"account {account.id}" in str(exc.value)
    assert "100.00" in str(exc.value) and "101.00" in str(exc.value)
    session.expire_all()
    assert session.get(MonthEndSnapshot, (account.id, "2024-01")).balance == D("100.00")


def test_conflict_checks_all_accounts_before_inserting_any(session, account_factory):
    missing = account_factory()
    first = account_factory()
    second = account_factory()
    for account in (first, second):
        record_daily(account.id, JAN_END, 100, session=session)
    assert close_month("2024-01", today=TODAY, session=session) == 2
    record_daily(missing.id, dt.date(2024, 1, 1), 200, session=session)
    for account in (first, second):
        record_daily(account.id, JAN_END, 150, session=session)
    with pytest.raises(MonthEndConflictError) as exc:
        close_month("2024-01", today=TODAY, session=session)
    assert f"account {first.id}" in str(exc.value)
    assert f"account {second.id}" in str(exc.value)
    assert session.get(MonthEndSnapshot, (missing.id, "2024-01")) is None
    # A later caller commit cannot accidentally save partial results.
    session.commit()
    stored = list(session.scalars(select(MonthEndSnapshot)))
    assert len(stored) == 2
    assert all(row.balance == D("100.00") for row in stored)


@pytest.mark.parametrize(
    "month,today",
    [
        ("2024-02", dt.date(2024, 2, 1)),
        ("2024-03", dt.date(2024, 2, 1)),
        ("2024-02", FEB_END),
        ("2024-12", dt.date(2024, 12, 31)),
    ],
)
def test_close_refuses_unfinished_months(session, month, today):
    with pytest.raises(MonthNotOverError):
        close_month(month, today=today, session=session)


def test_close_allows_next_day_and_defaults_to_today(session):
    assert close_month("2024-02", today=TODAY, session=session) == 0
    assert close_month("2024-12", today=dt.date(2025, 1, 1), session=session) == 0
    with pytest.raises(MonthNotOverError):
        close_month(dt.date.today(), session=session)


@pytest.mark.parametrize(
    "month",
    [
        None,
        202401,
        True,
        "2024-1",
        "2024-00",
        "2024-13",
        "0000-01",
        "2024-01-01",
        " 2024-01",
        dt.datetime(2024, 1, 1),
    ],
)
def test_close_invalid_month(session, month):
    with pytest.raises(ValueError):
        close_month(month, today=TODAY, session=session)


def test_daily_carry_forward_and_liabilities(session, account_factory):
    cash = account_factory()
    card = account_factory(type="cards", is_liability=True)
    start = dt.date(2024, 1, 2)
    record_daily(cash.id, dt.date(2023, 12, 31), 1000, session=session)
    record_daily(card.id, start, 100, session=session)
    record_daily(cash.id, dt.date(2024, 1, 3), 1200, session=session)
    record_daily(cash.id, dt.date(2024, 1, 5), 9999, session=session)
    points = net_worth_series(start, dt.date(2024, 1, 4), session=session)
    assert points == [
        NetWorthPoint(start, D("1000.00"), D("100.00"), D("900.00")),
        NetWorthPoint(dt.date(2024, 1, 3), D("1200.00"), D("100.00"), D("1100.00")),
        NetWorthPoint(dt.date(2024, 1, 4), D("1200.00"), D("100.00"), D("1100.00")),
    ]
    assert all(
        isinstance(value, D)
        for point in points
        for value in (point.assets, point.liabilities, point.net_worth)
    )
    with pytest.raises(FrozenInstanceError):
        points[0].assets = D("0")


@pytest.mark.parametrize("balance,net_worth", [(500, "-500.00"), (-50, "50.00")])
def test_card_default_and_overpayment_net_worth(session, balance, net_worth):
    card = Account(name="Visa", type="cards")
    session.add(card)
    session.flush()
    record_daily(card.id, JAN_END, balance, session=session)
    assert close_month("2024-01", today=TODAY, session=session) == 1
    for step in ("day", "month"):
        assert net_worth_series(JAN_END, JAN_END, step, session=session) == [
            NetWorthPoint(JAN_END, D("0.00"), D(balance), D(net_worth))
        ]


def test_daily_omits_leading_empty_days_but_keeps_recorded_zero(session, account_factory):
    account = account_factory()
    account_factory()  # Missing accounts do not invent a balance.
    start = dt.date(2024, 1, 1)
    first = dt.date(2024, 1, 3)
    assert net_worth_series(start, JAN_END, session=session) == []
    record_daily(account.id, first, 0, session=session)
    points = net_worth_series(start, dt.date(2024, 1, 4), session=session)
    assert [point.date for point in points] == [first, dt.date(2024, 1, 4)]
    assert [point.net_worth for point in points] == [D("0.00"), D("0.00")]


def test_month_uses_frozen_rows_and_omits_unclosed_month(session, account_factory):
    account = account_factory()
    record_daily(account.id, JAN_END, 100, session=session)
    close_month("2024-01", today=TODAY, session=session)
    record_daily(account.id, JAN_END, 200, session=session)
    record_daily(account.id, FEB_END, 300, session=session)
    assert net_worth_series(JAN_END, FEB_END, "month", session=session) == [
        NetWorthPoint(JAN_END, D("100.00"), D("0.00"), D("100.00")),
    ]
    assert net_worth_series(JAN_END, JAN_END, session=session)[0].net_worth == D("200.00")
    assert net_worth_series(dt.date(2024, 2, 1), FEB_END, "month", session=session) == []
    assert (
        net_worth_series(dt.date(2024, 1, 1), dt.date(2024, 1, 30), "month", session=session) == []
    )


def test_month_has_no_carry_forward_without_a_closed_row(session, account_factory):
    account = account_factory()
    record_daily(account.id, JAN_END, 100, session=session)
    close_month("2024-01", today=TODAY, session=session)
    close_month("2024-03", today=dt.date(2024, 4, 1), session=session)
    points = net_worth_series(JAN_END, dt.date(2024, 3, 31), "month", session=session)
    assert [point.date for point in points] == [JAN_END, dt.date(2024, 3, 31)]


def test_closed_month_reader_uses_hidden_months_and_exact_account_selection(
    session, account_factory
):
    hidden = account_factory(hidden=True)
    record_daily(hidden.id, JAN_END, 100, session=session)
    close_month("2024-01", today=TODAY, session=session)
    late = account_factory()
    future = account_factory()
    missing = account_factory()
    record_daily(late.id, dt.date(2024, 1, 15), 0, source="csv", session=session)
    record_daily(future.id, FEB_END, 200, source="csv", session=session)
    assert closed_month_balances(session=session) == {
        "2024-01": {late.id: D("0.00")},
        "2024-02": {late.id: D("0.00"), future.id: D("200.00")},
    }
    assert closed_month_balances(
        account_ids=[hidden.id, missing.id, future.id], session=session
    ) == {
        "2024-01": {hidden.id: D("100.00")},
        "2024-02": {hidden.id: D("100.00"), future.id: D("200.00")},
    }
    assert closed_month_balances(account_ids=[], session=session) == {
        "2024-01": {},
        "2024-02": {},
    }
    assert session.execute(
        select(MonthEndSnapshot.account_id, MonthEndSnapshot.month, MonthEndSnapshot.balance)
    ).all() == [(hidden.id, "2024-01", D("100.00"))]


def test_computed_closed_balance_participates_in_currency_check(session, account_factory):
    usd = account_factory()
    record_daily(usd.id, JAN_END, 100, session=session)
    close_month("2024-01", today=TODAY, session=session)
    eur = account_factory(currency="EUR")
    record_daily(eur.id, JAN_END, 200, source="csv", session=session)
    with pytest.raises(MixedCurrencyError, match="EUR.*USD"):
        net_worth_series(JAN_END, JAN_END, "month", session=session)
    assert session.get(MonthEndSnapshot, (eur.id, "2024-01")) is None


def test_hidden_accounts_excluded_from_both_series_but_closed(session, account_factory):
    visible = account_factory()
    hidden_asset = account_factory(hidden=True, currency="EUR")
    hidden_debt = account_factory(type="cards", is_liability=True, hidden=True)
    record_daily(hidden_asset.id, dt.date(2024, 1, 1), 10000, session=session)
    record_daily(hidden_debt.id, dt.date(2024, 1, 1), 1000, session=session)
    record_daily(visible.id, JAN_END, 100, session=session)
    assert close_month("2024-01", today=TODAY, session=session) == 3
    expected = [NetWorthPoint(JAN_END, D("100.00"), D("0.00"), D("100.00"))]
    for step in ("day", "month"):
        assert net_worth_series(dt.date(2024, 1, 1), JAN_END, step, session=session) == expected
    assert session.get(MonthEndSnapshot, (hidden_asset.id, "2024-01")).balance == D("10000.00")
    assert session.get(MonthEndSnapshot, (hidden_debt.id, "2024-01")).balance == D("1000.00")


@pytest.mark.parametrize("step", ["day", "month"])
def test_mixed_currency_raises_only_for_contributing_accounts(session, account_factory, step):
    usd = account_factory()
    eur = account_factory(currency="EUR")
    record_daily(usd.id, JAN_END, 100, session=session)
    close_month("2024-01", today=TODAY, session=session)
    assert net_worth_series(JAN_END, JAN_END, step, session=session)[0].net_worth == D("100.00")
    record_daily(eur.id, FEB_END, 200, session=session)
    close_month("2024-02", today=TODAY, session=session)
    # EUR has no data in January and must not block a January-only series.
    assert len(net_worth_series(JAN_END, JAN_END, step, session=session)) == 1
    with pytest.raises(MixedCurrencyError, match="EUR.*USD"):
        net_worth_series(JAN_END, FEB_END, step, session=session)


def test_month_currency_check_spans_the_whole_series(session, account_factory):
    usd = account_factory()
    eur = account_factory(currency="EUR")
    session.add_all(
        [
            MonthEndSnapshot(account_id=usd.id, month="2024-01", balance=100),
            MonthEndSnapshot(account_id=eur.id, month="2024-02", balance=200),
        ]
    )
    session.flush()
    with pytest.raises(MixedCurrencyError):
        net_worth_series(JAN_END, FEB_END, "month", session=session)


@pytest.mark.parametrize(
    "start,end,step",
    [
        (FEB_END, JAN_END, "day"),
        (JAN_END, FEB_END, "week"),
    ],
)
def test_bad_series_arguments(session, start, end, step):
    with pytest.raises(ValueError):
        net_worth_series(start, end, step, session=session)


@pytest.mark.parametrize(
    "case,other_type,is_liability,before,after,expected_change",
    [
        ("checking to savings", "cash", False, (1000, 500), (800, 700), 0),
        ("credit card payment", "cards", True, (1000, 300), (900, 200), 0),
        ("loan principal payment", "loans", True, (2000, 1000), (1800, 800), 0),
        ("purchase control", "cash", False, (1000, 500), (900, 500), -100),
    ],
)
def test_transfers_preserve_net_worth_and_purchase_changes_it(
    session,
    account_factory,
    case,
    other_type,
    is_liability,
    before,
    after,
    expected_change,
):
    checking = account_factory(name="Checking")
    other = account_factory(name=case, type=other_type, is_liability=is_liability)
    for account, old_balance, new_balance in zip((checking, other), before, after, strict=False):
        record_daily(account.id, JAN_END, old_balance, session=session)
        record_daily(account.id, FEB_END, new_balance, session=session)
        delta = new_balance - old_balance
        if delta:
            session.add(
                Transaction(
                    account_id=account.id,
                    date=FEB_END,
                    amount=D(delta),
                    category="purchase" if expected_change else "transfer",
                    memo=case,
                )
            )
    session.flush()
    transactions = list(session.scalars(select(Transaction).order_by(Transaction.account_id)))
    assert [(row.account_id, row.amount) for row in transactions] == [
        (account.id, D(new - old))
        for account, old, new in zip((checking, other), before, after, strict=False)
        if new != old
    ]
    assert all(not row.pending for row in transactions)
    for account, old, new in zip((checking, other), before, after, strict=False):
        assert session.get(BalanceSnapshot, (account.id, JAN_END)).balance == D(old)
        assert session.get(BalanceSnapshot, (account.id, FEB_END)).balance == D(new)
        if expected_change == 0:
            assert old != new
    assert close_month("2024-01", today=TODAY, session=session) == 2
    assert close_month("2024-02", today=TODAY, session=session) == 2
    expected_before = D(before[0] - before[1] if is_liability else sum(before))
    expected_after = D(after[0] - after[1] if is_liability else sum(after))
    for step in ("day", "month"):
        points = net_worth_series(JAN_END, FEB_END, step, session=session)
        assert len(points) == (30 if step == "day" else 2)
        assert points[0].net_worth == expected_before
        assert points[-1].net_worth == expected_after
        assert points[-1].net_worth - points[0].net_worth == D(expected_change)
        if expected_change == 0:
            assert all(point.net_worth == expected_before for point in points)
        if is_liability:
            assert points[0].liabilities == D(before[1])
            assert points[-1].liabilities == D(after[1])


def test_supplied_session_flushes_without_committing(session, account_factory):
    account = account_factory()
    account_id = account.id
    session.commit()
    record_daily(account_id, JAN_END, 100, session=session)
    assert close_month("2024-01", today=TODAY, session=session) == 1
    assert net_worth_series(JAN_END, JAN_END, session=session)[0].net_worth == D("100.00")
    assert net_worth_series(JAN_END, JAN_END, "month", session=session)[0].net_worth == D("100.00")
    session.rollback()
    assert session.get(BalanceSnapshot, (account_id, JAN_END)) is None
    assert session.get(MonthEndSnapshot, (account_id, "2024-01")) is None


def test_functions_without_session_commit(database):
    with session_scope() as active:
        account = Account(name="Checking", type="cash")
        active.add(account)
        active.flush()
        account_id = account.id
    record_daily(account_id, JAN_END, 100)
    assert close_month("2024-01", today=TODAY) == 1
    for step in ("day", "month"):
        assert net_worth_series(JAN_END, JAN_END, step)[0].net_worth == D("100.00")
