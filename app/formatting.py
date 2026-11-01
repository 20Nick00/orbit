"""Prototype display helpers; monetary inputs are integer cents."""

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from core.models import _money


def fmt(value):
    if value is None:
        return "—"
    dollars = (Decimal(abs(value)) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return ("−" if value < 0 else "") + f"${dollars:,.0f}"


def fmt_s(value):
    return fmt(value)


def d_txt(value):
    if value is None:
        return "—"
    amount = value["d"] if isinstance(value, dict) else value
    return ("+" if amount > 0 else "") + fmt(amount)


def pct(value, digits=1):
    if value is None:
        return "—"
    percentage = (Decimal(str(value)) * 100).quantize(
        Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP
    )
    return f"{percentage:.{digits}f}%".replace("-", "−")


def updated(value, today):
    if value is None:
        return "updated —"
    age = (today - date.fromisoformat(value)).days
    return "updated today" if age == 0 else f"updated {age}d ago"


def parse_balance(value):
    value = value.strip().replace("−", "-")
    if not re.fullmatch(
        r"-?\$?(?:(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?|\.[0-9]{1,2})",
        value,
    ):
        raise ValueError("Enter an amount with at most two decimal places")
    return _money(value.replace("$", "").replace(",", ""))


def money2(value):
    if value is None:
        return "—"
    return ("−" if value < 0 else "") + f"${Decimal(abs(value)) / 100:,.2f}"


def d_txt2(value):
    if value is None:
        return "—"
    amount = value["d"] if isinstance(value, dict) else value
    return ("+" if amount > 0 else "") + money2(amount)


def day_label(value):
    if value is None:
        return "—"
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return f"{value:%b} {value.day}, {value.year}"


def month_label(value):
    if value is None:
        return "—"
    if isinstance(value, str):
        value = date.fromisoformat(value + "-01")
    return f"{value:%B %Y}"


FILTERS = {
    "fmt": fmt,
    "fmtS": fmt_s,
    "dTxt": d_txt,
    "dTxt2": d_txt2,
    "pct": pct,
    "updated": updated,
    "money2": money2,
    "day_label": day_label,
    "month_label": month_label,
}
