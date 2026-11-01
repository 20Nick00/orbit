"""Manual valuations and edits to file accounts, preserving unchanged imported balances."""

from sqlalchemy import select

from aggregators.file_store import is_plaid_linked
from core.db import write_savepoint
from core.models import Account, AccountFact, BalanceSnapshot, MonthEndSnapshot, _money
from core.snapshots import record_daily
from core.types import ACCOUNT_TYPES, infer_type, is_liability_type


def _save(account, name, type, value, note, today, is_liability, session):
    name = name.strip()
    if not name:
        raise ValueError("Enter an account name.")
    type = type or infer_type("manual", None, None, name, session=session)
    if type not in ACCOUNT_TYPES:
        raise ValueError("Choose a valid account type.")
    value = _money(value)
    liability = bool(is_liability) if type == "other" else is_liability_type(type)
    if (
        account.id is not None
        and liability != account.is_liability
        and session.scalar(
            select(MonthEndSnapshot.account_id)
            .where(MonthEndSnapshot.account_id == account.id)
            .limit(1)
        )
        is not None
    ):
        raise ValueError(
            "This account has closed months. Reopen them before moving it between assets and debts."
        )
    account.name, account.type = name, type
    account.is_liability = liability
    session.add(account)
    session.flush()
    fact = session.get(AccountFact, (account.id, "note"))
    if note and note.strip():
        if fact is None:
            session.add(AccountFact(account_id=account.id, key="note", value=note.strip()))
        else:
            fact.value = note.strip()
    elif fact is not None:
        session.delete(fact)
    latest = session.scalar(
        select(BalanceSnapshot.balance)
        .where(BalanceSnapshot.account_id == account.id)
        .order_by(BalanceSnapshot.date.desc())
        .limit(1)
    )
    if account.is_manual or value != latest:
        record_daily(account.id, today, value, source="manual", session=session)
    session.flush()
    return account


def create_manual_account(name, type, value, note, today, is_liability=None, *, session):
    with write_savepoint(session):
        return _save(Account(is_manual=True), name, type, value, note, today, is_liability, session)


def update_manual_account(
    account_id, name, type, value, note, today, is_liability=None, *, session
):
    account = session.get(Account, account_id)
    if account is None or is_plaid_linked(session, account):
        raise ValueError("Plaid-linked accounts cannot be edited here.")
    with write_savepoint(session):
        return _save(account, name, type, value, note, today, is_liability, session)
