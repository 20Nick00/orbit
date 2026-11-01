from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from paystubs.analysis import (
    annualized,
    facts,
    latest_stub,
    pay_frequency,
    segments,
    waterfall,
    ytd_totals,
)
from paystubs.limits import limits_for
from paystubs.rules import StubLine


def test_hand_calculated_ytd(stubs):
    totals = ytd_totals(stubs, 2026)
    assert totals.totals == {
        "gross": D("256340"),
        "tax": D("75272"),
        "pretax": D("27740"),
        "benefit": D("4400"),
        "posttax": D("2400"),
        "employer": D("8940"),
        "net": D("146528"),
    }
    assert totals.warnings == []
    assert len([row for row in totals.lines if row.group == "gross"]) == 2


def test_hand_calculated_facts(stubs):
    result = facts(stubs, 2026, limits_for(2026), "self")
    assert result.gross_ytd == D("256340")
    assert result.effective_tax_rate == D("75272") / D("256340")
    assert result.federal_fica_rate == D("55472") / D("256340")
    assert result.state_city_rate == D("19800") / D("256340")
    assert result.retirement_401k.ytd == D("26840")
    assert result.retirement_401k.limit == D("24500")
    assert result.retirement_401k.remaining == D("0")
    assert result.retirement_401k.over is True
    assert result.hsa.ytd == D("3900")
    assert result.hsa.limit == D("4400")
    assert result.hsa.remaining == D("500")
    assert result.hsa.over is False
    assert facts(stubs, 2026, limits_for(2026), "family").hsa.limit == D("8750")
    unknown = facts(stubs, 2026, None, "self")
    assert unknown.hsa.limit is None and unknown.hsa.remaining is None and unknown.hsa.over is None


def test_hand_calculated_waterfall(stubs):
    flow = waterfall(ytd_totals(stubs, 2026), D("60000"), D("140000"))
    assert [step.label for step in flow] == [
        "Gross",
        "Taxes",
        "Pre-tax savings",
        "Benefits",
        "Spending",
        "Pre-tax back in",
        "Employer match",
        "Market & home",
        "Net worth change",
    ]
    assert [step.value for step in flow] == list(
        map(
            D, ["256340", "-75272", "-27740", "-4400", "-60000", "27740", "8940", "14392", "140000"]
        )
    )
    assert sum(step.value for step in flow[:-1]) == D("140000")
    assert flow[0].is_total and flow[-1].is_total
    assert not any(step.is_total for step in flow[1:-1])
    assert not any(step.missing for step in flow)
    assert flow.posttax_note_amount == D("2400")


@pytest.mark.parametrize(
    "spending,change,missing",
    [
        (None, "140000", {4, 7}),
        ("60000", None, {7, 8}),
        (None, None, {4, 7, 8}),
        ("0", "0", set()),
    ],
)
def test_waterfall_missing_states(stubs, spending, change, missing):
    flow = waterfall(
        ytd_totals(stubs, 2026),
        D(spending) if spending is not None else None,
        D(change) if change is not None else None,
    )
    assert {i for i, step in enumerate(flow) if step.missing} == missing
    assert {i for i, step in enumerate(flow) if step.value is None} == missing


def test_latest_and_ties(stubs):
    assert latest_stub(stubs) is stubs[0]
    tied = replace(stubs[0], employer="Another fake employer")
    assert latest_stub([*stubs, tied], 2026) is tied
    assert latest_stub(stubs, 2025) is None
    assert latest_stub([]) is None
    assert latest_stub([replace(stubs[0], pay_date=None)]) is None


@pytest.mark.parametrize(
    "days,expected",
    [
        (5, "Weekly"),
        (8, "Weekly"),
        (12, "Biweekly"),
        (14, "Semi-monthly"),
        (15, "Semi-monthly"),
        (16, "Semi-monthly"),
        (28, "Monthly"),
        (31, "Monthly"),
        (9, None),
    ],
)
def test_frequency(stubs, days, expected):
    start = date(2026, 1, 1)
    assert (
        pay_frequency(
            replace(stubs[0], period_start=start, period_end=start + timedelta(days=days - 1))
        )
        == expected
    )
    assert pay_frequency(replace(stubs[0], period_start=None)) is None


def test_segments(stubs):
    result = segments(stubs[0])
    assert [s.group for s in result] == ["tax", "pretax", "benefit", "net"]
    assert [s.amount for s in result] == [D("3160"), D("1207"), D("160"), D("5890")]
    assert result[0].share == D("3160") / D("10417")
    assert "posttax" in [s.group for s in segments(stubs[1])]
    assert all(s.share is None for s in segments(replace(stubs[0], gross=D("0"))))


def test_later_stub_without_ytd_and_next_anchor(stubs):
    later = replace(
        stubs[1],
        pay_date=date(2026, 7, 1),
        gross_ytd=None,
        net_ytd=None,
        lines=[replace(line, ytd=None) for line in stubs[1].lines],
    )
    totals = ytd_totals([later, *stubs], 2026)
    assert totals.totals["gross"] == D("260340")
    assert totals.totals["net"] == D("148922")
    assert totals.totals["tax"] == D("76278")
    assert totals.totals["pretax"] == D("28040")
    assert totals.warnings == []
    newer = replace(later, pay_date=date(2026, 7, 15), gross_ytd=D("60000"))
    assert ytd_totals([*stubs, later, newer], 2026).totals["gross"] == D("268340")


def test_no_ytd_sums_current_and_filters_year(stubs):
    a = replace(
        stubs[1],
        gross_ytd=None,
        net_ytd=None,
        lines=[replace(line, ytd=None) for line in stubs[1].lines],
    )
    b = replace(a, pay_date=date(2026, 7, 1))
    old = replace(a, pay_date=date(2025, 12, 31))
    totals = ytd_totals([a, b, old], 2026)
    assert totals.totals["gross"] == D("8000")
    assert totals.totals["tax"] == D("2012")
    assert totals.totals["net"] == D("4788")


def test_missing_current_never_becomes_zero(stubs):
    later = replace(
        stubs[1],
        pay_date=date(2026, 7, 1),
        gross=None,
        gross_ytd=None,
        lines=[StubLine("Federal Withholding", "tax", None, None)],
    )
    totals = ytd_totals([*stubs, later], 2026)
    assert totals.totals["gross"] is None
    assert totals.totals["tax"] is None
    assert waterfall(totals, D("0"), D("0"))[7].missing


def test_empty_year_and_ytd_warning(stubs):
    empty = ytd_totals(stubs, 2030)
    assert all(value is None for value in empty.totals.values())
    assert empty.warnings
    assert facts([], 2026, None, "self").retirement_401k.ytd is None
    bad = replace(stubs[0], net_ytd=D("117802"))
    assert ytd_totals([bad, stubs[1]], 2026).warnings


def test_small_employer_discrepancies_warn_when_combined(stubs):
    a = replace(stubs[0], net_ytd=D("117800.60"))
    b = replace(stubs[1], net_ytd=D("28728.60"))
    assert ytd_totals([a, b], 2026).warnings == [
        "Combined YTD gross minus deductions differs from stated net"
    ]


def test_annualized():
    assert annualized(D("100"), date(2026, 1, 10)) == D("3650")
    assert annualized(D("100"), date(2026, 12, 31)) == D("100")
    assert annualized(None, date(2026, 1, 1)) is None


@pytest.mark.parametrize(
    "year,expected",
    [
        (2024, ("23000", "7500", None, "4150", "8300", "1000")),
        (2025, ("23500", "7500", "11250", "4300", "8550", "1000")),
        (2026, ("24500", "8000", "11250", "4400", "8750", "1000")),
    ],
)
def test_limit_table(year, expected):
    limits = limits_for(year)
    assert limits is not None
    assert (
        limits.deferral_401k,
        limits.catchup_50,
        limits.catchup_60_63,
        limits.hsa_self,
        limits.hsa_family,
        limits.hsa_catchup_55,
    ) == tuple(D(v) if v is not None else None for v in expected)
    assert limits_for(2030) is None
