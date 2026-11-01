"""Pure paycheck and year-to-date views; no persistence or filesystem access."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .limits import YearLimits
from .parsers.pdf import normalize
from .rules import StubLine, complete_sum

ZERO = Decimal("0")
GROUPS = ("tax", "pretax", "benefit", "posttax", "employer")


@dataclass
class Stub:
    employer: str
    pay_date: date | None
    period_start: date | None
    period_end: date | None
    gross: Decimal | None
    net: Decimal | None
    gross_ytd: Decimal | None
    net_ytd: Decimal | None
    lines: list[StubLine]
    reconcile_diff: Decimal | None


@dataclass(frozen=True)
class Segment:
    group: str
    amount: Decimal | None
    share: Decimal | None


@dataclass(frozen=True)
class YtdLine:
    employer: str
    group: str
    label: str
    amount: Decimal | None
    limit: str | None = None


@dataclass
class YtdTotals:
    lines: list[YtdLine]
    totals: dict[str, Decimal | None]
    warnings: list[str]


@dataclass(frozen=True)
class LimitFact:
    ytd: Decimal | None
    limit: Decimal | None
    remaining: Decimal | None
    over: bool | None


@dataclass(frozen=True)
class Facts:
    gross_ytd: Decimal | None
    effective_tax_rate: Decimal | None
    federal_fica_rate: Decimal | None
    state_city_rate: Decimal | None
    retirement_401k: LimitFact
    hsa: LimitFact


@dataclass(frozen=True)
class Step:
    label: str
    label2: str
    value: Decimal | None
    is_total: bool
    missing: bool


class Waterfall(list[Step]):
    """Nine list-compatible steps, with post-tax disclosure metadata."""

    def __init__(self, steps: Iterable[Step], posttax_note_amount: Decimal | None):
        super().__init__(steps)
        self.posttax_note_amount = posttax_note_amount


def latest_stub(stubs: Iterable[Stub], year: int | None = None) -> Stub | None:
    candidates = [
        (stub.pay_date, stub.gross_ytd or ZERO, i, stub)
        for i, stub in enumerate(stubs)
        if stub.pay_date is not None and (year is None or stub.pay_date.year == year)
    ]
    return max(candidates, key=lambda item: item[:3])[3] if candidates else None


def pay_frequency(stub: Stub) -> str | None:
    if stub.period_start is None or stub.period_end is None:
        return None
    days = (stub.period_end - stub.period_start).days + 1
    if stub.period_start.day in {1, 16} and 13 <= days <= 16:
        return "Semi-monthly"
    for low, high, name in (
        (5, 8, "Weekly"),
        (12, 14, "Biweekly"),
        (15, 16, "Semi-monthly"),
        (28, 31, "Monthly"),
    ):
        if low <= days <= high:
            return name
    return None


def _ratio(amount: Decimal | None, gross: Decimal | None) -> Decimal | None:
    return amount / gross if amount is not None and gross is not None and gross != ZERO else None


def segments(stub: Stub) -> list[Segment]:
    result = []
    for group in ("tax", "pretax", "benefit", "posttax", "net"):
        amount = (
            stub.net
            if group == "net"
            else complete_sum(line.amount for line in stub.lines if line.group == group)
        )
        if group != "posttax" or amount != ZERO:
            result.append(Segment(group, amount, _ratio(amount, stub.gross)))
    return result


def _roll_forward(values: list[tuple[Decimal | None, Decimal | None]]) -> Decimal | None:
    anchors = [i for i, (_, ytd) in enumerate(values) if ytd is not None]
    if not anchors:
        return complete_sum(amount for amount, _ in values)
    index = anchors[-1]
    later = complete_sum(amount for amount, _ in values[index + 1 :])
    return values[index][1] + later if later is not None else None


def ytd_totals(stubs: Iterable[Stub], year: int) -> YtdTotals:
    ordered = sorted(
        (s for s in stubs if s.pay_date is not None and s.pay_date.year == year),
        key=lambda s: (s.pay_date, s.gross_ytd or ZERO),
    )
    history: dict[tuple[str, str], list[tuple[Decimal | None, Decimal | None]]] = {}
    metadata: dict[tuple[str, str], tuple[str, str, str | None, str]] = {}
    warnings = []
    previous_gross = {}
    for stub in ordered:
        employer = normalize(stub.employer)
        prior = previous_gross.get(employer)
        if prior is not None and stub.gross_ytd is not None and stub.gross_ytd < prior:
            warnings.append(f"Gross YTD fell between consecutive stubs for {stub.employer}")
        previous_gross[employer] = stub.gross_ytd
        entries = [
            StubLine("Gross", "gross", stub.gross, stub.gross_ytd),
            StubLine("Net", "net", stub.net, stub.net_ytd),
            *stub.lines,
        ]
        # Collapse repeated labels within one stub before choosing its YTD anchor.
        current: dict[tuple[str, str], list[StubLine]] = {}
        for line in entries:
            label_key = (
                f"total:{line.group}"
                if line.group in {"gross", "net"}
                else f"line:{normalize(line.label)}"
            )
            key = (employer, label_key)
            current.setdefault(key, []).append(line)
            metadata[key] = (stub.employer, line.label, line.limit, line.group)
        for key, lines in current.items():
            history.setdefault(key, []).append(
                (
                    complete_sum(line.amount for line in lines),
                    complete_sum(line.ytd for line in lines),
                )
            )
    rows = [
        YtdLine(
            metadata[key][0],
            metadata[key][3],
            metadata[key][1],
            _roll_forward(values),
            metadata[key][2],
        )
        for key, values in history.items()
    ]
    totals = {
        group: complete_sum(row.amount for row in rows if row.group == group) if ordered else None
        for group in ("gross", "net", *GROUPS)
    }
    # Check each employer as well as the combined result; errors must not cancel out.
    for employer in dict.fromkeys(normalize(row.employer) for row in rows):
        employer_rows = [row for row in rows if normalize(row.employer) == employer]
        by_group = {
            group: complete_sum(row.amount for row in employer_rows if row.group == group)
            for group in ("gross", "net", *GROUPS)
        }
        parts = [
            by_group[group] for group in ("gross", "net", "tax", "pretax", "benefit", "posttax")
        ]
        if all(value is not None for value in parts):
            gross, net, tax, pretax, benefit, posttax = parts
            if abs(gross - tax - pretax - benefit - posttax - net) > Decimal("1"):
                warnings.append(
                    "YTD gross minus deductions differs from stated net for "
                    f"{employer_rows[0].employer}"
                )
    combined = [totals[group] for group in ("gross", "net", "tax", "pretax", "benefit", "posttax")]
    if all(value is not None for value in combined):
        gross, net, tax, pretax, benefit, posttax = combined
        if abs(gross - tax - pretax - benefit - posttax - net) > Decimal("1"):
            warnings.append("Combined YTD gross minus deductions differs from stated net")
    if not ordered:
        warnings.append("No paystubs available for this year")
    return YtdTotals(rows, totals, warnings)


def annualized(amount: Decimal | None, as_of: date) -> Decimal | None:
    return None if amount is None else amount * Decimal(365) / Decimal(as_of.timetuple().tm_yday)


def _limit_fact(amount: Decimal | None, limit: Decimal | None) -> LimitFact:
    known = amount is not None and limit is not None
    return LimitFact(
        amount,
        limit,
        max(limit - amount, ZERO) if known else None,
        amount > limit if known else None,
    )


def facts(stubs: Iterable[Stub], year: int, limits: YearLimits | None, hsa_coverage: str) -> Facts:
    if hsa_coverage not in {"self", "family"}:
        raise ValueError("HSA coverage must be self or family")
    if limits is not None and limits.year != year:
        raise ValueError("IRS limits must match the requested year")
    ytd = ytd_totals(stubs, year)
    gross = ytd.totals["gross"]
    federal = re.compile(
        r"\b(?:fitw|fwt|fit|fed|federal|fica|oasdi|soc\s+sec|social\s+security|"
        r"ss\s+tax|ss\s+ee|medicare|med\s+ee|ee\s+medicare|med\s+tax)\b",
        re.I,
    )
    taxes = [row for row in ytd.lines if row.group == "tax"]
    fed = complete_sum(row.amount for row in taxes if federal.search(row.label)) if taxes else None
    state = (
        complete_sum(row.amount for row in taxes if not federal.search(row.label))
        if taxes
        else None
    )
    retirement_rows = [
        row for row in ytd.lines if row.limit == "401k" and row.group in {"pretax", "posttax"}
    ]
    hsa_rows = [row for row in ytd.lines if row.limit == "hsa"]
    retirement = complete_sum(row.amount for row in retirement_rows) if retirement_rows else None
    hsa = complete_sum(row.amount for row in hsa_rows) if hsa_rows else None
    return Facts(
        gross,
        _ratio(ytd.totals["tax"], gross),
        _ratio(fed, gross),
        _ratio(state, gross),
        _limit_fact(retirement, limits.deferral_401k if limits else None),
        _limit_fact(
            hsa,
            (limits.hsa_self if hsa_coverage == "self" else limits.hsa_family) if limits else None,
        ),
    )


def waterfall(
    totals: YtdTotals, spending: Decimal | None, net_worth_change: Decimal | None
) -> Waterfall:
    values = totals.totals
    gross, taxes, pretax, benefits, match = (
        values.get(group) for group in ("gross", "tax", "pretax", "benefit", "employer")
    )
    residual = None
    if all(v is not None for v in (gross, taxes, benefits, spending, match, net_worth_change)):
        residual = net_worth_change - (gross - taxes - benefits - spending + match)

    def negative(value: Decimal | None) -> Decimal | None:
        return -value if value is not None else None

    rows = [
        ("Gross", "", gross, True),
        ("Taxes", "", negative(taxes), False),
        ("Pre-tax savings", "", negative(pretax), False),
        ("Benefits", "", negative(benefits), False),
        ("Spending", "", negative(spending), False),
        ("Pre-tax back in", "", pretax, False),
        ("Employer match", "", match, False),
        ("Market & home", "", residual, False),
        ("Net worth change", "", net_worth_change, True),
    ]
    return Waterfall(
        (Step(label, label2, value, total, value is None) for label, label2, value, total in rows),
        values.get("posttax"),
    )
