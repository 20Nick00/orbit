"""Plaid SDK boundary and Orbit's account data adapter."""

import datetime as dt
import json
import re
from contextlib import nullcontext
from decimal import Decimal
from importlib import import_module

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert

from aggregators.base import AccountBalance
from core.clock import local_today
from core.config import get_settings
from core.crypto import decrypt
from core.db import session_scope
from core.models import Account, AccountFact, Holding, PlaidAccount, PlaidItem, Transaction, _money
from core.snapshots import record_daily
from core.types import infer_type, is_liability_type

OPTIONAL_UNAVAILABLE = {
    "PRODUCTS_NOT_SUPPORTED",
    "NO_INVESTMENT_ACCOUNTS",
    "NO_LIABILITY_ACCOUNTS",
    "NO_ACCOUNTS",
    "PRODUCT_NOT_READY",
    "ADDITIONAL_CONSENT_REQUIRED",
}
# Only fixed codes may be persisted, displayed, or included in exception messages.
# A character whitelist alone also accepts many real tokens and account numbers.
SAFE_ERROR_CODES = OPTIONAL_UNAVAILABLE | {
    "ACCESS_NOT_GRANTED",
    "ACCOUNT_ITEM_MISMATCH",
    "DUPLICATE_CONNECTION",
    "INTERNAL_SERVER_ERROR",
    "INVALID_ACCESS_TOKEN",
    "INVALID_CREDENTIALS",
    "INVALID_KEY_ID",
    "INVALID_MFA",
    "INVALID_REQUEST",
    "INVALID_SYNC_CURSOR",
    "ITEM_LOCKED",
    "ITEM_LOGIN_REQUIRED",
    "ITEM_NOT_FOUND",
    "NOT_CONFIGURED",
    "REQUEST_FAILED",
    "SANDBOX_ONLY",
    "UNKNOWN_ERROR",
    "UNMAPPED_ACCOUNT",
    "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION",
    "USER_PERMISSION_REVOKED",
}


def _safe(value):
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) else None
    )


class PlaidError(Exception):
    def __init__(self, error_code=None, error_type=None, request_id=None):
        self.error_code = (
            error_code
            if isinstance(error_code, str) and error_code in SAFE_ERROR_CODES
            else "UNKNOWN_ERROR"
        )
        self.error_type = _safe(error_type)
        self.request_id = _safe(request_id)
        super().__init__(self.error_code)


def _model(module, **values):
    cls = getattr(
        import_module(f"plaid.model.{module}"), "".join(s.title() for s in module.split("_"))
    )
    try:
        return cls(**values)
    except Exception:
        raise PlaidError("INVALID_REQUEST") from None


def _enum(module, value):
    cls = getattr(
        import_module(f"plaid.model.{module}"), "".join(s.title() for s in module.split("_"))
    )
    return cls(value)


class PlaidClient:
    def __init__(self, api=None):
        # Import the SDK only when a connection actually needs it.
        import plaid
        from plaid.api import plaid_api

        self._api_exception = plaid.ApiException
        if api is not None:
            self.api = api
            return
        settings = get_settings()
        if not settings.plaid_configured:
            raise PlaidError("NOT_CONFIGURED")
        configuration = plaid.Configuration(
            host=(
                plaid.Environment.Sandbox
                if settings.plaid_env == "sandbox"
                else plaid.Environment.Production
            ),
            api_key={
                "clientId": settings.plaid_client_id.get_secret_value(),
                "secret": settings.plaid_secret.get_secret_value(),
            },
        )
        self.api = plaid_api.PlaidApi(plaid.ApiClient(configuration))

    def _call(self, method, model, **values):
        request = _model(model, **values)
        try:
            return getattr(self.api, method)(request, _request_timeout=30).to_dict()
        except self._api_exception as exc:
            try:
                body = json.loads(exc.body or "{}")
                if not isinstance(body, dict):
                    body = {}
            except (ValueError, TypeError):
                body = {}
            raise PlaidError(
                body.get("error_code"), body.get("error_type"), body.get("request_id")
            ) from None
        except Exception:
            raise PlaidError("REQUEST_FAILED") from None

    def create_link_token(self, **values):
        values["user"] = _model("link_token_create_request_user", **values["user"])
        values["country_codes"] = [_enum("country_code", v) for v in values["country_codes"]]
        values["hosted_link"] = _model("link_token_create_hosted_link", **values["hosted_link"])
        for field in ("products", "optional_products"):
            if field in values:
                values[field] = [_enum("products", value) for value in values[field]]
        if "transactions" in values:
            values["transactions"] = _model("link_token_transactions", **values["transactions"])
        return self._call("link_token_create", "link_token_create_request", **values)

    def get_link_token(self, link_token):
        return self._call("link_token_get", "link_token_get_request", link_token=link_token)

    def exchange_public_token(self, public_token):
        return self._call(
            "item_public_token_exchange",
            "item_public_token_exchange_request",
            public_token=public_token,
        )

    def get_item(self, access_token):
        return self._call("item_get", "item_get_request", access_token=access_token)

    def get_institution(self, institution_id):
        return self._call(
            "institutions_get_by_id",
            "institutions_get_by_id_request",
            institution_id=institution_id,
            country_codes=[_enum("country_code", "US")],
        )

    def get_accounts(self, access_token):
        return self._call("accounts_get", "accounts_get_request", access_token=access_token)

    def sync_transactions(self, access_token, cursor=None, count=500):
        values = {"access_token": access_token, "count": count}
        if cursor:
            values["cursor"] = cursor
        return self._call("transactions_sync", "transactions_sync_request", **values)

    def get_holdings(self, access_token):
        return self._call(
            "investments_holdings_get",
            "investments_holdings_get_request",
            access_token=access_token,
        )

    def get_liabilities(self, access_token):
        return self._call("liabilities_get", "liabilities_get_request", access_token=access_token)

    def remove_item(self, access_token):
        return self._call("item_remove", "item_remove_request", access_token=access_token)

    def get_webhook_key(self, kid):
        return self._call(
            "webhook_verification_key_get", "webhook_verification_key_get_request", key_id=kid
        )

    def sandbox_public_token(self, institution_id="ins_109508", products=None):
        if get_settings().plaid_env != "sandbox":
            raise PlaidError("SANDBOX_ONLY")
        return self._call(
            "sandbox_public_token_create",
            "sandbox_public_token_create_request",
            institution_id=institution_id,
            initial_products=[
                _enum("products", p)
                for p in (products or ["transactions", "investments", "liabilities"])
            ],
        )

    def sandbox_reset_login(self, access_token):
        if get_settings().plaid_env != "sandbox":
            raise PlaidError("SANDBOX_ONLY")
        return self._call(
            "sandbox_item_reset_login",
            "sandbox_item_reset_login_request",
            access_token=access_token,
        )


def decimal(value):
    return None if value is None else Decimal(str(value))


def category(value):
    value = value or {}
    primary = value.get("primary")
    if primary in {"TRANSFER_IN", "TRANSFER_OUT"} or value.get("detailed") == (
        "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
    ):
        return "Transfer"
    return primary.replace("_", " ").title() if primary else None


def _date(value):
    return dt.date.fromisoformat(value) if isinstance(value, str) else value


def _item(session, item):
    row = session.get(PlaidItem, item if isinstance(item, int) else item.id)
    if row is None:
        raise PlaidError("ITEM_NOT_FOUND")
    return row


def _mapping(session, item):
    return dict(
        session.execute(
            select(PlaidAccount.plaid_account_id, PlaidAccount.account_id).where(
                PlaidAccount.item_id == item.id
            )
        ).all()
    )


def _fact(session, account_id, key, value):
    if value is None:
        return
    statement = insert(AccountFact).values(account_id=account_id, key=key, value=value)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["account_id", "key"], set_={"value": statement.excluded.value}
        )
    )


def _legacy_history(session, mapping):
    mapping.legacy_transaction_max_id = session.scalar(
        select(func.max(Transaction.id)).where(Transaction.account_id == mapping.account_id)
    )


class PlaidAggregator:
    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = PlaidClient()
        return self._client

    def link(self, kind="connect", product_set="banking", redirect_base="", item=None):
        from aggregators.plaid_link import start

        return start(self, kind, product_set, redirect_base, item)

    def finish(self, state):
        from aggregators.plaid_link import finish

        return finish(self, state)

    def remove(self, item):
        from aggregators.plaid_link import remove

        return remove(self, item)

    def sync_accounts(self, item, *, session=None, record_balances=True):
        from aggregators.plaid_link import resolve_institution

        with nullcontext(session) if session is not None else session_scope() as active:
            item = _item(active, item)
            data = self.client.get_accounts(decrypt(item.access_token_encrypted))
            resolve_institution(self, item, active)
            mappings = list(
                active.scalars(select(PlaidAccount).where(PlaidAccount.item_id == item.id))
            )
            if not mappings:
                other_accounts = set(
                    active.execute(
                        select(Account.name, Account.mask, Account.subtype)
                        .join(PlaidAccount, PlaidAccount.account_id == Account.id)
                        .where(
                            Account.institution_id == item.institution_id,
                            PlaidAccount.item_id != item.id,
                        )
                    ).all()
                )
                if any(
                    row.get("mask") is not None
                    and (row["name"], row["mask"], row.get("subtype")) in other_accounts
                    for row in data["accounts"]
                ):
                    raise PlaidError("DUPLICATE_CONNECTION")
            today = local_today()
            returned = {row["account_id"] for row in data["accounts"]}
            missing = [mapping for mapping in mappings if mapping.plaid_account_id not in returned]
            recent = [mapping for mapping in missing if mapping.missing_since in {None, today}]
            known_ids = {mapping.plaid_account_id for mapping in mappings}
            new_remotes = [row for row in data["accounts"] if row["account_id"] not in known_ids]
            # Decide both directions before changing any mapping; iteration order cannot decide.
            candidates = {}
            for remote in new_remotes:
                if remote.get("mask") is not None:
                    candidates[remote["account_id"]] = [
                        candidate
                        for candidate in recent
                        if (old := active.get(Account, candidate.account_id)).mask == remote["mask"]
                        and old.subtype == remote.get("subtype")
                    ]
            for mapping in missing:
                mapping.missing_since = mapping.missing_since or today
            balances = []
            for remote in data["accounts"]:
                mapping = active.get(PlaidAccount, remote["account_id"])
                if mapping is not None and mapping.item_id != item.id:
                    raise PlaidError("ACCOUNT_ITEM_MISMATCH")
                if mapping is None and remote.get("mask") is not None:
                    matches = candidates.get(remote["account_id"], [])
                    if (
                        len(matches) == 1
                        and sum(matches[0] in options for options in candidates.values()) == 1
                    ):
                        mapping = matches[0]
                        mapping.plaid_account_id = remote["account_id"]
                        _legacy_history(active, mapping)
                        item.transactions_cursor = None
                account = active.get(Account, mapping.account_id) if mapping else None
                if account is None:
                    account = active.scalar(
                        select(Account)
                        .where(
                            Account.institution_id == item.institution_id,
                            Account.is_manual.is_(False),
                            Account.name == remote["name"],
                            Account.mask == remote.get("mask"),
                            Account.subtype == remote.get("subtype"),
                            ~Account.id.in_(select(PlaidAccount.account_id)),
                        )
                        .order_by(Account.id)
                    )
                    adopted = account is not None
                    if account is None:
                        kind = infer_type(
                            "plaid",
                            remote["type"],
                            remote.get("subtype"),
                            remote["name"],
                            session=active,
                        )
                        currency = (
                            remote["balances"].get("iso_currency_code")
                            or remote["balances"].get("unofficial_currency_code")
                            or "XXX"
                        )
                        account = Account(
                            institution_id=item.institution_id,
                            name=remote["name"],
                            type=kind,
                            subtype=remote.get("subtype"),
                            is_liability=is_liability_type(kind),
                            currency=currency,
                            hidden=currency != "USD",
                            is_manual=False,
                        )
                        active.add(account)
                        active.flush()
                    mapping = PlaidAccount(
                        plaid_account_id=remote["account_id"],
                        item_id=item.id,
                        account_id=account.id,
                    )
                    active.add(mapping)
                    if adopted:
                        account.hidden = account.currency != "USD"
                        _legacy_history(active, mapping)
                mapping.missing_since = None
                account.name = remote["name"]
                account.mask = remote.get("mask")
                limit = decimal(remote["balances"].get("limit"))
                _fact(
                    active,
                    account.id,
                    "credit limit",
                    format(limit, "f") if limit is not None else None,
                )
                current = decimal(remote["balances"].get("current"))
                if current is None and remote["type"] == "depository":
                    current = decimal(remote["balances"].get("available"))
                if current is not None:
                    balances.append(AccountBalance(account.id, current))
                    if record_balances:
                        record_daily(account.id, today, current, source="plaid", session=active)
            active.flush()
            return balances

    def _pages(self, token, stored_cursor):
        for attempt in range(3):
            pages = []
            cursor = stored_cursor
            try:
                while True:
                    page = self.client.sync_transactions(token, cursor=cursor, count=500)
                    pages.append(page)
                    next_cursor = page.get("next_cursor") or cursor
                    if not page["has_more"]:
                        return pages, next_cursor
                    if next_cursor == cursor:
                        raise PlaidError("INVALID_SYNC_CURSOR")
                    cursor = next_cursor
            except PlaidError as exc:
                if exc.error_code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" or attempt == 2:
                    raise

    def sync_transactions(self, item, *, session=None):
        with nullcontext(session) if session is not None else session_scope() as active:
            item = _item(active, item)
            try:
                pages, cursor = self._pages(
                    decrypt(item.access_token_encrypted), item.transactions_cursor
                )
            except PlaidError as exc:
                if exc.error_code in OPTIONAL_UNAVAILABLE:
                    return
                raise
            mapping = _mapping(active, item)
            changed = [row for page in pages for key in ("added", "modified") for row in page[key]]
            if any(row["account_id"] not in mapping for row in changed):
                previous_cursor = item.transactions_cursor
                self.sync_accounts(item, session=active)
                mapping = _mapping(active, item)
                if previous_cursor is not None and item.transactions_cursor is None:
                    # Discovery repointed an ID: restart history instead of saving a delta cursor.
                    return self.sync_transactions(item, session=active)
                if any(row["account_id"] not in mapping for row in changed):
                    raise PlaidError("UNMAPPED_ACCOUNT")
            watermarks = dict(
                active.execute(
                    select(PlaidAccount.account_id, PlaidAccount.legacy_transaction_max_id).where(
                        PlaidAccount.item_id == item.id
                    )
                ).all()
            )
            for page in pages:
                for row in page["added"] + page["modified"]:
                    account_id = mapping[row["account_id"]]
                    watermark = watermarks[account_id]
                    if row in page["added"] and watermark is not None:
                        existing = active.scalar(
                            select(Transaction.id).where(
                                Transaction.account_id == account_id,
                                Transaction.external_id == row["transaction_id"],
                            )
                        )
                        if existing is None:
                            legacy_id = active.scalar(
                                select(Transaction.id)
                                .where(
                                    Transaction.account_id == account_id,
                                    Transaction.date == _date(row["date"]),
                                    Transaction.amount == _money(-decimal(row["amount"])),
                                    Transaction.pending == row["pending"],
                                    Transaction.external_id.is_not(None),
                                    Transaction.id <= watermark,
                                )
                                .order_by(Transaction.id)
                                .limit(1)
                            )
                            if legacy_id is not None:
                                active.execute(
                                    delete(Transaction).where(Transaction.id == legacy_id)
                                )
                    pending_id = row.get("pending_transaction_id")
                    if pending_id:
                        active.execute(
                            delete(Transaction).where(
                                Transaction.account_id == account_id,
                                Transaction.external_id == pending_id,
                            )
                        )
                    statement = insert(Transaction).values(
                        account_id=account_id,
                        external_id=row["transaction_id"],
                        date=_date(row["date"]),
                        amount=-decimal(row["amount"]),
                        merchant=row.get("merchant_name") or row.get("name"),
                        memo=row.get("name"),
                        pending=row["pending"],
                        category=category(row.get("personal_finance_category")),
                    )
                    if watermark is not None:
                        # SQLite can reuse deleted row IDs; new rows must stay above the watermark.
                        highest = active.scalar(select(func.max(Transaction.id))) or 0
                        statement = statement.values(id=max(highest, watermark) + 1)
                    active.execute(
                        statement.on_conflict_do_update(
                            index_elements=["account_id", "external_id"],
                            set_={
                                key: getattr(statement.excluded, key)
                                for key in (
                                    "date",
                                    "amount",
                                    "merchant",
                                    "memo",
                                    "pending",
                                    "category",
                                )
                            },
                        )
                    )
                removed = [row["transaction_id"] for row in page["removed"]]
                active.execute(
                    delete(Transaction).where(
                        Transaction.account_id.in_(list(mapping.values())),
                        Transaction.external_id.in_(removed),
                    )
                )
            if (
                not pages[-1]["has_more"]
                and pages[-1].get("transactions_update_status") == "HISTORICAL_UPDATE_COMPLETE"
            ):
                # Plaid has now re-sent everything, so an unmatched old pending row has posted.
                for account_id, watermark in watermarks.items():
                    if watermark is not None:
                        active.execute(
                            delete(Transaction).where(
                                Transaction.account_id == account_id,
                                Transaction.pending.is_(True),
                                Transaction.external_id.is_not(None),
                                Transaction.id <= watermark,
                            )
                        )
                active.execute(
                    update(PlaidAccount)
                    .where(PlaidAccount.item_id == item.id)
                    .values(legacy_transaction_max_id=None)
                )
            item.transactions_cursor = cursor or item.transactions_cursor
            active.flush()

    def _optional(self, method, item):
        try:
            return getattr(self.client, method)(decrypt(item.access_token_encrypted))
        except PlaidError as exc:
            if exc.error_code in OPTIONAL_UNAVAILABLE:
                return None
            raise

    def _resolve_accounts(self, active, item, ids):
        mapping = _mapping(active, item)
        if any(key not in mapping for key in ids):
            self.sync_accounts(item, session=active)
            mapping = _mapping(active, item)
        if any(key not in mapping for key in ids):
            raise PlaidError("UNMAPPED_ACCOUNT")
        return mapping

    def sync_holdings(self, item, *, today=None, session=None):
        with nullcontext(session) if session is not None else session_scope() as active:
            item = _item(active, item)
            data = self._optional("get_holdings", item)
            if data is None:
                return
            today = today or dt.date.today()
            ids = {row["account_id"] for row in data["accounts"] + data["holdings"]}
            mapping = self._resolve_accounts(active, item, ids)
            securities = {row["security_id"]: row for row in data["securities"]}
            active.execute(
                delete(Holding).where(
                    Holding.account_id.in_([mapping[key] for key in ids]), Holding.date == today
                )
            )
            for row in data["holdings"]:
                security = securities.get(row["security_id"], {})
                active.add(
                    Holding(
                        account_id=mapping[row["account_id"]],
                        date=today,
                        ticker=security.get("ticker_symbol"),
                        name=security.get("name"),
                        qty=decimal(row.get("quantity")),
                        value=decimal(row.get("institution_value")),
                        cost_basis=decimal(row.get("cost_basis")),
                    )
                )
            active.flush()

    def sync_liabilities(self, item, *, session=None):
        from aggregators.plaid_liabilities import facts

        with nullcontext(session) if session is not None else session_scope() as active:
            item = _item(active, item)
            data = self._optional("get_liabilities", item)
            if data is None:
                return
            rows = [
                (kind, row)
                for kind in ("credit", "mortgage", "student")
                for row in (data["liabilities"].get(kind) or [])
                if row.get("account_id") is not None
            ]
            mapping = self._resolve_accounts(active, item, {row["account_id"] for _, row in rows})
            for kind, row in rows:
                for key, value in facts(kind, row).items():
                    _fact(active, mapping[row["account_id"]], key, value)
