"""Allowlisted liability facts; private loan details never enter the database."""

from decimal import Decimal

FIELDS = {
    "credit": {
        "minimum payment": "minimum_payment_amount",
        "statement balance": "last_statement_balance",
        "due": "next_payment_due_date",
    },
    "mortgage": {
        "payment": "next_monthly_payment",
        "original amount": "origination_principal_amount",
        "principal ytd": "ytd_principal_paid",
        "interest ytd": "ytd_interest_paid",
        "due": "next_payment_due_date",
        "payoff": "maturity_date",
    },
    "student": {
        "minimum payment": "minimum_payment_amount",
        "original amount": "origination_principal_amount",
        "due": "next_payment_due_date",
        "payoff": "expected_payoff_date",
    },
}


def facts(kind, row):
    result = {}
    for key, field in FIELDS[kind].items():
        value = row.get(field)
        if value is not None:
            result[key] = (
                str(value) if key in {"due", "payoff"} else format(Decimal(str(value)), "f")
            )
    rate = None
    if kind == "credit":
        aprs = row.get("aprs") or []
        purchase = next((apr for apr in aprs if apr.get("apr_type") == "purchase_apr"), None)
        rate = (purchase or (aprs[0] if aprs else {})).get("apr_percentage")
    elif kind == "mortgage":
        rate = (row.get("interest_rate") or {}).get("percentage")
    else:
        rate = row.get("interest_rate_percentage")
    if rate is not None:
        result["rate"] = format(Decimal(str(rate)), "f") + "%"
    return result
