import datetime as dt
from copy import deepcopy

from aggregators.plaid import PlaidError


def account(account_id="a1", **values):
    return {
        "account_id": account_id,
        "name": "Checking",
        "mask": "1234",
        "type": "depository",
        "subtype": "checking",
        "balances": {"current": 123.45, "limit": None, "iso_currency_code": "USD"},
        **values,
    }


def transaction(transaction_id="t1", **values):
    return {
        "account_id": "a1",
        "transaction_id": transaction_id,
        "date": "2026-10-01",
        "amount": 0.1 + 0.2,
        "name": "Coffee",
        "merchant_name": None,
        "pending": False,
        "personal_finance_category": {
            "primary": "FOOD_AND_DRINK",
            "detailed": "FOOD_AND_DRINK_COFFEE",
        },
        **values,
    }


def page(added=(), modified=(), removed=(), next_cursor="cursor-1", has_more=False):
    return {
        "added": list(added),
        "modified": list(modified),
        "removed": list(removed),
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


class FakePlaidClient:
    def __init__(self):
        self.accounts = [account()]
        self.pages = [page()]
        self.calls = []
        self.errors = {}
        self.exchange = {"item_id": "remote-item", "access_token": "private-access-token"}
        self.link_data = {"link_sessions": []}
        self.holdings = {"accounts": [], "holdings": [], "securities": []}
        self.liabilities = {"liabilities": {"credit": [], "mortgage": [], "student": []}}
        self.keys = {}

    def _call(self, name, *args):
        self.calls.append((name, args))
        error = self.errors.get(name)
        if error:
            raise PlaidError(error, "ITEM_ERROR", "safe-request")

    def create_link_token(self, **values):
        self._call("create_link_token", values)
        return {
            "link_token": "private-link-token",
            "hosted_link_url": "https://secure.plaid.com/link/example",
            "expiration": dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        }

    def get_link_token(self, token):
        self._call("get_link_token", token)
        return deepcopy(self.link_data)

    def exchange_public_token(self, token):
        self._call("exchange_public_token", token)
        return dict(self.exchange)

    def get_item(self, token):
        self._call("get_item", token)
        return {"item": {"item_id": self.exchange["item_id"], "institution_id": "ins_example"}}

    def get_institution(self, institution_id):
        self._call("get_institution", institution_id)
        return {"institution": {"institution_id": institution_id, "name": "Example Bank"}}

    def get_accounts(self, token):
        self._call("get_accounts", token)
        return {"accounts": deepcopy(self.accounts)}

    def sync_transactions(self, token, cursor=None, count=500):
        self._call("sync_transactions", token, cursor, count)
        result = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    def get_holdings(self, token):
        self._call("get_holdings", token)
        return deepcopy(self.holdings)

    def get_liabilities(self, token):
        self._call("get_liabilities", token)
        return deepcopy(self.liabilities)

    def remove_item(self, token):
        self._call("remove_item", token)
        return {"request_id": "safe-request"}

    def get_webhook_key(self, kid):
        self._call("get_webhook_key", kid)
        if kid not in self.keys:
            raise PlaidError("INVALID_KEY_ID")
        return {"key": self.keys[kid]}
