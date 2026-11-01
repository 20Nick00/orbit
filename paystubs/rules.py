"""Validated employer mappings and conservative classification."""

import logging
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path

import yaml

from .parsers.pdf import ParsedStub, normalize

LOG = logging.getLogger(__name__)
GROUPS = frozenset({"gross", "net", "tax", "pretax", "posttax", "benefit", "employer", "ignore"})
LINE_GROUPS = frozenset({"tax", "pretax", "posttax", "benefit", "employer"})
ZERO = Decimal("0")
DEDUCTIONS = LINE_GROUPS - {"employer"}
MULTIPLE_STATEMENTS = "This PDF seems to hold more than one statement"
GROSS_YTD_FELL = "Gross YTD fell compared with the previous stub. This may be a different employer."
GENERIC_MATCHES = frozenset(
    "earnings statement|pay statement|payroll statement|pay stub|paystub|pay slip|payslip|"
    "statement of earnings|direct deposit|payroll|adp|paychex|gusto|workday|ukg|paylocity|"
    "rippling|trinet|bamboohr|justworks|quickbooks".split("|")
)


def reviewable_problem(problem: str) -> bool:
    return problem in {MULTIPLE_STATEMENTS, GROSS_YTD_FELL} or (
        problem.startswith("'")
        and problem.endswith(
            "' appears twice with identical amounts; check it isn't two different lines"
        )
    )


@dataclass
class Rule:
    employer: str
    match: list[str]
    labels: dict[str, str]
    limits: dict[str, str] = field(default_factory=dict)


class LoadedRules(list[Rule]):
    """Iterable rules plus names of malformed files skipped during loading."""

    def __init__(self) -> None:
        super().__init__()
        self.errors: list[str] = []


@dataclass
class StubLine:
    label: str
    group: str
    amount: Decimal | None
    ytd: Decimal | None
    limit: str | None = None


@dataclass
class ClassifyResult:
    gross: Decimal | None
    net: Decimal | None
    gross_ytd: Decimal | None
    net_ytd: Decimal | None
    lines: list[StubLine]
    unknown_labels: list[str]
    problems: list[str]
    reconcile_diff: Decimal | None

    @property
    def reconciles(self) -> bool:
        return self.reconcile_diff is not None and abs(self.reconcile_diff) <= Decimal("0.02")


def slugify(employer: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", employer.lower()).strip("-")
    if not slug:
        raise ValueError("Employer must contain letters or numbers")
    return slug


def _mapping(value: object, allowed: frozenset[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("Invalid mapping")
    result: dict[str, str] = {}
    for key, group in value.items():
        if (
            not isinstance(key, str)
            or not normalize(key)
            or not isinstance(group, str)
            or group not in allowed
        ):
            raise ValueError("Invalid mapping entry")
        result[normalize(key)] = group
    return result


def _validate(data: object, check_links: bool = True) -> Rule:
    if not isinstance(data, dict):
        raise ValueError("Invalid rule")
    employer, matches = data.get("employer"), data.get("match")
    if not isinstance(employer, str) or not employer.strip():
        raise ValueError("Missing employer")
    slugify(employer)
    if (
        not isinstance(matches, list)
        or not matches
        or any(not isinstance(s, str) or not normalize(s) for s in matches)
    ):
        raise ValueError("Invalid match list")
    labels = _mapping(data.get("labels"), GROUPS)
    limits = _mapping(data.get("limits", {}), frozenset({"401k", "hsa"}))
    for label, limit in limits.items():
        if not check_links:
            continue
        if label not in labels or labels[label] in {"gross", "net", "ignore"}:
            raise ValueError("Limit must refer to a classified deduction or contribution")
        if limit == "401k" and labels[label] not in {"pretax", "posttax"}:
            raise ValueError("401k deferral limits apply only to employee contributions")
    return Rule(
        employer.strip(), list(dict.fromkeys(normalize(s) for s in matches)), labels, limits
    )


def _data(rule: Rule) -> dict:
    return {
        "employer": rule.employer,
        "match": rule.match,
        "labels": rule.labels,
        "limits": rule.limits,
    }


def _read(path: Path) -> Rule:
    return _validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_rules(directory: str | Path) -> LoadedRules:
    result = LoadedRules()
    for path in sorted(Path(directory).glob("*.yaml")):
        if path.name == "example.yaml":
            continue
        try:
            result.append(_read(path))
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            result.errors.append(path.name)
            LOG.warning("Paystub rule: InvalidRule")
    return result


def get_rule(directory: str | Path, slug: str) -> Rule | None:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("Invalid rule slug")
    path = Path(directory) / f"{slug}.yaml"
    if not path.exists():
        return None
    try:
        return _read(path)
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        LOG.warning("Paystub rule: InvalidRule")
        return None


def save_rule(
    directory: str | Path, rule: Rule, *, slug: str | None = None, replace_mappings: bool = False
) -> Rule:
    incoming = _validate(_data(rule), check_links=False)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if slug is not None and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("Invalid rule slug")
    target = directory / f"{slug or slugify(incoming.employer)}.yaml"
    if target.exists():
        existing = _read(target)  # Refuse to overwrite a malformed file.
        if normalize(existing.employer) != normalize(incoming.employer):
            raise ValueError("Another employer already uses this slug")
        incoming = Rule(
            **{
                "employer": incoming.employer,
                "match": existing.match + incoming.match,
                "labels": incoming.labels
                if replace_mappings
                else existing.labels | incoming.labels,
                "limits": (
                    incoming.limits
                    if replace_mappings
                    else {
                        key: value
                        for key, value in existing.limits.items()
                        if key not in incoming.labels
                    }
                    | incoming.limits
                ),
            }
        )
    incoming = _validate(_data(incoming))
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            yaml.safe_dump(_data(incoming), stream, sort_keys=False, allow_unicode=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return incoming


def match_rule(rules: Iterable[Rule], text: str) -> Rule | None:
    text = normalize(text)
    winners: list[Rule] = []
    longest = 0
    for rule in rules:
        length = max(
            (len(normalize(s)) for s in rule.match if normalize(s) and normalize(s) in text),
            default=0,
        )
        if length > longest:
            winners, longest = [rule], length
        elif length and length == longest:
            winners.append(rule)
    if not winners or len({normalize(r.employer) for r in winners}) != 1:
        return None
    return winners[0]


def suggest_group(label: str) -> str | None:
    label = normalize(label)
    patterns = (
        (r"\b(?:employer|er|company|match)\b", "employer"),
        (r"\bnet\b", "net"),
        (r"\bgross\b|\btotal earnings\b", "gross"),
        (r"roth|after[- ]?tax|post[- ]?tax", "posttax"),
        (r"401|403|\bhsa\b|pre[- ]?tax", "pretax"),
        (
            r"federal|\bfed\b|\bfit\b|fica|social security|oasdi|medicare|state|city|"
            r"\bnyc\b|local|\bsdi\b|tax",
            "tax",
        ),
        (r"medical|dental|vision|health|life|insurance|premium|\bfsa\b|commuter", "benefit"),
        (r"total|regular|overtime|salary|bonus|hours|rate", "ignore"),
    )
    return next((group for pattern, group in patterns if re.search(pattern, label)), None)


def suggest_limit(label: str, group: str | None) -> str | None:
    label = normalize(label)
    if re.search(r"after[- ]?tax", label):
        return None
    if re.search(r"\bhsa\b", label):
        return "hsa"
    if group in {"pretax", "posttax"} and re.search(r"\b(?:401\s*\(?k\)?|403\s*\(?b\)?)", label):
        return "401k"
    return None


def complete_sum(values: Iterable[Decimal | None]) -> Decimal | None:
    values = list(values)
    return None if any(v is None for v in values) else sum(values, ZERO)


def classify(
    parsed: ParsedStub, rule: Rule, previous_ytd: dict[str, Decimal] | None = None
) -> ClassifyResult:
    rule = _validate(_data(rule))
    tax_amounts = [
        line.amount
        for line in parsed.lines
        if rule.labels.get(normalize(line.label)) == "tax" and line.amount
    ]
    sample = tax_amounts or [
        line.amount
        for line in parsed.lines
        if rule.labels.get(normalize(line.label)) in DEDUCTIONS and line.amount
    ]
    flip = sum(amount < ZERO for amount in sample) > len(sample) / 2
    roles: dict[str, list] = {"gross": [], "net": []}
    lines: list[StubLine] = []
    unknown: list[str] = []
    problems: list[str] = []
    if parsed.pay_date is None:
        problems.append("Pay date missing")
    for line in parsed.lines:
        key = normalize(line.label)
        group = rule.labels.get(key)
        if flip and group in DEDUCTIONS:
            line = replace(
                line,
                amount=-line.amount if line.amount is not None else None,
                ytd=-line.ytd if line.ytd is not None else None,
            )
        # A running total can't be negative, so this is the stub's print style, not a reversal.
        if group == "employer" and line.ytd is not None and line.ytd < ZERO:
            line = replace(
                line,
                amount=-line.amount if line.amount is not None else None,
                ytd=-line.ytd,
            )
        if (
            group in LINE_GROUPS
            and line.ytd is None
            and line.amount is not None
            and previous_ytd is not None
            and key in previous_ytd
            and line.amount >= previous_ytd[key]
        ):
            line = replace(line, amount=ZERO, ytd=line.amount)
        if group in roles and line.merged:
            if MULTIPLE_STATEMENTS not in problems:
                problems.append(MULTIPLE_STATEMENTS)
        if group in LINE_GROUPS and line.repeats:
            problems.append(
                f"'{line.label}' appears twice with identical amounts; "
                "check it isn't two different lines"
            )
        if group is None:
            unknown.append(line.label)
            problems.append(f"Unknown label: {line.label}")
        elif group in roles:
            roles[group].append(line)
        elif group in LINE_GROUPS:
            lines.append(StubLine(line.label, group, line.amount, line.ytd, rule.limits.get(key)))
            if line.amount is None:
                problems.append(f"Current amount missing: {line.label}")
    totals: dict[str, Decimal | None] = {}
    for group, rows in roles.items():
        totals[group] = complete_sum(row.amount for row in rows) if rows else None
        totals[group + "_ytd"] = complete_sum(row.ytd for row in rows) if rows else None
        if totals[group] is None:
            problems.append(f"{group.capitalize()} missing")
    deductions = complete_sum(line.amount for line in lines if line.group != "employer")
    gross, net = totals["gross"], totals["net"]
    diff = (
        gross - deductions - net
        if gross is not None and deductions is not None and net is not None
        else None
    )
    return ClassifyResult(
        gross, net, totals["gross_ytd"], totals["net_ytd"], lines, unknown, problems, diff
    )
