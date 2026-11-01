"""Hosted Link lifecycle and preservation of disconnected account history."""

import datetime as dt
import secrets
from dataclasses import dataclass

from sqlalchemy import delete, select

from aggregators.base import LinkStart
from aggregators.locks import connections_lock, item_lock
from aggregators.plaid import PlaidError
from core.config import get_settings
from core.crypto import cipher, decrypt, encrypt
from core.db import session_scope
from core.models import Institution, PlaidAccount, PlaidItem, PlaidLinkSession


def utcnow():
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _timestamp(value):
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(dt.UTC).replace(tzinfo=None) if value.tzinfo else value


@dataclass(frozen=True)
class LinkFinish:
    message: str
    item_ids: tuple[int, ...] = ()


def start(aggregator, kind, product_set, redirect_base, item):
    if kind not in {"connect", "reauth"} or product_set not in {"banking", "investments"}:
        raise ValueError("Invalid connection kind or product set")
    cipher()
    state = secrets.token_urlsafe(32)
    values = {
        "client_name": "Orbit",
        "language": "en",
        "country_codes": ["US"],
        "user": {"client_user_id": "orbit-owner"},
        "hosted_link": {"completion_redirect_uri": f"{redirect_base}?state={state}"},
    }
    settings = get_settings()
    if settings.plaid_webhook_usable:
        values["webhook"] = settings.plaid_webhook_url
    with session_scope() as session:
        item_id = None
        if kind == "reauth":
            item_id = item if isinstance(item, int) else item.id
            row = session.get(PlaidItem, item_id)
            if row is None:
                raise PlaidError("ITEM_NOT_FOUND")
            values["access_token"] = decrypt(row.access_token_encrypted)
        else:
            primary = "transactions" if product_set == "banking" else "investments"
            values["products"] = [primary]
            values["optional_products"] = [
                value
                for value in ("transactions", "investments", "liabilities")
                if value != primary
            ]
            values["transactions"] = {"days_requested": 730}
        data = aggregator.client.create_link_token(**values)
        session.execute(delete(PlaidLinkSession).where(PlaidLinkSession.expires_at <= utcnow()))
        session.add(
            PlaidLinkSession(
                state=state,
                kind=kind,
                plaid_item_id=item_id,
                link_token_encrypted=encrypt(data["link_token"]),
                expires_at=_timestamp(data["expiration"]),
            )
        )
        return LinkStart(data["hosted_link_url"], state)


def _accept(aggregator, public_token):
    exchange = aggregator.client.exchange_public_token(public_token)
    token = exchange["access_token"]
    with session_scope() as session:
        existing = session.scalar(select(PlaidItem).where(PlaidItem.item_id == exchange["item_id"]))
        if existing:
            item_id = existing.id
        else:
            # Persist before the next API call: even a metadata outage must not lose the token.
            institution = Institution(name="Institution details pending", aggregator="plaid")
            session.add(institution)
            session.flush()
            item = PlaidItem(
                item_id=exchange["item_id"],
                institution_id=institution.id,
                access_token_encrypted=encrypt(token),
                status="error",
                last_error_code="INITIAL_SYNC_PENDING",
            )
            session.add(item)
            session.flush()
            item_id = item.id
    try:
        with item_lock(item_id):
            aggregator.sync_accounts(item_id)
    except Exception as exc:
        from jobs.nightly import _failure

        _failure(item_id, exc)
        if isinstance(exc, PlaidError) and exc.error_code == "DUPLICATE_CONNECTION":
            _remove_locked(aggregator, item_id)
            return None, True
    return item_id, False


def resolve_institution(aggregator, item, session):
    institution = session.get(Institution, item.institution_id)
    if institution.external_id is not None:
        return
    metadata = aggregator.client.get_item(decrypt(item.access_token_encrypted))["item"]
    external_id = metadata["institution_id"]
    data = aggregator.client.get_institution(external_id)["institution"]
    existing = session.scalar(
        select(Institution).where(
            Institution.aggregator == "plaid", Institution.external_id == external_id
        )
    )
    if existing:
        existing.name = data["name"]
        item.institution_id = existing.id
        session.flush()
        session.delete(institution)
    else:
        institution.name = data["name"]
        institution.external_id = external_id
    session.flush()


def finish(aggregator, state):
    with connections_lock:
        with session_scope() as session:
            session.execute(delete(PlaidLinkSession).where(PlaidLinkSession.expires_at <= utcnow()))
        with session_scope() as session:
            row = session.scalar(select(PlaidLinkSession).where(PlaidLinkSession.state == state))
            if row is None:
                return LinkFinish("expired")
            session_id, kind, item_id = row.id, row.kind, row.plaid_item_id
            token = decrypt(row.link_token_encrypted)
        data = aggregator.client.get_link_token(token)
        sessions = data.get("link_sessions") or []
        if kind == "reauth":
            # A callback alone is not proof that the user completed update mode.
            if not any(
                row.get("on_success") is not None
                or (
                    row.get("finished_at")
                    and row.get("exit") is None
                    and row.get("on_exit") is None
                )
                for row in sessions
            ):
                return LinkFinish("pending")
            from jobs.nightly import sync_item

            if not sync_item(item_id, client=aggregator.client):
                return LinkFinish("error")
            result = LinkFinish("reauthenticated")
        else:
            tokens = [
                addition["public_token"]
                for remote in sessions
                for addition in ((remote.get("results") or {}).get("item_add_results") or [])
                if addition.get("public_token")
            ]
            if not tokens:
                return LinkFinish("pending")
            ids = []
            duplicate = False
            for token in dict.fromkeys(tokens):
                new_id, rejected = _accept(aggregator, token)
                duplicate |= rejected
                if new_id is not None:
                    ids.append(new_id)
            result = LinkFinish("duplicate" if duplicate else "connected", tuple(ids))
        with session_scope() as session:
            session.execute(delete(PlaidLinkSession).where(PlaidLinkSession.id == session_id))
        return result


def _remove_locked(aggregator, item_id):
    with item_lock(item_id), session_scope() as session:
        row = session.get(PlaidItem, item_id)
        if row is None:
            return
        try:
            aggregator.client.remove_item(decrypt(row.access_token_encrypted))
        except PlaidError as exc:
            if exc.error_code not in {"ITEM_NOT_FOUND", "INVALID_ACCESS_TOKEN"}:
                raise
        session.execute(delete(PlaidAccount).where(PlaidAccount.item_id == item_id))
        session.execute(delete(PlaidLinkSession).where(PlaidLinkSession.plaid_item_id == item_id))
        institution = session.get(Institution, row.institution_id)
        session.delete(row)
        session.flush()
        if institution.external_id is None:
            session.delete(institution)


def remove(aggregator, item):
    item_id = item if isinstance(item, int) else item.id
    with connections_lock:
        _remove_locked(aggregator, item_id)
