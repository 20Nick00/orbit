"""Account and type views of recorded balances and their audit history."""

import re
from datetime import UTC, date, timedelta
from types import SimpleNamespace

from sqlalchemy import func, select

from core.models import (
    Account,
    AccountFact,
    BalanceEdit,
    BalanceSnapshot,
    Holding,
    Institution,
    MonthEndArchive,
    MonthEndReopen,
    MonthEndSnapshot,
    Transaction,
)
from core.rollups import TYPES, SchemaOutOfDateError, _chart, cents, delta, read_dashboard
from core.snapshots import (
    MixedCurrencyError,
    _check_currency,
    _month_end,
    closed_month_balances,
    locked_days,
)

DETAIL_RANGES = ("1M", "3M", "1Y", "All")
BLURBS = {
    "cash": "Checking and savings. Liquid, spendable tomorrow.",
    "invest": "Taxable brokerage. Liquid but taxed when sold.",
    "retire": "401(k) and IRA. Locked until 59½ without penalty.",
    "health": "HSA. Triple tax-advantaged; invested, not spent.",
    "insure": "Cash value of permanent life policies. Term policies have no balance.",
    "crypto": "Exchange balances, wallets and prediction-market stake.",
    "property": "Home at estimated market value. Equity = value − mortgage.",
    "cards": "Statement balances. Paid in full each cycle.",
    "loans": "Personal and student debt, fixed rate.",
    "mortgage": "Secured by the condo. Principal only; escrow excluded.",
}
TERM_KEYS = {
    "rate",
    "payment",
    "payoff",
    "term",
    "original amount",
    "principal ytd",
    "interest ytd",
    "statement balance",
    "due",
    "credit limit",
    "utilization",
    "minimum payment",
}
HOLDING_TYPES = {"invest", "retire", "health", "crypto"}


def _series(session, members, today):
    accounts = {account.id: account for account in members}
    _check_currency(accounts, accounts)
    snapshots = session.scalars(
        select(BalanceSnapshot)
        .where(BalanceSnapshot.account_id.in_(accounts), BalanceSnapshot.date <= today)
        .order_by(BalanceSnapshot.date)
    ).all()
    owed = bool(members) and all(a.is_liability for a in members)
    daily, closed, balances = {}, {}, {}
    index = 0
    day = snapshots[0].date if snapshots else today + timedelta(days=1)
    while day <= today:
        while index < len(snapshots) and snapshots[index].date <= day:
            row = snapshots[index]
            balances[row.account_id] = row.balance
            index += 1
        total = sum(
            (-value if accounts[key].is_liability and not owed else value)
            for key, value in balances.items()
        )
        daily[day] = SimpleNamespace(net_worth=total)
        day += timedelta(days=1)
    for month, values in closed_month_balances(account_ids=accounts, session=session).items():
        _, last = _month_end(month)
        if last <= today and values:
            total = sum(
                (-value if accounts[key].is_liability and not owed else value)
                for key, value in values.items()
            )
            closed[last] = SimpleNamespace(net_worth=total)
    return daily, closed, balances, owed


def _metrics(session, members, today, range_key):
    daily, closed, balances, owed = _series(session, members, today)
    total = None
    if len(balances) == len(members):
        total = sum(
            cents(balances[a.id]) * (-1 if a.is_liability and not owed else 1) for a in members
        )
    sign = -1 if owed else 1
    bases = {
        "1M": today - timedelta(days=30),
        "3M": today - timedelta(days=90),
        "YTD": date(today.year - 1, 12, 31),
        "1Y": today - timedelta(days=365),
    }
    deltas = {
        label: delta(
            total * sign if total is not None else None,
            cents(daily[day].net_worth) * sign if day in daily else None,
        )
        for label, day in bases.items()
    }
    chart = {
        **_chart(
            today,
            range_key,
            daily if total is not None else {},
            closed if total is not None else {},
        ),
        "h": 190,
        "label": "Balance over time",
    }
    return {"balance": total, "deltas": deltas, "chart": chart, "is_liability": owed}


def _shares(dashboard, value, signed, liability):
    if dashboard is None or value is None:
        return None, None
    groups = dashboard["liabilities_groups" if liability else "assets_groups"]
    values = [row["balance"] for group in groups for row in group["accounts"]]
    gross = None if any(v is None for v in values) else sum(abs(v) for v in values)
    net = dashboard["net_worth"]
    return (abs(value) / gross if gross else None, signed / net if net else None)


def _facts(session, account):
    stored = dict(
        session.execute(
            select(AccountFact.key, AccountFact.value).where(AccountFact.account_id == account.id)
        ).all()
    )
    facts, terms = {}, {}
    for key, value in stored.items():
        if key.lower() in {"subtitle", "address", "linked_liability", "import_key"}:
            continue
        label = (
            key.replace("_", " ").capitalize()
            if "_" in key and re.fullmatch(r"[a-z_]+", key)
            else key
        )
        target = terms if account.is_liability and label.lower() in TERM_KEYS else facts
        target[label] = value
    subtitle = stored.get("subtitle")
    if subtitle is None:
        subtitle = account.subtype or ""
        if account.mask:
            subtitle = f"{subtitle} ••{account.mask}".strip()
    return facts, terms, subtitle


def _holdings(session, account_id):
    latest = session.scalar(select(func.max(Holding.date)).where(Holding.account_id == account_id))
    rows = session.scalars(
        select(Holding)
        .where(Holding.account_id == account_id, Holding.date == latest)
        .order_by(Holding.id)
    ).all()
    total = None if any(row.value is None for row in rows) else sum(row.value for row in rows)
    return [
        {
            "ticker": row.ticker,
            "name": row.name,
            "value": cents(row.value),
            "weight": row.value / total if row.value is not None and total else None,
        }
        for row in rows
    ]


def _history(session, account, today, month):
    daily = session.scalars(
        select(BalanceSnapshot)
        .where(BalanceSnapshot.account_id == account.id)
        .order_by(BalanceSnapshot.date)
    ).all()
    closed = {
        row.month: row
        for row in session.scalars(
            select(MonthEndSnapshot).where(MonthEndSnapshot.account_id == account.id)
        )
    }
    months = sorted({row.date.isoformat()[:7] for row in daily} | closed.keys())
    try:
        month, last = _month_end(month)
    except ValueError:
        month, last = _month_end(months[-1] if months else today)
    is_closed = (
        session.scalar(
            select(MonthEndSnapshot.account_id).where(MonthEndSnapshot.month == month).limit(1)
        )
        is not None
    )
    status = "in_progress" if last >= today else "closed" if is_closed else "open"
    locked = locked_days(account.id, session=session)
    own_month_end = (
        max((row.date for row in daily if row.date.isoformat()[:7] == month), default=None)
        if month in closed
        else None
    )
    rows, previous = [], None
    sign = -1 if account.is_liability else 1
    for row in daily:
        frozen = row.date == own_month_end
        balance = closed[month].balance if frozen else row.balance
        if row.date.isoformat()[:7] == month:
            rows.append(
                {
                    "date": row.date,
                    "balance_cents": cents(balance),
                    "input_value": f"{balance:.2f}",
                    "change": cents(row.balance - previous) * sign
                    if previous is not None
                    else None,
                    "source": row.source,
                    "is_month_end": frozen,
                    "editable": row.date not in locked,
                    "locked_by": locked.get(row.date) if not frozen else None,
                    "recorded_cents": cents(row.balance)
                    if frozen and row.balance != balance
                    else None,
                }
            )
        previous = row.balance
    if month in closed and own_month_end is None:
        rows.append(
            {
                "date": last,
                "balance_cents": cents(closed[month].balance),
                "input_value": f"{closed[month].balance:.2f}",
                "change": None,
                "source": "month-end",
                "is_month_end": True,
                "editable": False,
                "locked_by": None,
                "recorded_cents": None,
            }
        )
    reopens = session.execute(
        select(MonthEndArchive.balance, MonthEndReopen.reopened_at, MonthEndReopen.reason)
        .join(MonthEndReopen)
        .where(MonthEndArchive.account_id == account.id, MonthEndReopen.month == month)
        .order_by(MonthEndReopen.id.desc())
    ).all()
    return {
        "month": month,
        "months": months,
        "status": status,
        "own_row": month in closed,
        "prev_month": max((m for m in months if m < month), default=None),
        "next_month": min((m for m in months if m > month), default=None),
        "rows": list(reversed(rows)),
        "reopens": [
            {
                "balance": cents(r.balance),
                "reopened_at": r.reopened_at.replace(tzinfo=UTC).astimezone().date(),
                "reason": r.reason,
            }
            for r in reopens
        ],
    }


def read_account(session, account_id, today, *, month=None, range_key="3M"):
    account = session.get(Account, account_id)
    if account is None:
        return None
    metrics = _metrics(session, [account], today, range_key)
    side, net = None, None
    if not account.hidden:
        try:
            dashboard = read_dashboard(session, today)
            signed = metrics["balance"]
            if signed is not None and account.is_liability:
                signed = -signed
            side, net = _shares(dashboard, metrics["balance"], signed, account.is_liability)
        except (MixedCurrencyError, SchemaOutOfDateError):
            pass
    institution = (
        session.get(Institution, account.institution_id) if account.institution_id else None
    )
    facts, terms, subtitle = _facts(session, account)
    holdings = _holdings(session, account_id)
    activity = session.scalars(
        select(Transaction)
        .where(Transaction.account_id == account_id)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(15)
    )
    edits = session.scalars(
        select(BalanceEdit)
        .where(BalanceEdit.account_id == account_id)
        .order_by(BalanceEdit.edited_at.desc(), BalanceEdit.id.desc())
        .limit(10)
    )
    return {
        **metrics,
        "account": account,
        "name": account.name,
        "type_name": TYPES[account.type],
        "institution": institution.name if institution else None,
        "subtitle": subtitle,
        "eyebrow": "Balance owed" if account.is_liability else "Balance",
        "facts": facts,
        "terms": terms,
        "holdings": holdings,
        "show_holdings": bool(holdings) or account.type in HOLDING_TYPES,
        "terms_title": "Card terms" if account.type == "cards" else "Loan terms",
        "share_side": side,
        "share_net_worth": net,
        "is_demo": institution is not None and institution.aggregator == "demo",
        "can_edit": institution is None or institution.aggregator != "plaid",
        "activity": [
            {
                "date": r.date,
                "label": r.merchant or r.memo or "—",
                "amount": cents(r.amount),
                "category": r.category,
                "pending": r.pending,
            }
            for r in activity
        ],
        "history": _history(session, account, today, month),
        "edits": [
            {
                "date": r.date,
                "old_balance": cents(r.old_balance),
                "new_balance": cents(r.new_balance),
                "edited_at": r.edited_at.replace(tzinfo=UTC).astimezone().date(),
            }
            for r in edits
        ],
    }


def read_type(session, type_key, today, *, range_key="3M"):
    if type_key not in TYPES:
        return None
    members = session.scalars(
        select(Account)
        .where(Account.type == type_key, Account.hidden.is_(False))
        .order_by(Account.id)
    ).all()
    metrics = _metrics(session, members, today, range_key)
    dashboard = read_dashboard(session, today)
    rows = [
        dict(row)
        for key in ("assets_groups", "liabilities_groups")
        for group in (dashboard[key] if dashboard else [])
        if group["type"] == type_key
        for row in group["accounts"]
    ]
    gross = (
        None if any(r["balance"] is None for r in rows) else sum(abs(r["balance"]) for r in rows)
    )
    for row in rows:
        row["bar_pct"] = abs(row["balance"]) / gross * 100 if gross else None
    signed = metrics["balance"]
    if signed is not None and metrics["is_liability"]:
        signed = -signed
    side, net = _shares(dashboard, gross, signed, metrics["is_liability"])
    if any(a.is_liability for a in members) and not metrics["is_liability"]:
        side = None
    return {
        **metrics,
        "total": metrics["balance"],
        "name": TYPES[type_key],
        "blurb": BLURBS.get(type_key),
        "type_key": type_key,
        "rows": rows,
        "eyebrow": "Balance owed" if metrics["is_liability"] else "Balance",
        "share_side": side,
        "share_net_worth": net,
        "account_count": len(members),
        "year_change": metrics["deltas"]["1Y"],
        "is_demo": bool(dashboard and dashboard["is_demo"]),
    }
