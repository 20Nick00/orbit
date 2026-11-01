"""Pure home-equity calculation; callers supply visible accounts and dated balances."""

import re
from decimal import Decimal

VEHICLE_PATTERN = re.compile(r"\b(vehicle|car|truck|auto|motorcycle|boat|rv)\b", re.I)


def equity_summary(accounts, balances, links):
    """Balances are Decimal dollars with positive debt owed; shared debt counts once.

    Callers check the currencies of ok rows before displaying the total. No
    mixed-currency total is computed here.
    """
    rows = []
    good_assets = {}
    good_debts = {}
    currencies = set()
    for account_id, account in accounts.items():
        if (
            account.hidden
            or account.type != "property"
            or VEHICLE_PATTERN.search(account.name or "")
            or VEHICLE_PATTERN.search(account.subtype or "")
        ):
            continue
        link = links.get(account_id)
        row = dict(
            account_id=account_id,
            name=account.name,
            currency=account.currency,
            status="unlinked",
            equity=None,
            linked_liability=link,
        )
        rows.append(row)
        if not link:
            continue
        row["status"] = "invalid"
        if link == "none":
            debt_ids = set()
        else:
            parts = link.split(",")
            if any(not re.fullmatch(r"[0-9]+", part.strip()) for part in parts):
                continue
            try:
                debt_ids = {int(part) for part in parts}
            except ValueError:
                continue
        if any(
            debt_id not in accounts
            or accounts[debt_id].hidden
            or not accounts[debt_id].is_liability
            or accounts[debt_id].currency != account.currency
            or debt_id == account_id
            for debt_id in debt_ids
        ):
            continue
        if any(balances.get(aid) is None for aid in debt_ids | {account_id}):
            row["status"] = "missing"
            continue
        debts = {aid: balances[aid] for aid in debt_ids}
        row.update(status="ok", equity=balances[account_id] - sum(debts.values(), Decimal(0)))
        good_assets[account_id] = balances[account_id]
        good_debts.update(debts)
        currencies.add(account.currency)
    return dict(
        total=(sum(good_assets.values(), Decimal(0)) - sum(good_debts.values(), Decimal(0)))
        if good_assets and len(currencies) == 1
        else None,
        rows=rows,
        unlinked_count=sum(row["status"] == "unlinked" for row in rows),
        problem_count=sum(row["status"] in {"invalid", "missing"} for row in rows),
    )
