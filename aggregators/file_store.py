"""Shared, transactional file storage. No raw imported values belong in logs."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from core.clock import local_today
from core.db import write_savepoint
from core.models import (
    Account,
    BalanceEdit,
    BalanceSnapshot,
    ImportRule,
    Institution,
    MonthEndSnapshot,
    Transaction,
    _money,
)
from core.rollups import TYPES
from core.snapshots import _month_end, record_daily


class ImportError(ValueError):
    """A safe, user-facing import error (never include file contents)."""


@dataclass
class ImportRow:
    number: int
    date: date | None = None
    amount: Decimal | None = None
    memo: str | None = None
    merchant: str | None = None
    category: str | None = None
    amount_marker: str | None = None
    balance_only: bool = False


@dataclass
class ImportResult:
    account_id: int
    added: int = 0
    duplicates_skipped: int = 0
    unreadable_rows: list[int] = field(default_factory=list)
    balances_written: int = 0
    balance_days_skipped: int = 0
    date_range: tuple[date, date] | None = None
    balances: list[tuple[date, Decimal]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    is_liability: bool = False
    account_name: str = ""
    type_label: str = ""
    missing_balance: bool = False
    protected_balance_days: set[date] = field(default_factory=set)


def dedupe_key(account_id, date, amount, memo):
    payload = [
        account_id,
        date.isoformat(),
        int(_money(amount) * 100),
        " ".join((memo or "").casefold().split()),
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


def is_plaid_linked(session, account):
    institution = (
        session.get(Institution, account.institution_id) if account.institution_id else None
    )
    return bool(institution and institution.aggregator == "plaid")


def valid_target(session, account):
    return not account.is_manual and not is_plaid_linked(session, account)


def require_new_name(session, name):
    existing = next(
        (
            account
            for account in session.scalars(select(Account))
            if account.name.strip().casefold() == name.strip().casefold()
        ),
        None,
    )
    if existing is not None:
        if not valid_target(session, existing):
            raise ImportError(
                f'An account named "{name.strip()}" already exists '
                "and can't receive file imports. Use a different name."
            )
        raise ImportError(
            f'An account named "{name.strip()}" already exists. '
            "Choose it from the list so duplicates are caught."
        )


def get_rule(session, source, matcher):
    return session.scalar(
        select(ImportRule)
        .where(ImportRule.source_kind == source, ImportRule.matcher == matcher)
        .order_by(ImportRule.id)
    )


def save_rule(session, source, matcher, mapping):
    rule = get_rule(session, source, matcher)
    if rule is None:
        rule = ImportRule(source_kind=source, matcher=matcher, column_map=mapping)
        session.add(rule)
    else:
        rule.column_map = dict(mapping)
    session.flush()


def protected_balance_days(session, account_id, days):
    """Protect edits, closed account months, and the daily basis of later closes."""
    months = session.scalars(
        select(MonthEndSnapshot.month)
        .where(MonthEndSnapshot.account_id == account_id)
        .order_by(MonthEndSnapshot.month)
    ).all()
    existing_days = session.scalars(
        select(BalanceSnapshot.date)
        .where(BalanceSnapshot.account_id == account_id)
        .order_by(BalanceSnapshot.date)
    ).all()
    protected = set(
        session.scalars(select(BalanceEdit.date).where(BalanceEdit.account_id == account_id))
    )
    protected.update(
        session.scalars(
            select(BalanceSnapshot.date).where(
                BalanceSnapshot.account_id == account_id, BalanceSnapshot.source == "manual"
            )
        )
    )
    result = set()
    for day in days:
        month = day.isoformat()[:7]
        if day in protected or month in months:
            result.add(day)
            continue
        next_month = bisect_right(months, month)
        if next_month < len(months):
            _, last = _month_end(months[next_month])
            next_day = bisect_right(existing_days, day)
            if next_day == len(existing_days) or existing_days[next_day] > last:
                result.add(day)
    return result


def write_import(session, account, rows, balances, source):
    """Participate in the caller's transaction; a failed write rolls back this batch."""
    if source not in {"csv", "ofx"} or not valid_target(session, account):
        raise ImportError("Choose an account that is neither manual nor linked through Plaid.")
    result = ImportResult(
        account.id,
        is_liability=account.is_liability,
        account_name=account.name,
        type_label=TYPES[account.type],
    )
    with write_savepoint(session):
        protected = protected_balance_days(session, account.id, balances)
        occurrences = Counter()
        days = []
        for row in rows:
            if row.balance_only:
                continue
            if row.date is None or row.amount is None:
                result.unreadable_rows.append(row.number)
                continue
            key = dedupe_key(account.id, row.date, row.amount, row.memo)
            external_id = f"file:{key}:{occurrences[key]}"
            occurrences[key] += 1
            statement = (
                insert(Transaction)
                .values(
                    account_id=account.id,
                    date=row.date,
                    amount=row.amount,
                    memo=row.memo or None,
                    merchant=row.merchant or None,
                    category=row.category or None,
                    external_id=external_id,
                    pending=False,
                )
                .on_conflict_do_nothing(index_elements=["account_id", "external_id"])
            )
            if session.execute(statement).rowcount:
                result.added += 1
            else:
                result.duplicates_skipped += 1
            days.append(row.date)
        changed_days = []
        for day, balance in sorted(balances.items()):
            days.append(day)
            if day in protected:
                result.balance_days_skipped += 1
                result.protected_balance_days.add(day)
                continue
            existing_balance = session.scalar(
                select(BalanceSnapshot.balance).where(
                    BalanceSnapshot.account_id == account.id, BalanceSnapshot.date == day
                )
            )
            if existing_balance is None or existing_balance != _money(balance):
                changed_days.append(day)
            record_daily(account.id, day, balance, source=source, session=session)
            result.balances_written += 1
            result.balances.append((day, balance))
        if changed_days:
            account_months = select(MonthEndSnapshot.month).where(
                MonthEndSnapshot.account_id == account.id
            )
            missing_months = session.scalars(
                select(MonthEndSnapshot.month)
                .where(MonthEndSnapshot.month.not_in(account_months))
                .distinct()
                .order_by(MonthEndSnapshot.month)
            ).all()
            earliest = min(changed_days)
            included = [month for month in missing_months if _month_end(month)[1] >= earliest]
            if len(included) == 1:
                first = date.fromisoformat(included[0] + "-01").strftime("%b %Y")
                result.notices.append(f"Net worth for {first} now reflects this account.")
            elif included:
                first = date.fromisoformat(included[0] + "-01").strftime("%b %Y")
                last = date.fromisoformat(included[-1] + "-01").strftime("%b %Y")
                result.notices.append(
                    f"Net worth for closed months from {first} to {last} now reflects this account."
                )
        if days:
            result.date_range = min(days), max(days)
        session.flush()
        result.missing_balance = (
            session.scalar(
                select(BalanceSnapshot.account_id)
                .where(
                    BalanceSnapshot.account_id == account.id,
                    BalanceSnapshot.date <= local_today(),
                )
                .limit(1)
            )
            is None
        )
    return result
