"""Conservative CSV detection and mapping, independent of the web layer."""

import csv
import hashlib
import io
import json
import re
from calendar import monthrange
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from aggregators.file_store import ImportError, ImportRow, get_rule, save_rule, write_import
from core.clock import local_today
from core.db import write_savepoint
from core.models import _money

ROLES = (
    "date",
    "amount",
    "debit",
    "credit",
    "direction",
    "memo",
    "merchant",
    "balance",
    "category",
)
DATE_FORMATS = {"mdy": "%m/%d/%Y", "dmy": "%d/%m/%Y"}
HEADERS = {
    "date": (
        ("transaction date", "trans date", "transdate"),
        ("date", "posted date", "post date", "posting date"),
    ),
    "amount": (("amount", "transaction amount", "trans amount"),),
    "debit": (
        ("debit", "debits", "debit amount", "withdrawal", "withdrawals", "charge", "charges"),
    ),
    "credit": (
        ("credit", "credits", "credit amount", "deposit", "deposits", "payment", "payments"),
    ),
    "direction": (
        ("dr cr", "debit credit", "credit debit", "transaction type", "type", "indicator"),
    ),
    "memo": (
        ("description", "transaction description"),
        ("memo",),
        ("details",),
        ("payee", "merchant", "merchant name", "payee name"),
    ),
    "merchant": (("merchant", "merchant name", "payee name", "payee"),),
    "balance": (
        ("balance", "running balance", "running bal", "ending balance", "account balance"),
    ),
    "category": (("category", "transaction category"),),
}


@dataclass
class Analysis:
    columns: list[str]
    raw_rows: list[tuple[int, list[str]]]
    delimiter: str
    signature: str
    has_header: bool
    mapping: dict
    ambiguities: list[str] = field(default_factory=list)
    rows: list[ImportRow] = field(default_factory=list)
    balances: dict = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)


def decode(data):
    if any(byte < 32 and byte not in (9, 10, 13) for byte in data):
        raise ImportError("This looks like a binary file. Export CSV, OFX or QFX instead.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252")
        except UnicodeDecodeError:
            raise ImportError("This file's text encoding could not be read.") from None


def normalize(value):
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def parse_money(value, decimal="."):
    value = value.strip().replace("\u2212", "-").replace("\u00a0", "")
    suffix = re.search(r"\s*(CR|DR)$", value, re.I)
    direction = suffix.group(1).upper() if suffix else None
    if suffix:
        value = value[: suffix.start()].strip()
    negative = value.startswith("(") and value.endswith(")") or value.endswith("-")
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    if value.endswith("-"):
        value = value[:-1]
    value = value.replace("$", "").replace(" ", "")
    separator = "," if decimal == "." else "."
    # Validate grouping before removing it: malformed values must not become money.
    pattern = (
        rf"[+-]?(?:[0-9]+|[0-9]{{1,3}}(?:{re.escape(separator)}[0-9]{{3}})+)"
        rf"(?:{re.escape(decimal)}[0-9]{{1,2}})?"
    )
    if not re.fullmatch(pattern, value):
        raise ValueError("Unreadable amount")
    amount = Decimal(value.replace(separator, "").replace(decimal, "."))
    if direction:
        amount = abs(amount) * (-1 if direction == "DR" else 1)
    elif negative:
        amount = -abs(amount)
    return _money(amount)


def _date_text(value):
    return re.sub(r"[ T]+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?$", "", value.strip(), flags=re.I)


def parse_date(value, date_format="auto"):
    value = _date_text(value)
    formats = ["%Y-%m-%d", "%Y%m%d", "%d.%m.%Y", "%b %d, %Y", "%B %d, %Y"]
    if date_format in DATE_FORMATS:
        formats.extend([DATE_FORMATS[date_format], DATE_FORMATS[date_format].replace("%Y", "%y")])
        if re.fullmatch(r"\d{1,2}-\d{1,2}-\d{4}", value):
            formats.append(DATE_FORMATS[date_format].replace("/", "-"))
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt).date()
        except ValueError:
            continue
        today = local_today()
        latest = today.replace(
            year=today.year + 1,
            day=min(today.day, monthrange(today.year + 1, today.month)[1]),
        )
        if date(1970, 1, 1) <= parsed <= latest:
            return parsed
        break
    raise ValueError("Unreadable date")


def _is_text(value):
    if not value.strip():
        return False
    if re.fullmatch(r"[\d\s,./$()+\-\u2212]+(?:CR|DR)?", value.strip(), re.I):
        return False
    for fmt in ("auto", "mdy", "dmy"):
        try:
            parse_date(value, fmt)
            return False
        except ValueError:
            pass
    return True


def detect(data):
    text = decode(data)
    candidates = []
    for delimiter in (",", ";", "\t", "|"):
        try:
            reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
            rows = []
            for values in reader:
                if any(v.strip() for v in values):
                    rows.append((reader.line_num, values))
        except csv.Error:
            continue
        widths = Counter(len(row) for _, row in rows[:60] if len(row) > 1)
        if widths:
            width, count = widths.most_common(1)[0]
            candidates.append((count, count / min(len(rows), 60), width, delimiter, rows))
    if not candidates:
        raise ImportError("No table found. Export a CSV with at least two columns.")
    _, _, width, delimiter, rows = max(candidates, key=lambda item: item[:3])

    # A row naming a date and an amount beats shape: a bank's summary block above
    # a short table can be wider or more common than the table itself.
    def trailing_cell_only(index, row):
        return len(row) == width - 1 and all(
            not later[-1].strip() for _, later in rows[index + 1 :] if len(later) == width
        )

    header = next(
        (
            i
            for i, (_, row) in enumerate(rows[:20])
            if _names_table(row) and (len(row) >= width or trailing_cell_only(i, row))
        ),
        None,
    )
    if header is None:
        header = next(
            (
                i
                for i, (_, row) in enumerate(rows[:20])
                if len(row) >= 2
                and (len(row) == width or trailing_cell_only(i, row))
                and sum(map(_is_text, row)) > len(row) / 2
                and not any(
                    _works_date(value, fmt) for value in row for fmt in ("auto", "mdy", "dmy")
                )
            ),
            None,
        )
    columns = (
        [v.strip() for v in rows[header][1]]
        if header is not None
        else [f"Column {i + 1}" for i in range(width)]
    )
    body = rows[header + 1 :] if header is not None else rows
    signature = hashlib.sha256(
        json.dumps([delimiter, list(map(normalize, columns))]).encode("utf-8")
    ).hexdigest()
    return columns, body, delimiter, signature, header is not None


def _names_table(row):
    roles, _ = guess_roles(row)
    return "date" in roles and ("amount" in roles or {"debit", "credit"} <= roles.keys())


def guess_roles(columns):
    names = list(map(normalize, columns))
    roles, ties = {}, []
    for role, priorities in HEADERS.items():
        for aliases in priorities:
            matches = [i for i, name in enumerate(names) if name in aliases]
            if matches:
                if len(matches) == 1:
                    roles[role] = matches[0]
                else:
                    ties.append(role)
                break
    if roles.get("merchant") == roles.get("memo"):
        roles.pop("merchant", None)
    return roles, ties


def _cell(values, roles, role):
    index = roles.get(role)
    return values[index].strip() if index is not None and index < len(values) else ""


def _works_date(value, fmt):
    try:
        parse_date(value, fmt)
        return True
    except ValueError:
        return False


def analyze(data, account=None, *, session=None, mapping=None, confirmed=False):
    columns, raw, delimiter, signature, header = detect(data)
    guessed, ties = guess_roles(columns)
    saved = get_rule(session, "csv", signature) if session is not None else None
    supplied = mapping is not None or saved is not None
    sign_keys = ("liability_amount_sign", "liability_balance_sign")
    answered = {key for key in sign_keys if confirmed and mapping and mapping.get(key)}
    mapping = dict(mapping if mapping is not None else saved.column_map if saved else {})
    for key in sign_keys:
        if mapping.get(key) is None and saved and saved.column_map.get(key):
            mapping[key] = saved.column_map[key]
    roles = dict(mapping.get("roles", guessed))
    issues = []
    # Chase's Type column contains Sale/Payment, not a debit/credit sign indicator.
    if not supplied and "direction" in roles:
        indicators = [_cell(values, roles, "direction") for _, values in raw]
        known = sum(normalize(v) in {"dr", "cr", "debit", "credit"} for v in indicators)
        if known == 0:
            roles.pop("direction")
        elif known * 10 < len(indicators) * 9:
            issues.append("roles")
    if any(
        role not in ROLES or type(i) is not int or not 0 <= i < len(columns)
        for role, i in roles.items()
    ):
        raise ImportError("Choose columns from this file.")
    if len(set(roles.values())) != len(roles):
        issues.append("roles")
    if not header and not confirmed:
        issues.append("header")
    if ties and not supplied:
        issues.append("roles")
    if "date" not in roles or not ("amount" in roles or {"debit", "credit"} <= roles.keys()):
        issues.append("roles")
    if "amount" in roles and ("debit" in roles or "credit" in roles):
        issues.append("roles")
    amounts = [_cell(v, roles, r) for _, v in raw for r in ("amount", "debit", "credit", "balance")]
    decimal = mapping.get("decimal") or (
        "," if any(re.search(r",\d{1,2}(?:\D|$)", a) for a in amounts) else "."
    )
    if decimal not in {".", ","}:
        raise ImportError("Choose a decimal separator.")
    dates = [_date_text(_cell(v, roles, "date")) for _, v in raw if _cell(v, roles, "date")]
    fmt = mapping.get("date_format", "auto")
    if fmt not in {"auto", "mdy", "dmy"}:
        raise ImportError("Choose a date format.")
    slash_dates = [
        v
        for v in dates
        if re.fullmatch(r"\d{1,2}/\d{1,2}/(?:\d{2}|\d{4})|\d{1,2}-\d{1,2}-\d{4}", v)
    ]
    if fmt == "auto" and slash_dates:
        mdy = sum(_works_date(v, "mdy") for v in slash_dates)
        dmy = sum(_works_date(v, "dmy") for v in slash_dates)
        if mdy == dmy:
            issues.append("date_format")
        else:
            fmt = "mdy" if mdy > dmy else "dmy"
    threshold = 5 if confirmed else 9
    if not dates or sum(_works_date(v, fmt) for v in dates) * 10 < len(dates) * threshold:
        issues.append("date_format")
    for role in ("amount", "debit", "credit"):
        if role not in roles:
            continue
        values = [_cell(v, roles, role) for _, v in raw if _cell(v, roles, role)]
        good = 0
        for value in values:
            try:
                parse_money(value, decimal)
                good += 1
            except ValueError:
                pass
        if (role == "amount" and not values) or good * 10 < len(values) * threshold:
            issues.append("amount")
    liability = bool(account and account.is_liability)
    parsed, balances = [], []
    for number, values in raw:
        try:
            if len(values) < len(columns) or any(v.strip() for v in values[len(columns) :]):
                raise ValueError
            day = parse_date(_cell(values, roles, "date"), fmt)
            balance_text = _cell(values, roles, "balance")
            balance_marker = re.search(r"(CR|DR)$", balance_text, re.I)
            balance = parse_money(balance_text, decimal) if balance_text else None
            if liability and balance_marker:
                balance = -balance
            amount_text = _cell(values, roles, "amount")
            marker = re.search(r"(CR|DR)$", amount_text, re.I)
            balance_only = balance is not None and not any(
                _cell(values, roles, r) for r in ("amount", "debit", "credit")
            )
            amount = None
            if balance_only:
                pass
            elif "amount" in roles:
                amount = parse_money(amount_text, decimal)
                if "direction" in roles:
                    direction = normalize(_cell(values, roles, "direction"))
                    if direction not in {"dr", "cr", "debit", "credit"}:
                        raise ValueError
                    amount = abs(amount) * (-1 if direction in {"dr", "debit"} else 1)
            else:
                debit, credit = (_cell(values, roles, r) for r in ("debit", "credit"))
                if not debit and not credit:
                    raise ValueError
                amount = abs(parse_money(credit, decimal)) if credit else Decimal(0)
                amount -= abs(parse_money(debit, decimal)) if debit else Decimal(0)
            parsed.append(
                ImportRow(
                    number,
                    day,
                    amount,
                    *(_cell(values, roles, r) or None for r in ("memo", "merchant", "category")),
                    amount_marker=marker[1].upper() if marker else None,
                    balance_only=balance_only,
                )
            )
            if balance is not None:
                balances.append((day, balance, bool(balance_marker)))
        except (ValueError, InvalidOperation):
            parsed.append(ImportRow(number))
    valid = [row for row in parsed if row.amount is not None]
    markers = {row.amount_marker for row in valid if row.amount_marker}
    directional = "direction" in roles or bool(markers)
    if (
        "direction" not in roles
        and "CR" in markers
        and not any(row.amount < 0 and not row.amount_marker for row in valid)
    ):
        for row in valid:
            if row.amount > 0 and not row.amount_marker:
                row.amount = -row.amount
    balance_only_count = sum(row.balance_only for row in parsed)
    if not (valid or balance_only_count) or (
        not confirmed and len(valid) * 10 < (len(raw) - balance_only_count) * 9
    ):
        issues.append("rows")
    amount_sign = mapping.get("liability_amount_sign")
    balance_sign = mapping.get("liability_balance_sign")
    for setting in (amount_sign, balance_sign):
        if setting not in {None, "positive", "negative"}:
            raise ImportError("Choose positive or negative for the sign.")
    if liability and "amount" in roles and not directional:
        nonzero = [row.amount for row in valid if row.amount]
        positives = sum(a > 0 for a in nonzero)
        evidence = None
        if account.type == "cards":
            if len(nonzero) >= 5 and positives * 10 >= len(nonzero) * 7:
                evidence = "positive"
            elif len(nonzero) >= 5 and (len(nonzero) - positives) * 10 >= len(nonzero) * 7:
                evidence = "negative"
        if (
            amount_sign
            and evidence
            and amount_sign != evidence
            and "liability_amount_sign" not in answered
        ):
            issues.append("liability_amount_sign")
            amount_sign = evidence
        amount_sign = amount_sign or evidence
        if amount_sign is None:
            issues.append("liability_amount_sign")
        if amount_sign == "positive":
            for row in valid:
                row.amount = -row.amount
    if liability and balances:
        unmarked = [b for _, b, marked in balances if not marked]
        evidence = None
        if unmarked and all(b >= 0 for b in unmarked):
            evidence = "positive"
        elif len(unmarked) >= 5 and all(b < 0 for b in unmarked):
            evidence = "negative"
        if (
            balance_sign
            and evidence
            and balance_sign != evidence
            and "liability_balance_sign" not in answered
        ):
            issues.append("liability_balance_sign")
            balance_sign = evidence
        balance_sign = balance_sign or evidence
        if unmarked and balance_sign is None:
            issues.append("liability_balance_sign")
        if balance_sign == "negative":
            balances = [(day, b if marked else -b, marked) for day, b, marked in balances]
    if (
        not liability
        and "amount" in roles
        and not directional
        and len(valid) >= 3
        and all(row.amount > 0 for row in valid)
        and mapping.get("amounts_signed") is not True
    ):
        issues.append("unsigned_amounts")
    # Preserve the first value of each day in a descending export, otherwise the last.
    dated = [row.date for row in parsed if row.date is not None]
    newest_first = len(dated) > 1 and dated[0] > dated[-1]
    pairs = [(day, balance) for day, balance, _ in balances]
    daily = dict(reversed(pairs)) if newest_first else dict(pairs)
    notices = []
    if len(set(dated)) == 1 and len({b for _, b in pairs}) > 1:
        daily = {}
        notices.append(
            "No closing balance recorded: every dated row is on the same day and "
            "the file has different balances, so Orbit can't tell which balance closed the day."
        )
    # Only retain sign answers actually supplied by a person or a saved rule.
    mapping = dict(
        mapping,
        roles=roles,
        date_format=fmt,
        decimal=decimal,
    )
    for key in sign_keys:
        if mapping.get(key) is None:
            mapping.pop(key, None)
    return Analysis(
        columns,
        raw,
        delimiter,
        signature,
        header,
        mapping,
        list(dict.fromkeys(issues)),
        parsed,
        daily,
        notices,
    )


def import_csv(session, account, data, *, mapping=None, confirmed=False):
    analysis = analyze(data, account, session=session, mapping=mapping, confirmed=confirmed)
    if analysis.ambiguities:
        raise ImportError("Review the column mapping, dates and signs before importing.")
    with write_savepoint(session):
        result = write_import(session, account, analysis.rows, analysis.balances, "csv")
        result.notices.extend(analysis.notices)
        if not analysis.balances and not analysis.notices:
            result.notices.append("No balance recorded: this file has no readable balance values.")
        if confirmed:
            save_rule(session, "csv", analysis.signature, analysis.mapping)
    return result
