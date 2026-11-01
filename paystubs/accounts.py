"""Account-backed inputs for the paycheck waterfall."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from core.models import Account, BalanceSnapshot, Transaction
from core.snapshots import MixedCurrencyError, net_worth_series


@dataclass(frozen=True)
class NetWorthChange:
    value: Decimal | None
    reason: str | None
    unbaselined_accounts: int = 0


@dataclass(frozen=True)
class Spending:
    value: Decimal | None
    reason: str | None
    late_accounts: int = 0


def spending(session, start, end):
    rows = list(
        session.scalars(
            select(Transaction)
            .join(Account)
            .where(
                Account.hidden.is_(False),
                Transaction.date.between(start, end),
                Transaction.pending.is_(False),
            )
        )
    )
    if not rows:
        return Spending(None, "needs transactions from Jan 1 through the latest pay date.")
    firsts = {}
    for row in rows:
        firsts[row.account_id] = min(firsts.get(row.account_id, row.date), row.date)
    january_end = date(start.year, 1, 31)
    late_accounts = sum(first > january_end for first in firsts.values())
    earliest, latest = min(row.date for row in rows), max(row.date for row in rows)
    if earliest > january_end or latest < end - timedelta(days=31):

        def when(day):
            return f"{day:%b} {day.day}, {day.year}"

        return Spending(
            None,
            f"Transactions only cover {when(earliest)} to {when(latest)}, "
            "so Spending would be understated.",
            late_accounts,
        )
    value = -sum(
        (
            row.amount
            for row in rows
            if not row.pending and (row.category or "").casefold() not in {"transfer", "income"}
        ),
        Decimal("0"),
    )
    return Spending(value, None, late_accounts)


def uncategorized_count(session, start, end):
    """File imports may leave category empty, so transfers and income can distort spending."""
    return session.scalar(
        select(func.count(Transaction.id))
        .join(Account)
        .where(
            Account.hidden.is_(False),
            Transaction.date.between(start, end),
            Transaction.pending.is_(False),
            func.coalesce(Transaction.category, "") == "",
        )
    )


def net_worth_change(session, start_baseline, end):
    firsts = list(
        session.execute(
            select(Account.id, func.min(BalanceSnapshot.date))
            .join(BalanceSnapshot)
            .where(Account.hidden.is_(False), BalanceSnapshot.date <= end)
            .group_by(Account.id)
        )
    )
    unbaselined = sum(first > start_baseline for _, first in firsts)
    if not any(first <= start_baseline for _, first in firsts):
        when = f"{start_baseline:%b} {start_baseline.day}, {start_baseline.year}"
        return NetWorthChange(None, f"Needs a balance from on or before {when}", unbaselined)
    try:
        # The daily series reads ALL snapshots <= end, carrying older balances forward.
        baseline = net_worth_series(start_baseline, start_baseline, session=session)
        ending = net_worth_series(end, end, session=session)
    except MixedCurrencyError:
        return NetWorthChange(None, "Net worth uses more than one currency", unbaselined)
    if not ending:
        return NetWorthChange(None, "Needs a balance on or before the latest pay date", unbaselined)
    return NetWorthChange(ending[0].net_worth - baseline[0].net_worth, None, unbaselined)
