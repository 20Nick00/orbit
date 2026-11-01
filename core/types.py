"""Conservative account classification, with user overrides first."""

import re
from contextlib import nullcontext

from sqlalchemy import select

from core.db import session_scope

OUR_TYPES = (
    "cash",
    "invest",
    "retire",
    "health",
    "insure",
    "crypto",
    "property",
    "cards",
    "loans",
    "mortgage",
)
OTHER = "other"
ACCOUNT_TYPES = OUR_TYPES + (OTHER,)
LIABILITY_TYPES = {"cards", "loans", "mortgage"}


def is_liability_type(t):
    return t in LIABILITY_TYPES


def _normalize(value):
    return " ".join((value or "").lower().replace("_", " ").replace("-", " ").split())


_PLAID_GROUPS = {
    ("depository", "cash"): (
        "checking",
        "savings",
        "cd",
        "money market",
        "cash management",
        "paypal",
        "prepaid",
        "ebt",
    ),
    ("depository", "health"): ("hsa",),
    ("credit", "cards"): ("credit card", "paypal", "all"),
    ("loan", "mortgage"): (
        "mortgage",
        "home equity",
        "line of credit secured by a home",
    ),
    ("loan", "loans"): (
        "auto",
        "student",
        "consumer",
        "business",
        "commercial",
        "construction",
        "overdraft",
        "line of credit",
        "loan",
    ),
    ("investment", "retire"): (
        "401a",
        "401k",
        "403b",
        "457b",
        "ira",
        "roth",
        "roth 401k",
        "sep ira",
        "simple ira",
        "sarsep",
        "keogh",
        "pension",
        "profit sharing plan",
        "retirement",
        "stock plan",
        "thrift savings plan",
        "rrsp",
        "rrif",
        "lira",
        "lif",
        "sipp",
        "lrif",
        "lrsp",
        "prif",
        "rlif",
        "rlsp",
    ),
    ("investment", "health"): ("hsa", "health reimbursement arrangement"),
    ("investment", "insure"): (
        "life insurance",
        "other insurance",
        "fixed annuity",
        "variable annuity",
        "other annuity",
    ),
    ("investment", "crypto"): ("crypto exchange", "non-custodial wallet"),
    ("investment", "invest"): (
        "brokerage",
        "529",
        "mutual fund",
        "ugma",
        "utma",
        "trust",
        "tfsa",
        "isa",
        "non-taxable brokerage account",
        "education savings account",
        "gic",
        "lisa",
        "resp",
        "rdsp",
    ),
}
_PLAID_RULES = {
    (account_type, _normalize(subtype)): mapped_type
    for (account_type, mapped_type), subtypes in _PLAID_GROUPS.items()
    for subtype in subtypes
}
_PLAID_FALLBACK = {
    "depository": "cash",
    "credit": "cards",
    "loan": "loans",
    "investment": "invest",
}
_OFX_RULES = {
    "checking": "cash",
    "savings": "cash",
    "moneymrkt": "cash",
    "creditcard": "cards",
    "creditline": "cards",
    "investment": "invest",
}
_KEYWORDS = (
    ("mortgage", ("mortgage", "heloc", "home equity")),
    ("loans", ("loan",)),
    ("cash", ("debit card", "debit")),
    ("cards", ("credit card", "card", "visa", "mastercard", "amex")),
    ("retire", ("401k", "401(k)", "403b", "ira", "roth", "pension", "retirement")),
    ("health", ("hsa", "health savings")),
    ("insure", ("life insurance", "whole life", "term life", "annuity", "insurance")),
    ("crypto", ("bitcoin", "btc", "ethereum", "crypto", "coinbase")),
    ("invest", ("brokerage", "investment", "stocks", "etf")),
    # Cash comes before property: "Home Savings Bank Checking" is a bank account.
    ("cash", ("checking", "savings", "money market", "cash")),
    ("property", ("house", "home", "condo", "property", "real estate", "vehicle", "car", "truck")),
)
_NAME_RULES = tuple(
    (mapped_type, re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, words)) + r")(?!\w)"))
    for mapped_type, words in _KEYWORDS
)


def builtin_type(aggregator, account_type, subtype, name):
    aggregator, account_type, subtype, name = map(
        _normalize, (aggregator, account_type, subtype, name)
    )
    if aggregator == "plaid":
        # Every credit subtype is a card, including newly introduced subtypes.
        exact = "cards" if account_type == "credit" else _PLAID_RULES.get((account_type, subtype))
        if exact:
            return exact
    elif aggregator == "ofx" and account_type in _OFX_RULES:
        return _OFX_RULES[account_type]
    for mapped_type, pattern in _NAME_RULES:
        if aggregator == "plaid":
            if account_type == "loan" and mapped_type not in LIABILITY_TYPES:
                continue
            if account_type in ("depository", "investment") and mapped_type in LIABILITY_TYPES:
                continue
        if pattern.search(name):
            return mapped_type
    if aggregator == "plaid":
        return _PLAID_FALLBACK.get(account_type, OTHER)
    return OTHER


def infer_type(aggregator, account_type, subtype, name, *, session=None):
    from core.models import TypeOverride

    values = dict(
        zip(
            ("aggregator", "account_type", "subtype", "name_contains"),
            map(_normalize, (aggregator, account_type, subtype, name)),
            strict=False,
        )
    )
    with nullcontext(session) if session is not None else session_scope() as active:
        matches = []
        for override in active.scalars(select(TypeOverride)):
            matchers = {
                key: getattr(override, key) for key in values if getattr(override, key) is not None
            }
            if all(
                _normalize(value) in values[key]
                if key == "name_contains"
                else _normalize(value) == values[key]
                for key, value in matchers.items()
            ):
                matches.append((len(matchers), override.id, override.mapped_type))
        active.flush()
        if matches:
            return max(matches)[2]
    return builtin_type(aggregator, account_type, subtype, name)


def add_override(
    mapped_type,
    *,
    aggregator=None,
    account_type=None,
    subtype=None,
    name_contains=None,
    session=None,
):
    from core.models import TypeOverride

    if mapped_type not in ACCOUNT_TYPES:
        raise ValueError(f"Unknown account type: {mapped_type!r}")
    matchers = dict(
        aggregator=aggregator,
        account_type=account_type,
        subtype=subtype,
        name_contains=name_contains,
    )
    given = [value for value in matchers.values() if value is not None]
    # A blank name_contains would match every account.
    if not given or not all(map(_normalize, given)):
        raise ValueError("At least one override matcher is required, and none may be blank")
    with nullcontext(session) if session is not None else session_scope() as active:
        override = TypeOverride(mapped_type=mapped_type, **matchers)
        active.add(override)
        active.flush()
        return override.id
