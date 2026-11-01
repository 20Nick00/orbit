"""Shared display and CSV fields for the two historical views."""

from dataclasses import asdict
from decimal import Decimal

from app.csv_export import csv_response
from app.templating import templates
from core.rollups import _earliest, _previous

VALUE_FIELDS = (
    "net_worth_end",
    "net_worth_change",
    "income",
    "spending",
    "kept",
    "savings_rate",
    "contributions",
    "market_gain",
)
VALUE_HEADERS = (
    "Net worth end",
    "Change",
    "Income",
    "Spending",
    "Kept",
    "Savings rate",
    "Contributions",
    "Market gain",
)


def money(value):
    return "—" if value is None else f"{'−' if value < 0 else ''}${abs(value):,.2f}"


def rate(value):
    return "—" if value is None else f"{value * 100:.1f}%"


def sign(value):
    return "" if value is None or value == 0 else "pos" if value > 0 else "neg"


templates.env.filters.update(money=money, rate=rate, sign=sign)


def chart_rows(rows):
    # Preserve Decimal values as strings until the browser draws the chart.
    return [
        {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in (asdict(row) if hasattr(row, "__dataclass_fields__") else row).items()
        }
        for row in rows
    ]


def trends_start(active, today, *, _loaded=None):
    earliest = _loaded.earliest if _loaded is not None else _earliest(active, today)
    if earliest is None:
        return None
    start = today.strftime("%Y-%m")
    for _ in range(11):
        start = _previous(start)
    return max(start, earliest.strftime("%Y-%m"))


def csv_rate(value):
    return None if value is None else value.quantize(Decimal("0.0001"))


def rollup_csv(filename, rows, *, yearly=False):
    fields = (
        ("year" if yearly else "month",)
        + VALUE_FIELDS
        + (
            "new_accounts",
            "ytd" if yearly else "closed",
        )
        + (("note",) if yearly else ())
    )
    headers = (
        ("Year" if yearly else "Month",)
        + VALUE_HEADERS
        + (
            "New accounts",
            "YTD" if yearly else "Closed",
        )
        + (("Notes",) if yearly else ())
    )
    return csv_response(
        filename,
        headers,
        (
            [
                csv_rate(getattr(r, key)) if key == "savings_rate" else getattr(r, key)
                for key in fields
            ]
            for r in rows
        ),
    )
