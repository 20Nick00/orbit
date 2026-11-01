"""Single-owner connection controls with same-origin form submissions."""

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select

from aggregators.locks import connections_lock, item_lock
from aggregators.plaid import PlaidAggregator, PlaidError
from aggregators.plaid_link import utcnow
from app.security import require_csrf
from app.templating import templates
from core.clock import local_today
from core.config import get_settings
from core.crypto import CryptoError, cipher, token_unreadable
from core.db import session_scope
from core.models import (
    Account,
    BalanceSnapshot,
    Institution,
    PlaidAccount,
    PlaidItem,
    PlaidLinkSession,
)
from core.snapshots import record_daily
from jobs import backup
from jobs.nightly import sync_item

router = APIRouter()
MESSAGES = {
    "connected": "Connected. The first account sync is running.",
    "reauthenticated": "Connection restored and accounts updated.",
    "removed": "Connection removed. Accounts and their history remain in Orbit.",
    "pending": "Link is not finished yet. Complete the Plaid window, then check again.",
    "expired": "Link expired. Start a new connection or re-authenticate.",
    "duplicate": "Already connected, use re-authenticate.",
    "error": "Plaid could not finish that request. Your saved connections are kept. Try again.",
    "config": "Configure the settings below before connecting.",
    "currency": "This account is not in USD and must stay hidden from totals.",
    "forgotten": "Connection forgotten locally. Remove it from the Plaid dashboard too.",
}


def missing_config():
    settings = get_settings()
    missing = [
        name
        for name, value in (
            ("PLAID_CLIENT_ID", settings.plaid_client_id),
            ("PLAID_SECRET", settings.plaid_secret),
        )
        if not value.get_secret_value()
    ]
    try:
        cipher()
    except CryptoError:
        missing.append("FERNET_KEY")
    return missing


def _redirect(message):
    return RedirectResponse(f"/settings?message={message}", status_code=303)


@router.get("/settings")
def settings_page(request: Request):
    with session_scope() as session:
        session.execute(delete(PlaidLinkSession).where(PlaidLinkSession.expires_at <= utcnow()))
        items = []
        for item, name in session.execute(
            select(PlaidItem, Institution.name)
            .join(Institution, Institution.id == PlaidItem.institution_id)
            .order_by(Institution.name)
        ):
            items.append(
                {
                    "id": item.id,
                    "name": name,
                    "status": item.status,
                    "last_error_code": item.last_error_code,
                    "last_synced_at": item.last_synced_at,
                    "unreadable": token_unreadable(item.access_token_encrypted),
                    "accounts": [],
                }
            )
        by_item = {item["id"]: item for item in items}
        disconnected = []
        has_balance = (
            select(BalanceSnapshot.account_id)
            .where(BalanceSnapshot.account_id == Account.id)
            .exists()
        )
        latest_source = (
            select(BalanceSnapshot.source)
            .where(BalanceSnapshot.account_id == Account.id)
            .order_by(BalanceSnapshot.date.desc())
            .limit(1)
            .scalar_subquery()
        )
        for account, mapping, recorded, source in session.execute(
            select(Account, PlaidAccount, has_balance, latest_source)
            .join(Institution, Institution.id == Account.institution_id)
            .outerjoin(PlaidAccount, PlaidAccount.account_id == Account.id)
            .where(Institution.aggregator == "plaid", Account.is_manual.is_(False))
            .order_by(Account.name, Account.id)
        ):
            details = {
                "id": account.id,
                "name": account.name,
                "mask": account.mask[-4:] if account.mask else None,
                "hidden": account.hidden,
                "currency": account.currency,
                "missing_since": mapping.missing_since if mapping else None,
                "has_balance": recorded,
                "can_close": mapping is None or mapping.missing_since is not None,
                "closed": source == "plaid-closed",
            }
            if mapping:
                by_item[mapping.item_id]["accounts"].append(details)
            else:
                disconnected.append(details)
        pending = [
            dict(row._mapping)
            for row in session.execute(
                select(PlaidLinkSession.id, PlaidLinkSession.kind, PlaidLinkSession.created_at)
            )
        ]
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "items": items,
            "disconnected": disconnected,
            "sandbox": get_settings().plaid_env == "sandbox",
            "pending": pending,
            "missing": missing_config(),
            "webhook_invalid": bool(get_settings().plaid_webhook_url)
            and not get_settings().plaid_webhook_usable,
            "message": MESSAGES.get(request.query_params.get("message")),
            "last_backup": backup.latest(),
        },
    )


@router.post("/settings/accounts/{account_id}/visibility", dependencies=[Depends(require_csrf)])
def visibility(account_id: int, hidden: Annotated[Literal["true", "false"], Form()]):
    with session_scope() as session:
        account = session.scalar(
            select(Account)
            .join(Institution, Institution.id == Account.institution_id)
            .where(Account.id == account_id, Institution.aggregator == "plaid")
        )
        if account is None:
            raise HTTPException(404, "Plaid account not found")
        if hidden == "false" and account.currency != "USD":
            return _redirect("currency")
        account.hidden = hidden == "true"
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/plaid/items/{item_id}/forget", dependencies=[Depends(require_csrf)])
def forget(item_id: int):
    with connections_lock, item_lock(item_id), session_scope() as session:
        item = session.get(PlaidItem, item_id)
        if item is None:
            raise HTTPException(404, "Connection not found")
        if not token_unreadable(item.access_token_encrypted):
            raise HTTPException(409, "A valid FERNET_KEY and an unreadable token are required.")
        session.execute(delete(PlaidAccount).where(PlaidAccount.item_id == item_id))
        session.execute(delete(PlaidLinkSession).where(PlaidLinkSession.plaid_item_id == item_id))
        session.delete(item)
    return _redirect("forgotten")


@router.post("/settings/accounts/{account_id}/closed", dependencies=[Depends(require_csrf)])
def mark_closed(account_id: int):
    with session_scope() as session:
        account = session.scalar(
            select(Account)
            .join(Institution, Institution.id == Account.institution_id)
            .where(
                Account.id == account_id,
                Institution.aggregator == "plaid",
                Account.is_manual.is_(False),
            )
        )
        if account is None:
            raise HTTPException(404, "Plaid account not found")
        mapping = session.scalar(select(PlaidAccount).where(PlaidAccount.account_id == account_id))
        if mapping is not None and mapping.missing_since is None:
            raise HTTPException(409, "Only missing or disconnected accounts can be marked closed.")
        record_daily(
            account_id, local_today(), Decimal("0"), source="plaid-closed", session=session
        )
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/plaid/connect", dependencies=[Depends(require_csrf)])
def connect(
    request: Request,
    product_set: Annotated[Literal["banking", "investments"], Form()] = "banking",
):
    if missing_config():
        return _redirect("config")
    try:
        result = PlaidAggregator().link("connect", product_set, _callback_url(request))
    except (PlaidError, CryptoError):
        return _redirect("error")
    return RedirectResponse(result.url, status_code=303)


def _callback_url(request):
    public_url = get_settings().public_url
    if public_url:
        return public_url.rstrip("/") + "/settings/plaid/callback"
    return str(request.url_for("plaid_callback"))


def _require_item(item_id):
    with session_scope() as session:
        if session.get(PlaidItem, item_id) is None:
            raise HTTPException(404, "Connection not found")


@router.post("/settings/plaid/items/{item_id}/reauth", dependencies=[Depends(require_csrf)])
def reauth(request: Request, item_id: int):
    _require_item(item_id)
    if missing_config():
        return _redirect("config")
    try:
        result = PlaidAggregator().link("reauth", "banking", _callback_url(request), item=item_id)
    except (PlaidError, CryptoError):
        return _redirect("error")
    return RedirectResponse(result.url, status_code=303)


@router.post("/settings/plaid/items/{item_id}/remove", dependencies=[Depends(require_csrf)])
def remove(item_id: int):
    _require_item(item_id)
    if missing_config():
        return _redirect("config")
    try:
        PlaidAggregator().remove(item_id)
    except (PlaidError, CryptoError):
        return _redirect("error")
    return _redirect("removed")


def _finish(state, background_tasks):
    with session_scope() as session:
        session.execute(delete(PlaidLinkSession).where(PlaidLinkSession.expires_at <= utcnow()))
        known = session.scalar(select(PlaidLinkSession.id).where(PlaidLinkSession.state == state))
    if known is None:
        return _redirect("expired")
    if missing_config():
        return _redirect("config")
    try:
        result = PlaidAggregator().finish(state)
    except (PlaidError, CryptoError):
        return _redirect("error")
    for item_id in result.item_ids:
        background_tasks.add_task(sync_item, item_id)
    return _redirect(result.message)


@router.get("/settings/plaid/callback", name="plaid_callback")
def callback(background_tasks: BackgroundTasks, state: str = ""):
    return _finish(state, background_tasks)


@router.post("/settings/plaid/sessions/{session_id}/check", dependencies=[Depends(require_csrf)])
def check(session_id: int, background_tasks: BackgroundTasks):
    with session_scope() as session:
        state = session.scalar(
            select(PlaidLinkSession.state).where(PlaidLinkSession.id == session_id)
        )
    return _finish(state or "", background_tasks)
