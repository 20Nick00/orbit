"""Dashboard read models, plus monthly cash flow and net worth rollups.

Dashboard reads use the cache rebuilt here. Monthly rollups use permanent closes
and daily carry-forward for accounts without a row in a closed month.
"""

import calendar
import datetime as dt
import re
from bisect import bisect_left
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby

from sqlalchemy import delete, func, inspect, select
from sqlalchemy.exc import OperationalError

from core.db import session_scope
from core.equity import equity_summary
from core.models import (
    Account,
    AccountFact,
    AlmanacNote,
    BalanceSnapshot,
    Institution,
    MonthEndSnapshot,
    Paystub,
    PaystubLine,
    RollupCache,
    Transaction,
)
from core.snapshots import (
    MixedCurrencyError,
    _check_currency,
    _lock_out_writers,
    _point,
    closed_month_balances,
    net_worth_series,
)
from core.snapshots import (
    _month_end as _month_bounds,
)

RANGES = ("1M", "3M", "6M", "1Y", "All")
TYPES = {
    "cash": "Cash",
    "invest": "Investments",
    "retire": "Retirement",
    "health": "Health",
    "insure": "Insurance",
    "crypto": "Crypto & markets",
    "property": "Property",
    "cards": "Credit cards",
    "loans": "Loans",
    "mortgage": "Mortgage",
    "other": "Other",
}


def cents(value):
    return None if value is None else int(value * 100)


def delta(now, base):
    if now is None or base is None:
        return None
    difference = now - base
    # A percentage of zero is undefined, so a change from zero has none.
    proportion = difference / abs(base) if base else None if difference else 0
    flat = proportion is not None and abs(proportion) < 0.0005
    return {
        "d": difference,
        "p": proportion,
        "cls": "flat" if flat else "pos" if difference > 0 else "neg",
    }


def _sum(values):
    values = list(values)
    return None if any(value is None for value in values) else sum(values)


def _gross(values):
    values = list(values)
    return None if any(value is None for value in values) else sum(abs(v) for v in values)


def _share(value, scale):
    """Share of a side's gross size, so an overdrawn or overpaid account can't push past 100%."""
    if value is None or not scale:
        return None
    return abs(value) / scale


def _month_end(day):
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _chart(as_of, range_key, daily, closed):
    monthly = range_key in ("1Y", "All")
    dates = []
    if daily:
        first = min(daily)
        if range_key == "All":
            start = first
        elif range_key == "1Y":
            start = as_of - timedelta(days=365)
            if start != _month_end(start):
                start = start.replace(day=1) - timedelta(days=1)
        else:
            start = as_of - timedelta(days={"1M": 30, "3M": 90, "6M": 180}[range_key])
        start = max(start, first)
        if monthly:
            # Preserve the first available date when history clips the range.
            dates = [start]
            end = _month_end(start)
            while end <= as_of:
                dates.append(end)
                end = _month_end(end + timedelta(days=1))
            dates.append(as_of)
        elif range_key == "6M":
            dates = [start, as_of]
            day = as_of - timedelta(days=7)
            while day > start:
                dates.append(day)
                day -= timedelta(days=7)
        else:
            dates = [start + timedelta(days=n) for n in range((as_of - start).days + 1)]
    dates = sorted(set(dates))
    return {
        "range": range_key,
        "monthly": monthly,
        "dates": [day.isoformat() for day in dates],
        "series": [
            int(
                (closed.get(day, daily[day]) if monthly else daily[day]).net_worth.quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            for day in dates
        ],
    }


def _dashboard(accounts, institutions, facts, snapshots, daily, as_of):
    latest, bases = {}, {}
    base_day = as_of - timedelta(days=30)
    for row in snapshots:
        latest[row.account_id] = row
        if row.date <= base_day:
            bases[row.account_id] = row
    balances = {
        account.id: (
            cents(latest[account.id].balance) * (-1 if account.is_liability else 1)
            if account.id in latest
            else None
        )
        for account in accounts
    }
    assets = _sum(balances[a.id] for a in accounts if not a.is_liability)
    liabilities = _sum(balances[a.id] for a in accounts if a.is_liability)
    net_worth = _sum([assets, liabilities])
    base_dates = {
        "Today": as_of - timedelta(days=1),
        "1M": base_day,
        "YTD": date(as_of.year - 1, 12, 31),
        "1Y": as_of - timedelta(days=365),
    }
    deltas = {
        label: delta(net_worth, cents(daily[day].net_worth) if day in daily else None)
        for label, day in base_dates.items()
    }
    sides = []
    for liability in (False, True):
        scale = _gross(balances[a.id] for a in accounts if a.is_liability == liability)
        groups = []
        for kind, name in TYPES.items():
            members = [a for a in accounts if a.type == kind and a.is_liability == liability]
            if not members:
                continue
            group_total = _sum(balances[a.id] for a in members)
            rows = []
            for account in members:
                institution = institutions.get(account.institution_id)
                subtitle = facts.get(account.id, {}).get("subtitle")
                if subtitle is None:
                    subtitle = account.subtype or ""
                    if account.mask:
                        subtitle = f"{subtitle} ••{account.mask}".strip()
                base = bases.get(account.id)
                last = latest.get(account.id)
                share = _share(balances[account.id], scale)
                rows.append(
                    {
                        "id": account.id,
                        "name": account.name,
                        "institution": institution.name if institution else None,
                        "subtitle": subtitle,
                        "balance": balances[account.id],
                        "bar_pct": None if share is None else 100 * share,
                        "delta": delta(
                            balances[account.id],
                            cents(base.balance) * (-1 if liability else 1) if base else None,
                        ),
                        "last_snapshot": last.date.isoformat() if last else None,
                    }
                )
            groups.append(
                {
                    "type": kind,
                    "name": name,
                    "total": group_total,
                    "share": _share(_gross(balances[a.id] for a in members), scale),
                    "accounts": rows,
                }
            )
        sides.append(groups)
    equity = equity_summary(
        {a.id: a for a in accounts},
        {aid: row.balance for aid, row in latest.items()},
        {aid: values.get("linked_liability") for aid, values in facts.items()},
    )
    equity_notes = []
    if count := equity["unlinked_count"]:
        equity_notes.append(f"{count} home{'s' if count != 1 else ''} not linked")
    if count := equity["problem_count"]:
        equity_notes.append(
            f"{count} home{'s' if count != 1 else ''} need{'s' if count == 1 else ''} attention"
        )
    liquid = _sum(
        balances[a.id] for a in accounts if a.type in ("cash", "invest", "crypto", "cards")
    )
    return {
        "net_worth": net_worth,
        "assets": assets,
        "liabilities": liabilities,
        "deltas": deltas,
        "assets_groups": sides[0],
        "liabilities_groups": sides[1],
        "facts": {
            "Assets": assets,
            "Liabilities": liabilities,
            "Liquid net worth": liquid,
            "Debt to assets": (
                -liabilities / assets if assets and assets > 0 and liabilities is not None else None
            ),
            "Home equity": cents(equity["total"]) if equity["total"] is not None else None,
            "1-yr change": deltas["1Y"]["d"] if deltas["1Y"] else None,
        },
        "home_equity_note": "; ".join(equity_notes) or None,
        "is_demo": any(
            institutions[a.institution_id].aggregator == "demo"
            for a in accounts
            if a.institution_id in institutions
        ),
        "account_count": len(accounts),
        "last_updated": max((row.date for row in latest.values()), default=None).isoformat()
        if latest
        else None,
    }


def _compute(session, as_of):
    accounts = session.scalars(
        select(Account).where(Account.hidden.is_(False)).order_by(Account.id)
    ).all()
    snapshots = session.execute(
        select(BalanceSnapshot.account_id, BalanceSnapshot.date, BalanceSnapshot.balance)
        .join(Account)
        .where(Account.hidden.is_(False), BalanceSnapshot.date <= as_of)
        .order_by(BalanceSnapshot.date)
    ).all()
    start = snapshots[0].date if snapshots else as_of
    daily = {point.date: point for point in net_worth_series(start, as_of, "day", session=session)}
    closed = {
        point.date: point for point in net_worth_series(start, as_of, "month", session=session)
    }
    institutions = {row.id: row for row in session.scalars(select(Institution))}
    facts = {}
    for row in session.scalars(select(AccountFact).join(Account).where(Account.hidden.is_(False))):
        facts.setdefault(row.account_id, {})[row.key] = row.value
    dashboard = (
        _dashboard(accounts, institutions, facts, snapshots, daily, as_of) if accounts else None
    )
    payloads = {"dashboard": dashboard}
    payloads.update({f"chart:{key}": _chart(as_of, key, daily, closed) for key in RANGES})
    return payloads


def rebuild(session, as_of):
    """Replace all cached ranges within the caller's transaction, and return them."""
    session.flush()
    _lock_out_writers(session)
    payloads = _compute(session, as_of)
    session.execute(delete(RollupCache))
    session.add_all(
        RollupCache(key=key, as_of=as_of, payload=payload) for key, payload in payloads.items()
    )
    session.flush()
    return payloads


class SchemaOutOfDateError(RuntimeError):
    pass


def _read(session, today, key):
    # Select columns rather than ORM identities: triggers can delete rows behind
    # the identity map, even when record_daily and this read share one session.
    statement = select(RollupCache.as_of, RollupCache.payload).where(RollupCache.key == key)
    try:
        row = session.execute(statement).first()
        if row is not None and row.as_of == today:
            return row.payload
        _lock_out_writers(session)
        # Another request may have finished the same rebuild while this one waited.
        row = session.execute(statement).first()
        if row is not None and row.as_of == today:
            session.commit()
            return row.payload
        # Serve from memory: a writer may empty the cache as soon as this commits.
        payload = rebuild(session, today)[key]
        session.commit()
        return payload
    except MixedCurrencyError:
        session.rollback()
        raise
    except OperationalError as exc:
        session.rollback()
        message = str(exc.orig)
        if "database is locked" in message:
            # A long sync holds the write lock: show true figures, just don't store them.
            return _compute(session, today)[key]
        if any(f"no such table: {name}" in message for name in ("rollup_cache", "accounts")):
            if inspect(session.connection()).has_table("accounts"):
                raise SchemaOutOfDateError("rollup_cache table is missing") from exc
            return None
        raise


def read_dashboard(session, today, range_key="1Y"):
    if range_key not in RANGES:
        raise ValueError(f"Unknown range: {range_key}")
    payload = _read(session, today, "dashboard")
    if payload is None:
        return None
    return {**payload, "chart": _read(session, today, f"chart:{range_key}")}


def read_chart(session, today, range_key):
    if range_key not in RANGES:
        raise ValueError(f"Unknown range: {range_key}")
    return _read(session, today, f"chart:{range_key}")


ZERO = Decimal("0")
CASH_FLOW_TYPES = frozenset({"cash", "cards", "loans", "mortgage", "other"})
TRANSFER_CATEGORIES = re.compile(
    r"^(?:transfer.*|credit card payment|loan_payments_credit_card_payment)$"
)
INCOME_CATEGORIES = frozenset(
    {
        "income",
        "paycheck",
        "salary",
        "wages",
        "bonus",
        "interest",
        "dividend",
        "dividends",
    }
)
CONTRIBUTION_LABEL = re.compile(
    r"401|403|457|roth|\bhsa\b|retire|pension|match|espp|\bira\b|tsp|profit.?shar", re.I
)
MILESTONES = tuple(
    Decimal(n)
    for n in (
        100000,
        250000,
        500000,
        750000,
        1000000,
        1500000,
        2000000,
        3000000,
        5000000,
        10000000,
    )
)


@dataclass(frozen=True)
class MonthRollup:
    month: str
    net_worth_end: Decimal | None
    net_worth_change: Decimal | None
    income: Decimal
    spending: Decimal
    kept: Decimal
    savings_rate: Decimal | None
    contributions: Decimal
    new_accounts: Decimal
    market_gain: Decimal | None
    closed: bool


@dataclass(frozen=True)
class YearRollup:
    year: int
    net_worth_end: Decimal | None
    net_worth_change: Decimal | None
    income: Decimal
    spending: Decimal
    kept: Decimal
    savings_rate: Decimal | None
    contributions: Decimal
    new_accounts: Decimal
    market_gain: Decimal | None
    ytd: bool
    note: str


def _next_month(first):
    return (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def _months(start, end):
    first = dt.date.fromisoformat(_month_bounds(start)[0] + "-01")
    last = dt.date.fromisoformat(_month_bounds(end)[0] + "-01")
    while first <= last:
        yield first.strftime("%Y-%m")
        first = _next_month(first)


def _previous(month):
    return (dt.date.fromisoformat(month + "-01") - dt.timedelta(days=1)).strftime("%Y-%m")


def _category(value):
    return (value or "").strip().casefold()


def _income(category):
    return category in INCOME_CATEGORIES or category.startswith("income")


INTEREST_CHARGES = frozenset({"interest", "dividend", "dividends"})


def _income_transaction(category, amount):
    return (_income(category) and not (category in INTEREST_CHARGES and amount < 0)) or (
        not category and amount > 0
    )


@dataclass
class _ReportData:
    accounts: dict
    daily: list
    ends: dict
    transactions: list
    flows: dict
    contributions: dict
    history: dict
    data_months: set
    closed_months: set
    earliest: dt.date | None
    notes: dict
    months: dict = field(default_factory=dict)
    years: list | None = None


def _data(active, today):
    accounts = {a.id: a for a in active.scalars(select(Account).where(Account.hidden.is_(False)))}
    daily = list(
        active.execute(
            select(BalanceSnapshot.account_id, BalanceSnapshot.date, BalanceSnapshot.balance)
            .join(Account)
            .where(Account.hidden.is_(False), BalanceSnapshot.date <= today)
            .order_by(BalanceSnapshot.date)
        )
    )
    by_month = {
        month: balances
        for month, balances in closed_month_balances(session=active).items()
        if balances and month <= today.strftime("%Y-%m")
    }
    transactions = list(
        active.execute(
            select(
                Transaction.account_id, Transaction.date, Transaction.amount, Transaction.category
            )
            .join(Account)
            .where(
                Account.hidden.is_(False),
                Transaction.pending.is_(False),
                Transaction.date <= today,
            )
        )
    )
    flow_transactions = [
        tx for tx in transactions if accounts[tx.account_id].type in CASH_FLOW_TYPES
    ]
    _check_currency(
        {r.account_id for r in daily + flow_transactions}
        | {account_id for balances in by_month.values() for account_id in balances},
        accounts,
    )
    pay = list(
        active.execute(
            select(Paystub.pay_date, PaystubLine.label, PaystubLine.amount, PaystubLine.group_)
            .outerjoin(PaystubLine)
            .where(Paystub.pay_date <= today)
        )
    )
    live = {}
    history = {}
    data_months = set(by_month)
    for row in daily:
        live[row.account_id] = row.balance
        history.setdefault(row.account_id, []).append(row)
        data_months.add(row.date.isoformat()[:7])
    closed_months = set(by_month)
    by_month[today.strftime("%Y-%m")] = live
    flows = {}
    for tx in transactions:
        month = tx.date.isoformat()[:7]
        data_months.add(month)
        if accounts[tx.account_id].type not in CASH_FLOW_TYPES:
            continue
        category = _category(tx.category)
        if TRANSFER_CATEGORIES.fullmatch(category):
            continue
        flow = flows.setdefault(month, [ZERO, ZERO])
        if _income_transaction(category, tx.amount):
            flow[0] += tx.amount
        else:
            flow[1] -= tx.amount
    contributions = {}
    # Bank-to-brokerage transfers are already in kept, not contributions.
    # Counting them again would create a false market loss.
    for row in pay:
        month = row.pay_date.isoformat()[:7]
        data_months.add(month)
        if (
            row.amount is not None
            and row.group_ in {"pretax", "posttax", "employer"}
            and CONTRIBUTION_LABEL.search(row.label)
        ):
            contributions[month] = contributions.get(month, ZERO) + abs(row.amount)
    return _ReportData(
        accounts,
        daily,
        by_month,
        flow_transactions,
        flows,
        contributions,
        history,
        data_months,
        closed_months,
        _earliest(active, today),
        {n.year: n.note for n in active.execute(select(AlmanacNote.year, AlmanacNote.note))},
    )


def _earliest(active, today):
    dates = []
    for model, column in (
        (BalanceSnapshot, BalanceSnapshot.date),
        (MonthEndSnapshot, MonthEndSnapshot.month),
        (Transaction, Transaction.date),
    ):
        bound = today.strftime("%Y-%m") if model is MonthEndSnapshot else today
        query = (
            select(func.min(column))
            .select_from(model)
            .join(Account)
            .where(Account.hidden.is_(False), column <= bound)
        )
        if model is Transaction:
            query = query.where(Transaction.pending.is_(False))
        value = active.scalar(query)
        if value is not None:
            day = dt.date.fromisoformat(value + "-01") if isinstance(value, str) else value
            if day <= today:
                dates.append(day)
    first_pay = active.scalar(select(func.min(Paystub.pay_date)).where(Paystub.pay_date <= today))
    if first_pay is not None:
        dates.append(first_pay)
    return min(dates) if dates else None


def _opening(month, current, previous, history, accounts):
    if not previous:
        return ZERO
    first = dt.date.fromisoformat(month + "-01")
    last = _month_bounds(month)[1]
    total = ZERO
    for account_id in current.keys() - previous.keys():
        # _data builds each account's sorted history once. Binary search gives
        # the last balance before this month, or its first balance inside it.
        rows = history.get(account_id, [])
        index = bisect_left(rows, first, key=lambda row: row.date)
        opening = (
            rows[index - 1].balance
            if index
            else rows[0].balance
            if rows and rows[0].date <= last
            else ZERO
        )
        total += -opening if accounts[account_id].is_liability else opening
    return total


def monthly_rollups(start_month, end_month, *, today, session=None, _loaded=None):
    with nullcontext(session) if session is not None else session_scope() as active:
        data = _loaded if _loaded is not None else _data(active, today)
        accounts, ends = data.accounts, data.ends
        rows = []
        for month in _months(start_month, end_month):
            if month > today.strftime("%Y-%m"):
                break
            if month in data.months:
                rows.append(data.months[month])
                continue
            balances = ends.get(month, {})
            previous = ends.get(_previous(month), {})
            last = _month_bounds(month)[1]
            end = _point(last, balances, accounts).net_worth if balances else None
            prev = _point(last, previous, accounts).net_worth if previous else None
            change = end - prev if end is not None and prev is not None else None
            income, spending = data.flows.get(month, (ZERO, ZERO))
            contributions = data.contributions.get(month, ZERO)
            kept = income - spending
            opening = _opening(month, balances, previous, data.history, accounts)
            rows.append(
                MonthRollup(
                    month,
                    end,
                    change,
                    income,
                    spending,
                    kept,
                    kept / income if income > 0 else None,
                    contributions,
                    opening,
                    change - opening - contributions - kept if change is not None else None,
                    month in data.closed_months and month < today.strftime("%Y-%m"),
                )
            )
            data.months[month] = rows[-1]
        return rows


def yearly_rollups(*, today, session=None, _loaded=None):
    with nullcontext(session) if session is not None else session_scope() as active:
        data = _loaded if _loaded is not None else _data(active, today)
        if data.years is not None:
            return data.years
        earliest = data.earliest
        if earliest is None:
            return []
        months = monthly_rollups(
            f"{earliest.year - 1}-12",
            today.strftime("%Y-%m"),
            today=today,
            session=active,
            _loaded=data,
        )
        by_month = {m.month: m for m in months}
        notes = data.notes
        rows = []
        for year in range(earliest.year, today.year + 1):
            group = [m for m in months if m.month.startswith(f"{year}-")]
            end = group[-1].net_worth_end
            previous = by_month[f"{year - 1}-12"].net_worth_end
            change = end - previous if end is not None and previous is not None else None
            income, spending, contributions, opening = (
                sum((getattr(m, attr) for m in group), ZERO)
                for attr in ("income", "spending", "contributions", "new_accounts")
            )
            kept = income - spending
            rows.append(
                YearRollup(
                    year,
                    end,
                    change,
                    income,
                    spending,
                    kept,
                    kept / income if income > 0 else None,
                    contributions,
                    opening,
                    change - opening - contributions - kept if change is not None else None,
                    year == today.year,
                    notes.get(year, ""),
                )
            )
        data.years = rows
        return rows


def category_spend(days=90, *, today, session=None, _loaded=None):
    if days <= 0:
        raise ValueError("Days must be positive.")
    first = today - dt.timedelta(days=days - 1)
    with nullcontext(session) if session is not None else session_scope() as active:
        data = _loaded if _loaded is not None else _data(active, today)
        transactions = data.transactions
        amounts = {}
        labels = {}
        for tx in transactions:
            category = _category(tx.category)
            if (
                tx.date < first
                or TRANSFER_CATEGORIES.fullmatch(category)
                or _income_transaction(category, tx.amount)
            ):
                continue
            labels.setdefault(category, (tx.category or "").strip() or "Uncategorized")
            amounts[category] = amounts.get(category, ZERO) - tx.amount
        return [
            dict(category=labels[k], spending=v)
            for k, v in sorted(amounts.items(), key=lambda item: (-item[1], item[0]))
            if v > 0
        ]


def investments_vs_cash(start_month, end_month, *, today, session=None, _loaded=None):
    with nullcontext(session) if session is not None else session_scope() as active:
        data = _loaded if _loaded is not None else _data(active, today)
        accounts, ends = data.accounts, data.ends
        rows = []
        for month in _months(start_month, end_month):
            if month > today.strftime("%Y-%m"):
                break
            balances = ends.get(month, {})
            values = {}
            for name, types in (
                ("investments", {"invest", "retire", "health", "crypto"}),
                ("cash", {"cash"}),
            ):
                values[name] = (
                    sum(
                        (value for aid, value in balances.items() if accounts[aid].type in types),
                        ZERO,
                    )
                    if balances
                    else None
                )
            rows.append(
                dict(
                    month=month,
                    closed=month in data.closed_months and month < today.strftime("%Y-%m"),
                    **values,
                )
            )
        return rows


def almanac_records(*, today, session=None, _loaded=None):
    with nullcontext(session) if session is not None else session_scope() as active:
        data = _loaded if _loaded is not None else _data(active, today)
        earliest = data.earliest
        years = yearly_rollups(today=today, session=active, _loaded=data)
        # Only dates with changed balances can set a record or cross a threshold.
        # Group same-day updates before evaluating, just as the daily series does.
        daily = []
        balances = {}
        for day, updates in groupby(data.daily, key=lambda row: row.date):
            balances.update((row.account_id, row.balance) for row in updates)
            daily.append(_point(day, balances, data.accounts))
        months = (
            monthly_rollups(earliest, today, today=today, session=active, _loaded=data)
            if earliest
            else []
        )
        high = max(daily, key=lambda p: p.net_worth) if daily else None
        eligible = [m for m in months if m.closed and m.net_worth_change is not None]
        best = (
            max(eligible, key=lambda m: m.net_worth_change - m.new_accounts) if eligible else None
        )
        crossed = {}
        for before, after in zip(daily, daily[1:], strict=False):
            for threshold in MILESTONES:
                if threshold not in crossed and before.net_worth < threshold <= after.net_worth:
                    crossed[threshold] = after.date
        rates = [y.savings_rate for y in years if y.savings_rate is not None]
        return dict(
            all_time_high=high.net_worth if high else None,
            all_time_high_date=high.date if high else None,
            best_month=best.month if best else None,
            best_month_change=best.net_worth_change - best.new_accounts if best else None,
            milestones=[dict(threshold=t, date=d) for t, d in sorted(crossed.items())],
            years_tracked=len(years) if years else None,
            average_savings_rate=sum(rates, ZERO) / len(rates) if rates else None,
        )
