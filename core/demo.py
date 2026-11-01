"""Reproducible prototype data. Run with python -m core.demo."""

import argparse
import json
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from sqlalchemy import delete, insert, or_, select

from core import rollups
from core.db import session_scope
from core.models import (
    Account,
    AccountFact,
    BalanceSnapshot,
    Holding,
    Institution,
    MonthEndArchive,
    MonthEndSnapshot,
    Paystub,
    PaystubLine,
    Transaction,
    Valuation,
)

DATA = json.loads(Path(__file__).with_name("demo_data.json").read_text(encoding="utf-8"))
ACCTS = DATA["accounts"]
MERCH = DATA["merchants"]
DAYS = 730
EMPLOYER = "Example Company"
# Per-check amounts from the prototype's Paycheck view: label, group, amount, limit bucket.
GROSS_PAY = Decimal(10417)
PAY_LINES = (
    ("Federal income tax", "tax", 1493, None),
    ("State income tax", "tax", 540, None),
    ("City income tax", "tax", 330, None),
    ("Social Security", "tax", 646, None),
    ("Medicare", "tax", 151, None),
    ("401(k) pre-tax", "pretax", 1042, "401k"),
    ("HSA", "pretax", 165, "hsa"),
    ("Medical premium", "benefit", 142, None),
    ("Dental premium", "benefit", 18, None),
    ("Employer 401(k) match", "employer", 417, None),
)


class DemoRefusedError(ValueError):
    """The database contains data the demo must not replace."""


class Random:
    def __init__(self, seed=20261031):
        self.seed = seed

    def __call__(self):
        self.seed = (self.seed * 1664525 + 1013904223) % 4294967296
        return self.seed / 4294967296


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def generate(as_of):
    """Keep every prototype random call, including zero volatility and short circuits."""
    rnd = Random()
    histories = {}
    for account in ACCTS:
        history = [0.0] * DAYS
        history[-1] = account["bal"]
        for index in range(DAYS - 2, -1, -1):
            previous = history[index + 1]
            if account.get("step"):
                value = (
                    previous / (1 + account["drift"] * account["step"] + (rnd() - 0.5) * 0.02)
                    if (DAYS - 1 - index) % account["step"] == 0
                    else previous
                )
            elif account["type"] == "cards":
                value = -abs(previous) * (0.85 + rnd() * 0.3)
            else:
                value = previous / (1 + account["drift"] + (rnd() - 0.5) * 2 * account["vol"])
            history[index] = value
        histories[account["id"]] = history
    transactions = []

    def add(day, merchant, category, amount, account):
        transactions.append(
            {
                "date": day,
                "merchant": merchant,
                "category": category,
                "amount": money(amount),
                "account": account,
            }
        )

    for offset in range(95):
        day = as_of - timedelta(days=offset)
        count = 3 if offset == 0 else 1 + int(rnd() * 3)
        for _ in range(count):
            merchant = MERCH[int(rnd() * len(MERCH))]
            amount = -(merchant[2] + rnd() * (merchant[3] - merchant[2]))
            account = "amx" if rnd() < 0.6 else "csr" if rnd() < 0.5 else "chk"
            add(day, merchant[0], merchant[1], amount, account)
        if day.day == 1:
            add(day, "Mortgage payment", "Housing", -3105, "chk")
            add(day, "HOA · Example building", "Housing", -640, "chk")
            add(day, "Principal applied", "Transfer", 510, "mtg")
        if day.day in (15, 30):
            add(day, "Example Company payroll", "Income", 5890, "chk")
            add(day, "401(k) contribution + match", "Transfer", 1459, "401")
            add(day, "HSA contribution", "Transfer", 165, "hsa")
        if day.day == 2:
            add(day, "Transfer to Ally Savings", "Transfer", -1500, "chk")
            add(day, "Transfer from Chase", "Transfer", 1500, "sav")
        if day.day == 14:
            add(day, "Amex autopay", "Transfer", -(2100 + rnd() * 900), "chk")
        if day.day == 6:
            add(day, "Student loan autopay", "Transfer", -412, "chk")
        if day.day == 9:
            add(day, "Personal loan autopay", "Transfer", -310, "chk")
        if day.day == 20:
            add(day, "Life insurance premium", "Insurance", -185, "chk")
        if day.day == 28:
            add(day, "Interest paid", "Income", 97 + rnd() * 8, "sav")
        if offset % 9 == 3:
            value = (rnd() - 0.4) * 900
            add(day, "Wallet adjustment", "Income" if value > 0 else "Fees", value, "pm")
        if offset % 11 == 5:
            add(day, "Buy VTI", "Transfer", -2000, "brk")
    transactions.sort(key=lambda row: row["date"], reverse=True)
    return histories, transactions, rnd


def seed_demo(session, as_of, transactions=0):
    """Replace demo rows in the caller's transaction; refuse before any mutation."""
    if transactions < 0:
        raise ValueError("Transaction count cannot be negative")
    # Do not autoflush pending caller changes during the read-only safety check.
    with session.no_autoflush:
        existing = session.execute(
            select(Account.id, Institution.aggregator).outerjoin(Institution)
        ).all()
        if any(row.aggregator != "demo" for row in existing):
            raise DemoRefusedError("Demo refused: database contains non-demo accounts.")
        ids = [row.id for row in existing]
        if (
            ids
            and session.scalar(
                select(MonthEndArchive.account_id)
                .where(MonthEndArchive.account_id.in_(ids))
                .limit(1)
            )
            is not None
        ):
            raise DemoRefusedError("Demo refused: demo accounts have permanent month-end archives.")
        if (
            ids
            and session.scalar(
                select(MonthEndSnapshot.account_id)
                .where(MonthEndSnapshot.account_id.in_(ids))
                .limit(1)
            )
            is not None
        ):
            raise DemoRefusedError("Demo refused: demo accounts have immutable month-end rows.")
        if (
            ids
            and session.scalar(
                select(BalanceSnapshot.account_id)
                .where(BalanceSnapshot.account_id.in_(ids), BalanceSnapshot.source != "demo")
                .limit(1)
            )
            is not None
        ):
            raise DemoRefusedError(
                "Demo refused: an account holds a balance that did not come from the demo."
            )
        if (
            session.scalar(
                select(Paystub.id)
                .where(or_(Paystub.employer != EMPLOYER, Paystub.source_file.is_not(None)))
                .limit(1)
            )
            is not None
        ):
            raise DemoRefusedError("Demo refused: database contains imported paystubs.")
        if session.new or session.dirty or session.deleted:
            raise DemoRefusedError("Demo refused: save or discard pending changes first.")
    histories, transaction_rows, rnd = generate(as_of)
    if ids:
        for model in (Transaction, Holding, Valuation, AccountFact, BalanceSnapshot):
            session.execute(delete(model).where(model.account_id.in_(ids)))
        session.execute(delete(Account).where(Account.id.in_(ids)))
    session.execute(delete(Institution).where(Institution.aggregator == "demo"))
    institutions = {}
    for account in ACCTS:
        name = account["inst"]
        if name not in institutions:
            institution = Institution(name=name, aggregator="demo", external_id=name)
            session.add(institution)
            session.flush()
            institutions[name] = institution.id
    accounts = {}
    for item in ACCTS:
        account = Account(
            institution_id=institutions[item["inst"]],
            name=item["name"],
            type=item["type"],
            is_manual=item["id"] == "home",
        )
        session.add(account)
        session.flush()
        accounts[item["id"]] = account.id
    fact_rows, snapshot_rows = [], []
    first = as_of - timedelta(days=DAYS - 1)
    for item in ACCTS:
        account_id = accounts[item["id"]]
        session.add_all(
            Holding(
                account_id=account_id,
                date=as_of,
                ticker=ticker,
                name=name,
                value=money(Decimal(str(item["bal"])) * Decimal(str(weight)) / 100),
                qty=None,
                cost_basis=None,
            )
            for ticker, name, weight in item.get("holdings", [])
        )
        facts = dict(item["facts"])
        facts["subtitle"] = item["sub"]
        if item["id"] == "home":
            facts["linked_liability"] = str(accounts["mtg"])
        fact_rows.extend(
            {"account_id": account_id, "key": key, "value": value} for key, value in facts.items()
        )
        history = histories[item["id"]]
        for index, value in enumerate(history):
            if item.get("step") and index and value == history[index - 1]:
                continue
            snapshot_rows.append(
                {
                    "account_id": account_id,
                    "date": first + timedelta(days=index),
                    "balance": money(-value if item["bal"] < 0 else value),
                    "source": "demo",
                }
            )
    session.execute(insert(AccountFact), fact_rows)
    session.execute(insert(BalanceSnapshot), snapshot_rows)
    filler = Random(20261032)
    while len(transaction_rows) < transactions:
        merchant = MERCH[int(filler() * len(MERCH))]
        transaction_rows.append(
            {
                "date": as_of - timedelta(days=int(filler() * DAYS)),
                "merchant": merchant[0],
                "category": merchant[1],
                "amount": money(-(merchant[2] + filler() * (merchant[3] - merchant[2]))),
                "account": ("amx", "csr", "chk")[int(filler() * 3)],
            }
        )
    session.execute(
        insert(Transaction),
        [
            {
                "account_id": accounts[row["account"]],
                **{k: v for k, v in row.items() if k != "account"},
            }
            for row in transaction_rows
        ],
    )
    _seed_paystubs(session, as_of)
    rollups.rebuild(session, as_of)
    return {"accounts": len(accounts), "transactions": len(transaction_rows), "next_random": rnd()}


def _seed_paystubs(session, as_of):
    """Semi-monthly stubs for the current year, on the demo's payroll days."""
    session.execute(delete(PaystubLine))
    session.execute(delete(Paystub))
    deductions = sum(row[2] for row in PAY_LINES if row[1] != "employer")
    net = GROSS_PAY - deductions
    checks = 0
    day = date(as_of.year, 1, 1)
    while day <= as_of:
        if day.day in (15, 30):
            checks += 1
            stub = Paystub(
                employer=EMPLOYER,
                pay_date=day,
                period_start=day - timedelta(days=14),
                period_end=day,
                gross=GROSS_PAY,
                net=net,
                gross_ytd=GROSS_PAY * checks,
                net_ytd=net * checks,
            )
            session.add(stub)
            session.flush()
            session.add_all(
                PaystubLine(
                    paystub_id=stub.id,
                    label=label,
                    group_=group,
                    amount=Decimal(amount),
                    ytd_amount=Decimal(amount * checks),
                    limit_bucket=bucket,
                )
                for label, group, amount, bucket in PAY_LINES
            )
        day += timedelta(days=1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--transactions", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        with session_scope() as session:
            summary = seed_demo(session, args.as_of, args.transactions)
    except (DemoRefusedError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(
        f"Demo ready: {summary['accounts']} accounts, {summary['transactions']} transactions, "
        f"as of {args.as_of.isoformat()}."
    )


if __name__ == "__main__":
    main()
