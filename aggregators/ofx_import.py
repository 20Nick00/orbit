"""OFX/QFX statements, with local dates and explicit liability balance meanings."""

import hashlib
import io
import re
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ofxparse import OfxParser
from ofxparse.ofxparse import AccountType
from sqlalchemy import delete, select

from aggregators.file_store import (
    ImportError,
    ImportRow,
    get_rule,
    protected_balance_days,
    save_rule,
    valid_target,
    write_import,
)
from core.db import write_savepoint
from core.models import Account, AccountFact, Holding, Institution, _money
from core.types import infer_type, is_liability_type


class _Parser(OfxParser):
    """ofxparse defaults absent position values and transaction totals to zero."""

    @classmethod
    def parseTransaction(cls, tag):
        transaction = super().parseTransaction(tag)
        amount = tag.find("trnamt")
        if amount is not None and amount.get_text().strip().lower() in {"null", "-null"}:
            transaction.amount = None
        return transaction

    @classmethod
    def parseInvestmentPosition(cls, tag):
        position = super().parseInvestmentPosition(tag)
        if tag.find("mktval") is None:
            position.market_value = None
        if tag.find("units") is None:
            position.units = None
        return position

    @classmethod
    def parseInvestmentTransaction(cls, tag):
        transaction = super().parseInvestmentTransaction(tag)
        if tag.find("total") is None:
            transaction.total = None
        return transaction

    @classmethod
    def parseInvestmentStatement(cls, tag):
        statement = super().parseInvestmentStatement(tag)
        asof = tag.find("dtasof")
        statement.asof = cls.parseOfxDateTime(asof.get_text().strip()) if asof else None
        positions = tag.find("invposlist")
        statement.has_positions = positions is not None
        statement.positions_complete = positions is not None and len(
            positions.find_all(recursive=False)
        ) == len(statement.positions)
        return statement


@dataclass
class Statement:
    import_key: str
    institution_name: str
    fid: str
    mask: str | None
    name: str
    type: str
    currency: str
    rows: list[ImportRow]
    balance: Decimal | None
    balance_date: date | None
    holdings: list[dict] | None = None
    holding_date: date | None = None
    notices: list[str] = field(default_factory=list)
    account_id: int | None = None
    balance_sign: str | None = None
    needs_target: bool = False
    needs_sign: bool = False


def _day(value):
    return (
        value.date() if isinstance(value, datetime) else value if isinstance(value, date) else None
    )


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse(data, *, session):
    # Keep the date printed by the bank, instead of shifting it through UTC.
    data = re.sub(rb"(<DT[A-Z0-9]+>\s*\d+(?:\.\d+)?)\[[^\]]*\]", rb"\1", data, flags=re.I)
    try:
        if re.search(
            rb"<(?:TRNAMT|BALAMT|MKTVAL|TOTAL|AVAILCASH|UNITS)>"
            rb"\s*[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*(?=<|$)",
            data,
            re.I,
        ):
            raise ValueError
        # ofxparse assumes ASCII for XML files without legacy OFX headers.
        if data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
            declaration = re.search(rb"<\?xml[^>]*encoding=[\"']([^\"']+)", data, re.I)
            encoding = declaration[1].decode("ascii") if declaration else "utf-8-sig"
            data = b"OFXHEADER:200\nENCODING:UTF-8\n\n" + data.decode(encoding).encode("utf-8")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = _Parser.parse(io.BytesIO(data), fail_fast=True)
        if not parsed.accounts:
            raise ValueError
        securities = {s.uniqueid: s for s in getattr(parsed, "security_list", None) or []}
        result, seen = [], set()
        for account in parsed.accounts:
            if not account.account_id or account.statement is None:
                raise ValueError
            statement = account.statement
            organization = (getattr(account.institution, "organization", "") or "").strip()
            fid = getattr(account.institution, "fid", "") or ""
            import_key = _hash(f"{fid or organization or 'OFX import'}|{account.account_id}")
            if import_key in seen:
                raise ValueError
            seen.add(import_key)
            mask = account.account_id[-4:] if len(account.account_id) > 4 else None
            name = organization or "OFX import"
            if mask:
                name = f"{name} · {mask}"
            kind = {AccountType.CreditCard: "creditcard", AccountType.Investment: "investment"}.get(
                account.type, account.account_type.lower()
            )
            type_key = infer_type("ofx", kind, None, name, session=session)
            currency = (getattr(statement, "currency", None) or account.curdef or "").upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError
            rows = []
            for number, tx in enumerate(statement.transactions, 1):
                investment = hasattr(tx, "tradeDate")
                total = tx.total if investment else tx.amount
                if investment and total is None:
                    continue
                day = _day(tx.tradeDate if investment else tx.date)
                parts = list(
                    dict.fromkeys(
                        p.strip() for p in (getattr(tx, "payee", ""), tx.memo) if p and p.strip()
                    )
                )
                rows.append(
                    ImportRow(
                        number,
                        day,
                        _money(total) if total is not None else None,
                        " / ".join(parts),
                        category="Transfer" if tx.type.upper() == "XFER" else "",
                    )
                )
            balance = getattr(statement, "balance", None)
            balance_date = _day(getattr(statement, "balance_date", None)) or _day(
                getattr(statement, "end_date", None)
            )
            holdings, holding_date, notices = None, None, []
            if account.type == AccountType.Investment:
                holding_date = _day(statement.asof) or _day(getattr(statement, "end_date", None))
                balance_date = holding_date
                if statement.has_positions:
                    holdings = []
                    for position in statement.positions:
                        security = securities.get(position.security)
                        holdings.append(
                            dict(
                                ticker=security.ticker if security else None,
                                name=security.name if security else None,
                                qty=position.units,
                                value=position.market_value,
                                cost_basis=None,
                            )
                        )
                cash = getattr(statement, "available_cash", None)
                if (
                    holdings is not None
                    and statement.positions_complete
                    and all(p["value"] is not None for p in holdings)
                    and cash is not None
                ):
                    balance = sum((p["value"] for p in holdings), Decimal(0)) + cash
                else:
                    balance = None
                    notices.append(
                        "No investment balance recorded: a position value, position list "
                        "or available cash is missing."
                    )
                if holding_date is None:
                    notices.append("No holdings recorded: the statement date is missing.")
            if balance_date is None:
                notices.append(
                    "No balance recorded: the balance date and statement end date are missing."
                )
            elif balance is None:
                notices.append("No balance recorded: the statement has no complete balance.")
            result.append(
                Statement(
                    import_key,
                    organization,
                    fid,
                    mask,
                    name,
                    type_key,
                    currency,
                    rows,
                    _money(balance) if balance is not None else None,
                    balance_date,
                    holdings,
                    holding_date,
                    notices,
                )
            )
        return result
    except Exception:
        # Parser exceptions can contain entire statements and account numbers.
        raise ImportError(
            "This OFX/QFX file could not be read. Try a fresh export from your institution."
        ) from None


def analyze(data, *, session, choices=None):
    choices = choices or {}
    statements = parse(data, session=session)
    targets, questions = set(), set()
    institution_signs = {}
    for index, statement in enumerate(statements):
        institution_key = statement.fid or statement.institution_name
        sign = choices.get(str(index), {}).get("liability_balance_sign")
        if sign and institution_key:
            if institution_key in institution_signs and institution_signs[institution_key] != sign:
                raise ImportError("Choose one balance meaning for each institution.")
            institution_signs[institution_key] = sign
    for index, statement in enumerate(statements):
        institution_key = statement.fid or statement.institution_name
        choice = choices.get(str(index), {})
        known = session.scalar(
            select(AccountFact).where(
                AccountFact.key == "import_key", AccountFact.value == statement.import_key
            )
        )
        target = known.account_id if known else choice.get("account_id")
        statement.needs_target = not known and target is None
        if target not in (None, "new"):
            try:
                account = session.get(Account, int(target))
            except (ValueError, TypeError):
                account = None
            if account is None or not valid_target(session, account):
                raise ImportError(
                    "Choose an account that is neither manual nor linked through Plaid."
                )
            existing = session.get(AccountFact, (account.id, "import_key"))
            if existing and existing.value != statement.import_key:
                raise ImportError("That account already belongs to a different OFX account.")
            if account.currency != statement.currency:
                raise ImportError("The selected account uses a different currency.")
            if account.id in targets:
                raise ImportError("Choose a separate destination for each account in the file.")
            targets.add(account.id)
            statement.account_id = account.id
            liability = account.is_liability
        else:
            liability = is_liability_type(statement.type)
        rule = get_rule(session, "ofx", _hash(institution_key)) if institution_key else None
        sign = (
            choice.get("liability_balance_sign")
            or institution_signs.get(institution_key)
            or (rule.column_map.get("liability_balance_sign") if rule else None)
        )
        if sign not in (None, "positive", "negative"):
            raise ImportError("Choose whether this balance is owed or a credit.")
        statement.balance_sign = sign
        statement.needs_sign = bool(
            liability
            and statement.balance is not None
            and statement.balance_date is not None
            and statement.balance > 0
            and sign is None
            and (not institution_key or institution_key not in questions)
        )
        if statement.needs_sign:
            questions.add(institution_key)
    return statements


def import_ofx(session, data, *, choices=None):
    statements = analyze(data, session=session, choices=choices)
    if any(s.needs_target or s.needs_sign for s in statements):
        raise ImportError("Choose a destination and confirm any card balance before importing.")
    results = []
    with write_savepoint(session):
        for statement in statements:
            account = session.get(Account, statement.account_id) if statement.account_id else None
            if account is None:
                names = {name.strip().casefold() for name in session.scalars(select(Account.name))}
                name, suffix = statement.name, 2
                while name.casefold() in names:
                    name = f"{statement.name} ({suffix})"
                    suffix += 1
                institution_id = statement.fid or statement.institution_name or "OFX import"
                institution = session.scalar(
                    select(Institution).where(
                        Institution.aggregator == "ofx", Institution.external_id == institution_id
                    )
                )
                if institution is None:
                    institution = Institution(
                        name=statement.institution_name or "OFX import",
                        aggregator="ofx",
                        external_id=institution_id,
                    )
                    session.add(institution)
                    session.flush()
                account = Account(
                    institution_id=institution.id,
                    name=name,
                    type=statement.type,
                    currency=statement.currency,
                    mask=statement.mask,
                    is_liability=is_liability_type(statement.type),
                )
                session.add(account)
                session.flush()
            account.mask = statement.mask
            session.merge(
                AccountFact(account_id=account.id, key="import_key", value=statement.import_key)
            )
            balances = {}
            if statement.balance is not None and statement.balance_date is not None:
                balance = statement.balance
                if account.is_liability and statement.balance_sign != "positive":
                    balance = -balance
                balances[statement.balance_date] = balance
            holdings_protected = statement.holding_date is not None and protected_balance_days(
                session, account.id, [statement.holding_date]
            )
            result = write_import(session, account, statement.rows, balances, "ofx")
            result.notices.extend(statement.notices)
            if (
                statement.holdings is not None
                and statement.holding_date is not None
                and not holdings_protected
            ):
                session.execute(
                    delete(Holding).where(
                        Holding.account_id == account.id, Holding.date == statement.holding_date
                    )
                )
                session.add_all(
                    Holding(account_id=account.id, date=statement.holding_date, **p)
                    for p in statement.holdings
                )
            if (
                account.is_liability
                and (statement.fid or statement.institution_name)
                and statement.balance_sign
                and statement.balance is not None
                and statement.balance > 0
            ):
                save_rule(
                    session,
                    "ofx",
                    _hash(statement.fid or statement.institution_name),
                    {"liability_balance_sign": statement.balance_sign},
                )
            results.append(result)
        session.flush()
    return results
