from datetime import date
from decimal import Decimal as D

from sqlalchemy.orm import Session

from core.db import get_engine
from core.models import Account, BalanceSnapshot, Transaction
from paystubs.accounts import net_worth_change, spending, uncategorized_count

BASELINE = date(2025, 12, 31)
END = date(2026, 10, 30)


def seed_accounts(session):
    asset = Account(name="Synthetic savings", type="cash")
    debt = Account(name="Synthetic debt", type="loans", is_liability=True)
    hidden = Account(name="Hidden synthetic", type="cash", hidden=True)
    session.add_all([asset, debt, hidden])
    session.flush()
    for account, old, new in ((asset, "600000", "750000"), (debt, "100000", "110000")):
        # An earlier balance must carry forward to Dec 31, not be mistaken for a missing baseline.
        session.add_all(
            [
                BalanceSnapshot(
                    account_id=account.id, date=date(2025, 12, 20), balance=D(old), source="test"
                ),
                BalanceSnapshot(account_id=account.id, date=END, balance=D(new), source="test"),
            ]
        )
    transactions = [
        (asset, "-61000", None, False, date(2026, 1, 2)),
        (asset, "1000", "shopping", False, END),
        (asset, "-7000", "TRANSFER", False, END),
        (asset, "8000", "Income", False, END),
        (asset, "-9000", None, True, END),
        (hidden, "-10000", None, False, END),
        (asset, "-11000", None, False, BASELINE),
        (asset, "-12000", None, False, date(2026, 11, 1)),
    ]
    session.add_all(
        Transaction(account_id=a.id, amount=D(amount), category=category, pending=pending, date=day)
        for a, amount, category, pending, day in transactions
    )
    session.commit()
    return asset, debt, hidden


def test_spending_and_change_hand_calculated(paystub_env):
    with Session(get_engine()) as session:
        seed_accounts(session)
        assert spending(session, date(2026, 1, 1), END).value == D("60000")
        result = net_worth_change(session, BASELINE, END)
        assert result.value == D("140000")
        assert result.reason is None and result.unbaselined_accounts == 0


def test_no_data_is_missing_not_zero(paystub_env):
    with Session(get_engine()) as session:
        assert spending(session, date(2026, 1, 1), END).value is None
        result = net_worth_change(session, BASELINE, END)
        assert result.value is None
        assert result.reason == "Needs a balance from on or before Dec 31, 2025"


def test_first_snapshot_midyear_and_future_snapshot(paystub_env):
    with Session(get_engine()) as session:
        account = Account(name="New account", type="cash")
        future = Account(name="Future account", type="cash")
        session.add_all([account, future])
        session.flush()
        session.add_all(
            [
                BalanceSnapshot(
                    account_id=account.id, date=date(2026, 6, 1), balance=D("50"), source="test"
                ),
                BalanceSnapshot(
                    account_id=future.id, date=date(2027, 1, 1), balance=D("100"), source="test"
                ),
            ]
        )
        session.commit()
        result = net_worth_change(session, BASELINE, END)
        assert result.value is None and result.unbaselined_accounts == 1
        seed_accounts(session)
        result = net_worth_change(session, BASELINE, END)
        assert result.value == D("140050") and result.unbaselined_accounts == 1


def test_mixed_currencies_have_plain_reason(paystub_env):
    with Session(get_engine()) as session:
        _, debt, _ = seed_accounts(session)
        debt.currency = "EUR"
        session.commit()
        result = net_worth_change(session, BASELINE, END)
        assert result.value is None and "more than one currency" in result.reason


def test_known_transactions_with_only_exclusions_is_zero(paystub_env):
    with Session(get_engine()) as session:
        account = Account(name="Synthetic account", type="cash")
        session.add(account)
        session.flush()
        session.add(
            Transaction(account_id=account.id, date=END, amount=D("-5"), category="Transfer")
        )
        session.add(
            Transaction(
                account_id=account.id, date=date(2026, 1, 2), amount=D("5"), category="Transfer"
            )
        )
        session.commit()
        assert spending(session, date(2026, 1, 1), END).value == D("0")


def test_baseline_only_is_carried_to_end(paystub_env):
    with Session(get_engine()) as session:
        account = Account(name="Recorded balance", type="cash")
        session.add(account)
        session.flush()
        session.add(
            BalanceSnapshot(account_id=account.id, date=BASELINE, balance=D("10"), source="test")
        )
        session.commit()
        assert net_worth_change(session, BASELINE, END).value == D("0")


def test_uncategorized_count_ignores_pending_hidden_and_other_dates(paystub_env):
    with Session(get_engine()) as session:
        seed_accounts(session)
        assert uncategorized_count(session, date(2026, 1, 1), END) == 1
        account = session.query(Account).filter_by(name="Synthetic savings").one()
        session.add(Transaction(account_id=account.id, date=END, amount=D("-5"), category=""))
        session.flush()
        assert uncategorized_count(session, date(2026, 1, 1), END) == 2
