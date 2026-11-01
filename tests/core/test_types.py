import pytest
from sqlalchemy import select

from core.models import TypeOverride
from core.types import (
    ACCOUNT_TYPES,
    LIABILITY_TYPES,
    OTHER,
    OUR_TYPES,
    add_override,
    builtin_type,
    infer_type,
    is_liability_type,
)

PLAID_CASES = (
    [
        ("depository", subtype, "cash")
        for subtype in (
            "checking",
            "savings",
            "cd",
            "money market",
            "cash management",
            "paypal",
            "prepaid",
            "ebt",
        )
    ]
    + [
        ("depository", "hsa", "health"),
        ("credit", "credit card", "cards"),
        ("credit", "paypal", "cards"),
        ("credit", "new subtype", "cards"),
        ("credit", None, "cards"),
    ]
    + [
        ("loan", subtype, "mortgage")
        for subtype in (
            "mortgage",
            "home equity",
            "line of credit secured by a home",
        )
    ]
    + [
        ("loan", subtype, "loans")
        for subtype in (
            "auto",
            "student",
            "consumer",
            "business",
            "commercial",
            "construction",
            "overdraft",
            "line of credit",
            "loan",
        )
    ]
    + [
        ("investment", subtype, "retire")
        for subtype in (
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
        )
    ]
    + [("investment", subtype, "health") for subtype in ("hsa", "health reimbursement arrangement")]
    + [
        ("investment", subtype, "insure")
        for subtype in (
            "life insurance",
            "other insurance",
            "fixed annuity",
            "variable annuity",
            "other annuity",
        )
    ]
    + [("investment", subtype, "crypto") for subtype in ("crypto exchange", "non-custodial wallet")]
    + [
        ("investment", subtype, "invest")
        for subtype in (
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
        )
    ]
)


def test_type_constants_and_liability_types():
    assert OUR_TYPES == (
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
    assert OTHER == "other"
    assert ACCOUNT_TYPES == OUR_TYPES + (OTHER,)
    assert LIABILITY_TYPES == {"cards", "loans", "mortgage"}
    for account_type in ACCOUNT_TYPES:
        assert is_liability_type(account_type) == (account_type in LIABILITY_TYPES)
    assert not is_liability_type(None)
    assert not is_liability_type("unknown")


@pytest.mark.parametrize("account_type,subtype,expected", PLAID_CASES)
def test_every_plaid_group_and_structured_rules_beat_names(account_type, subtype, expected):
    # Conflicting keywords prove that these are exact rules, not bare fallbacks.
    name = "Savings" if expected != "cash" else "Mortgage"
    result = builtin_type("plaid", account_type, subtype, name)
    assert result == expected
    assert result in ACCOUNT_TYPES


@pytest.mark.parametrize(
    "account_type,expected",
    [
        ("depository", "cash"),
        ("credit", "cards"),
        ("loan", "loans"),
        ("investment", "invest"),
    ],
)
def test_plaid_bare_type_fallbacks(account_type, expected):
    assert builtin_type("plaid", account_type, "unrecognized", "Example") == expected


@pytest.mark.parametrize(
    "account_type,expected",
    [
        ("checking", "cash"),
        ("savings", "cash"),
        ("moneymrkt", "cash"),
        ("creditcard", "cards"),
        ("creditline", "cards"),
        ("investment", "invest"),
    ],
)
def test_ofx_type_rules(account_type, expected):
    result = builtin_type("ofx", account_type, None, "Mortgage")
    assert result == expected
    assert result in ACCOUNT_TYPES


NAME_CASES = (
    [(name, "mortgage") for name in ("Mortgage", "HELOC", "Home Equity")]
    + [
        ("Auto Loan", "loans"),
    ]
    + [
        (name, "cards")
        for name in ("Credit Card", "Card", "Visa", "Mastercard", "Amex", "Home Depot Card")
    ]
    + [
        (name, "retire")
        for name in ("401k", "401(k)", "403b", "IRA", "Roth", "Pension", "Retirement")
    ]
    + [(name, "health") for name in ("HSA", "Health Savings")]
    + [
        (name, "insure")
        for name in ("Life Insurance", "Whole Life", "Term Life", "Annuity", "Insurance")
    ]
    + [(name, "crypto") for name in ("Bitcoin", "BTC", "Ethereum", "Crypto", "Coinbase")]
    + [(name, "invest") for name in ("Brokerage", "Investment", "Stocks", "ETF")]
    + [
        (name, "property")
        for name in ("House", "Home", "Condo", "Property", "Real Estate", "Vehicle", "Car", "Truck")
    ]
    + [
        (name, "cash")
        for name in (
            "Checking",
            "Savings",
            "Money Market",
            "Cash",
            "Mirage Savings",
            "Credit Union Checking",
            "Home Savings Bank Checking",
            "House Fund Savings",
        )
    ]
)


@pytest.mark.parametrize("name,expected", NAME_CASES)
def test_name_keywords(name, expected):
    result = builtin_type("manual", None, None, "My " + name + " account")
    assert result == expected
    assert result in ACCOUNT_TYPES


@pytest.mark.parametrize(
    "name",
    [
        "Mirage",
        "Cardinal",
        "Carpet",
        "Cashmere",
        "Homestead",
        "Loaner",
        "Stocksworth",
        "401(k)extra",
    ],
)
def test_keywords_are_whole_words(name):
    assert builtin_type(None, None, None, name) == OTHER


@pytest.mark.parametrize("aggregator", ["csv", "manual", "simplefin", "unknown", None])
def test_other_aggregators_have_no_structured_rules(aggregator):
    assert builtin_type(aggregator, "investment", "ira", "Example") == OTHER
    assert builtin_type(aggregator, "investment", "ira", "Checking") == "cash"


@pytest.mark.parametrize(
    "args,expected",
    [
        (("plaid", "investment", "other", "Coinbase"), "crypto"),
        (("plaid", "loan", "unknown", "Home Equity"), "mortgage"),
        (("plaid", "loan", "other", "Honda Car"), "loans"),
        (("plaid", "depository", "other", "Amex Savings"), "cash"),
        (("plaid", "investment", "other", "Mortgage REIT Fund"), "invest"),
        (("plaid", "investment", "other", "Mortgage Coinbase"), "crypto"),
        (("csv", None, None, "Chase Debit Card"), "cash"),
        (("csv", None, None, "Chase Debit"), "cash"),
        (("plaid", "investment", "hsa", "Retirement"), "health"),
        ((" PLAID ", " INVESTMENT ", " ROTH_401K ", "House"), "retire"),
        (("plaid", "investment", "non_custodial-wallet", "Savings"), "crypto"),
        (("manual", None, None, "  HEALTH_SAVINGS  "), "health"),
        (("OFX", "CREDITCARD", None, None), "cards"),
        (("ofx", "unknown", None, "Brokerage"), "invest"),
        ((None, None, None, None), "other"),
        (("unknown", "unknown", "unknown", "Example"), "other"),
        (("plaid", "unknown", None, None), "other"),
        (("manual", None, None, "Mortgage Loan Credit Card"), "mortgage"),
        (("manual", None, None, "Loan Credit Card"), "loans"),
        (("manual", None, None, "Roth HSA"), "retire"),
        (("manual", None, None, "Insurance Crypto"), "insure"),
    ],
)
def test_priority_normalization_and_unknown(args, expected):
    result = builtin_type(*args)
    assert result == expected
    assert result in ACCOUNT_TYPES


@pytest.mark.parametrize("account_type", ["loan", "depository", "investment"])
@pytest.mark.parametrize(
    "name",
    ["Honda Car", "Amex Savings", "Mortgage REIT Fund", "Student Loan Fund", "Visa Checking"],
)
def test_plaid_name_fallback_respects_liability_type(account_type, name):
    result = builtin_type("plaid", account_type, "other", name)
    assert (result in LIABILITY_TYPES) == (account_type == "loan")


def test_infer_falls_back_to_builtin(session):
    assert infer_type("plaid", "depository", "checking", "Mortgage", session=session) == "cash"
    assert infer_type(None, None, None, None, session=session) == OTHER


def test_override_beats_builtin_and_normalizes_matchers(session):
    override_id = add_override(
        "property",
        aggregator=" PLAID ",
        account_type="INVESTMENT",
        subtype="NON_CUSTODIAL-WALLET",
        name_contains=" MY_WALLET ",
        session=session,
    )
    assert isinstance(override_id, int)
    assert session.get(TypeOverride, override_id).created_at is not None
    assert (
        infer_type(
            "plaid", "investment", "non-custodial wallet", "The My-Wallet Account", session=session
        )
        == "property"
    )
    assert (
        infer_type(
            "plaid", "investment", "non-custodial wallet", "Different wallet", session=session
        )
        == "crypto"
    )


def test_most_specific_override_wins_even_when_older(session):
    add_override(
        "health", aggregator="plaid", account_type="investment", subtype="ira", session=session
    )
    add_override("retire", aggregator="plaid", account_type="investment", session=session)
    add_override("cash", aggregator="plaid", session=session)
    assert infer_type("plaid", "investment", "ira", "Example", session=session) == "health"
    assert infer_type("plaid", "investment", "brokerage", "Example", session=session) == "retire"
    assert infer_type("plaid", "depository", "checking", "Example", session=session) == "cash"


def test_override_tie_goes_to_highest_id_and_substring_match(session):
    first = add_override("property", name_contains="mir", session=session)
    second = add_override("invest", name_contains="MIR", session=session)
    assert second > first
    assert infer_type("manual", None, None, "Mirage Savings", session=session) == "invest"


@pytest.mark.parametrize("mismatch", ["aggregator", "account_type", "subtype", "name"])
def test_every_non_null_override_matcher_is_required(session, mismatch):
    add_override(
        "property",
        aggregator="plaid",
        account_type="investment",
        subtype="ira",
        name_contains="special",
        session=session,
    )
    values = dict(aggregator="plaid", account_type="investment", subtype="ira", name="Special")
    values[mismatch] = "different"
    assert infer_type(**values, session=session) == builtin_type(**values)


@pytest.mark.parametrize(
    "mapped_type,matchers",
    [
        ("unknown", {"aggregator": "plaid"}),
        (None, {"name_contains": "test"}),
        ("cash", {}),
        (
            "cash",
            {"aggregator": None, "account_type": None, "subtype": None, "name_contains": None},
        ),
        ("cash", {"name_contains": ""}),
        ("cash", {"name_contains": "   "}),
        ("cash", {"aggregator": "plaid", "name_contains": "_"}),
    ],
)
def test_add_override_validation(session, mapped_type, matchers):
    with pytest.raises(ValueError):
        add_override(mapped_type, **matchers, session=session)
    assert list(session.scalars(select(TypeOverride))) == []


def test_override_supplied_session_never_commits(session):
    override_id = add_override("cash", name_contains="test", session=session)
    assert infer_type(None, None, None, "test", session=session) == "cash"
    session.rollback()
    assert session.get(TypeOverride, override_id) is None
    assert infer_type(None, None, None, "test", session=session) == OTHER


def test_override_without_session_commits(database):
    override_id = add_override("crypto", name_contains="special")
    assert override_id > 0
    assert infer_type(None, None, None, "Special Account") == "crypto"
