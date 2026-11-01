from datetime import date

import pytest

from app.formatting import d_txt, fmt, fmt_s, pct, updated


@pytest.mark.parametrize("formatter", [fmt, fmt_s])
@pytest.mark.parametrize(
    "amount,expected", [(None, "—"), (0, "$0"), (50, "$1"), (-50, "−$1"), (123456, "$1,235")]
)
def test_money_formats(formatter, amount, expected):
    assert formatter(amount) == expected


def test_delta_percent_and_age():
    assert d_txt(None) == "—"
    assert d_txt({"d": 1050}) == "+$11"
    assert d_txt({"d": -1050}) == "−$11"
    assert pct(None) == "—"
    assert pct(0.1255) == "12.6%"
    assert pct(0.125, 0) == "13%"
    assert pct(-0.125, 0) == "−13%"
    assert updated("2026-10-31", date(2026, 10, 31)) == "updated today"
    assert updated("2026-10-31", date(2026, 11, 1)) == "updated 1d ago"
