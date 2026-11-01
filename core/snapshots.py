"""Daily history, permanent month closes, and totals from recorded balances."""

import calendar
import datetime as dt
import re
from bisect import bisect_right
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, func, literal, select, union, update
from sqlalchemy.dialects.sqlite import insert

from core.db import session_scope
from core.models import (
    Account,
    BalanceEdit,
    BalanceSnapshot,
    MonthEndArchive,
    MonthEndReopen,
    MonthEndSnapshot,
    _money,
)

__all__ = [
    "MonthNotOverError",
    "MonthEndConflictError",
    "MixedCurrencyError",
    "NetWorthPoint",
    "record_daily",
    "close_month",
    "closed_month_balances",
    "net_worth_series",
    "MonthEndLockedError",
    "SnapshotNotFoundError",
    "MonthNotClosedError",
    "locked_days",
    "edit_snapshot",
    "reopen_month",
]


class MonthNotOverError(ValueError):
    pass


class MonthEndConflictError(ValueError):
    pass


class MixedCurrencyError(ValueError):
    pass


class MonthEndLockedError(ValueError):
    pass


class SnapshotNotFoundError(LookupError):
    pass


class MonthNotClosedError(ValueError):
    pass


@dataclass(frozen=True)
class NetWorthPoint:
    date: dt.date
    assets: Decimal
    liabilities: Decimal
    net_worth: Decimal


def _month_end(month):
    if type(month) is dt.date:
        first = month.replace(day=1)
    elif isinstance(month, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        try:
            first = dt.date.fromisoformat(month + "-01")
        except ValueError as exc:
            raise ValueError(f"Invalid month: {month!r}") from exc
    else:
        raise ValueError("Month must be YYYY-MM or a date")
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first.isoformat()[:7], last


def _lock_out_writers(session):
    # The driver only opens a transaction at the first write, so a rebuild's reads
    # would otherwise let a sync commit between them and then cache figures that
    # miss it. BEGIN IMMEDIATE because upgrading a WAL read to a write can fail.
    connection = session.connection()
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def record_daily(account_id, date, balance, *, source="manual", session=None):
    """Record a daily balance, replacing any existing balance for that day.

    For a liability, positive means money owed. Negative means the account is
    overpaid (a credit balance), which is legitimate and raises net worth.
    Importers must convert sources that show debts as negative numbers before
    calling this function.
    """
    balance = _money(balance)
    with nullcontext(session) if session is not None else session_scope() as active:
        if active.get(Account, account_id) is None:
            raise ValueError(f"Unknown account id: {account_id}")
        statement = insert(BalanceSnapshot).values(
            account_id=account_id, date=date, balance=balance, source=source
        )
        statement = statement.on_conflict_do_update(
            index_elements=["account_id", "date"],
            set_={"balance": statement.excluded.balance, "source": statement.excluded.source},
        ).returning(BalanceSnapshot)
        # Refresh a row the caller may already have loaded in this session.
        active.scalars(statement, execution_options={"populate_existing": True}).one()
        active.flush()


def close_month(month, *, today=None, session=None, skip_incomplete_files=False) -> int:
    """Close an ended month without changing any previously closed month.

    A late account gets no row in an already closed month but is counted when
    read. By default every account with a balance gets a row. Automatic jobs
    pass ``skip_incomplete_files=True`` to leave CSV/OFX balances dated before
    month-end computed, even when later balances exist. A correction to an
    existing row needs ``reopen_month``, then the month must be closed again.
    ``today`` defaults to the server's local date. A scheduled job should pass
    the owner's local date, otherwise a server on UTC could close a month hours early.
    """
    month, last = _month_end(month)
    if last >= (today if today is not None else dt.date.today()):
        raise MonthNotOverError(f"Month {month} has not fully ended")
    with nullcontext(session) if session is not None else session_scope() as active:
        _lock_out_writers(active)
        latest = {}
        latest_rows = {}
        for row in active.execute(
            select(
                BalanceSnapshot.account_id,
                BalanceSnapshot.date,
                BalanceSnapshot.balance,
                BalanceSnapshot.source,
            )
            .where(BalanceSnapshot.date <= last)
            .order_by(BalanceSnapshot.date)
        ):
            latest[row.account_id] = row.balance
            latest_rows[row.account_id] = row
        existing = {
            row.account_id: row.balance
            for row in active.execute(
                select(MonthEndSnapshot.account_id, MonthEndSnapshot.balance).where(
                    MonthEndSnapshot.month == month
                )
            )
        }
        conflicts = [
            f"account {account_id}: stored {existing[account_id]:.2f}, latest {balance:.2f}"
            for account_id, balance in latest.items()
            if account_id in existing and existing[account_id] != balance
        ]
        if conflicts:
            raise MonthEndConflictError(f"Month {month} conflicts: " + "; ".join(conflicts))
        if existing:
            return 0
        missing = [
            MonthEndSnapshot(account_id=account_id, month=month, balance=balance)
            for account_id, balance in latest.items()
            if not (
                skip_incomplete_files
                and latest_rows[account_id].source in {"csv", "ofx"}
                and latest_rows[account_id].date < last
            )
        ]
        active.add_all(missing)
        active.flush()
        return len(missing)


def closed_month_balances(*, account_ids=None, session=None) -> dict[str, dict[int, Decimal]]:
    """Read closed and file-covered months with daily carry-forward for missing rows.

    Existing rows always win. Any account's row, including a hidden account's,
    establishes a closed month. A CSV/OFX balance also establishes its month,
    including for hidden accounts, unless reopened with no remaining rows.
    Callers limit months by date. By default only visible accounts contribute;
    explicit IDs select exactly those accounts, including hidden ones. Writes nothing.
    """
    with nullcontext(session) if session is not None else session_scope() as active:
        found = active.execute(
            union(
                select(MonthEndSnapshot.month.label("month"), literal(True).label("stored")),
                select(
                    func.strftime("%Y-%m", BalanceSnapshot.date).label("month"),
                    literal(False).label("stored"),
                ).where(BalanceSnapshot.source.in_(("csv", "ofx"))),
            )
        ).all()
        months = {row.month for row in found if row.stored}
        file_months = {row.month for row in found if not row.stored} - months
        # Databases from before reopening have no file balances, so skip that table.
        if file_months:
            file_months -= set(active.scalars(select(MonthEndReopen.month).distinct()))
        months = sorted(months | file_months)
        if not months:
            return {}
        selected = select(Account.id).where(
            Account.hidden.is_(False) if account_ids is None else Account.id.in_(account_ids)
        )
        closed_query = select(
            MonthEndSnapshot.account_id, MonthEndSnapshot.month, MonthEndSnapshot.balance
        ).where(MonthEndSnapshot.account_id.in_(selected))
        daily_query = (
            select(BalanceSnapshot.account_id, BalanceSnapshot.date, BalanceSnapshot.balance)
            .where(BalanceSnapshot.account_id.in_(selected))
            .order_by(BalanceSnapshot.date)
        )
        result = {month: {} for month in months}
        for row in active.execute(closed_query):
            result[row.month][row.account_id] = row.balance
        histories = {}
        for row in active.execute(daily_query):
            days, balances = histories.setdefault(row.account_id, ([], []))
            days.append(row.date)
            balances.append(row.balance)
        for month in months:
            _, last = _month_end(month)
            for aid, (days, balances) in histories.items():
                if aid in result[month]:
                    continue
                index = bisect_right(days, last) - 1
                if index >= 0:
                    result[month][aid] = balances[index]
        return result


def locked_days(account_id, *, session=None) -> dict[dt.date, str]:
    """Map each daily row used by a close to the latest month relying on it."""
    with nullcontext(session) if session is not None else session_scope() as active:
        months = active.scalars(
            select(MonthEndSnapshot.month)
            .where(MonthEndSnapshot.account_id == account_id)
            .order_by(MonthEndSnapshot.month)
        ).all()
        days = active.scalars(
            select(BalanceSnapshot.date)
            .where(BalanceSnapshot.account_id == account_id)
            .order_by(BalanceSnapshot.date)
        ).all()
        locked = {}
        for month in months:
            _, last = _month_end(month)
            index = bisect_right(days, last) - 1
            if index >= 0:
                locked[days[index]] = month
        return locked


def edit_snapshot(account_id, date, balance, *, session=None) -> bool:
    balance = _money(balance)
    with nullcontext(session) if session is not None else session_scope() as active:
        _lock_out_writers(active)
        row = active.get(BalanceSnapshot, (account_id, date))
        if row is None:
            raise SnapshotNotFoundError("No balance recorded on this day")
        if date in locked_days(account_id, session=active):
            raise MonthEndLockedError("Reopen the month before editing its month-end balance")
        if row.balance == balance:
            return False
        active.add(
            BalanceEdit(
                account_id=account_id,
                date=date,
                old_balance=row.balance,
                new_balance=balance,
                old_source=row.source,
            )
        )
        active.execute(
            update(BalanceSnapshot)
            .where(BalanceSnapshot.account_id == account_id, BalanceSnapshot.date == date)
            .values(balance=balance, source="manual")
        )
        active.flush()
        return True


def reopen_month(month, reason=None, *, session=None) -> int:
    month, _ = _month_end(month)
    with nullcontext(session) if session is not None else session_scope() as active:
        _lock_out_writers(active)
        rows = active.scalars(select(MonthEndSnapshot).where(MonthEndSnapshot.month == month)).all()
        if not rows:
            raise MonthNotClosedError("Month is not closed")
        reopen = MonthEndReopen(month=month, reason=reason, deleting=True)
        active.add(reopen)
        active.flush()
        active.add_all(
            MonthEndArchive(reopen_id=reopen.id, account_id=row.account_id, balance=row.balance)
            for row in rows
        )
        active.flush()
        active.execute(delete(MonthEndSnapshot).where(MonthEndSnapshot.month == month))
        reopen.deleting = False
        active.flush()
        return len(rows)


def _point(date, balances, accounts):
    assets = Decimal("0.00")
    liabilities = Decimal("0.00")
    for account_id, balance in balances.items():
        if accounts[account_id].is_liability:
            liabilities += balance
        else:
            assets += balance
    return NetWorthPoint(date, assets, liabilities, assets - liabilities)


def _check_currency(account_ids, accounts):
    currencies = {accounts[account_id].currency for account_id in account_ids}
    if len(currencies) > 1:
        raise MixedCurrencyError("Cannot combine currencies: " + ", ".join(sorted(currencies)))


def net_worth_series(start, end, step="day", *, session=None) -> list[NetWorthPoint]:
    if start > end:
        raise ValueError("Start must be on or before end")
    if step not in ("day", "month"):
        raise ValueError(f"Unknown step: {step!r}")
    with nullcontext(session) if session is not None else session_scope() as active:
        accounts = {
            account.id: account
            for account in active.execute(
                select(Account.id, Account.is_liability, Account.currency).where(
                    Account.hidden.is_(False)
                )
            )
        }
        if step == "month":
            months = {}
            contributing = set()
            for month, balances in closed_month_balances(session=active).items():
                _, last = _month_end(month)
                if balances and start <= last <= end:
                    months[last] = balances
                    contributing.update(balances)
            _check_currency(contributing, accounts)
            points = [_point(last, balances, accounts) for last, balances in sorted(months.items())]
        else:
            rows = list(
                active.execute(
                    select(
                        BalanceSnapshot.account_id, BalanceSnapshot.date, BalanceSnapshot.balance
                    )
                    .join(Account)
                    .where(Account.hidden.is_(False), BalanceSnapshot.date <= end)
                    .order_by(BalanceSnapshot.date)
                )
            )
            _check_currency({row.account_id for row in rows}, accounts)
            points = []
            balances = {}
            index = 0
            day = start
            while day <= end:
                while index < len(rows) and rows[index].date <= day:
                    row = rows[index]
                    balances[row.account_id] = row.balance
                    index += 1
                if balances:
                    points.append(_point(day, balances, accounts))
                if day == end:
                    break
                day += dt.timedelta(days=1)
        active.flush()
        return points
