import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError

from app.csv_export import csv_response
from app.reporting import money  # noqa: F401 -- registers shared template filters
from app.security import require_csrf
from app.templating import templates
from core.clock import local_today
from core.config import get_settings
from core.db import session_scope
from core.models import Account, AccountFact, Valuation
from property.rentcast import (
    NoEstimateError,
    NotConfiguredError,
    QuotaExceededError,
    RentCastAuthError,
    RentCastError,
    usage_total,
)
from property.valuation import refresh, set_override, valuation_status

router = APIRouter()


def _linked_ids(link):
    return {int(part) for part in (link or "").split(",") if part.strip().isdigit()}


logger = logging.getLogger(__name__)
MESSAGES = {
    "saved": "Property details saved.",
    "override": "Manual override saved. It wins until you refresh.",
    "refreshed": "RentCast estimate refreshed.",
    "invalid_value": "Enter a positive amount in dollars.",
    "invalid_link": "Choose a visible liability account in the same currency.",
    "paid_off_conflict": "Choose No mortgage (paid off) on its own, or select the linked loans.",
    "details_failed": "Property details could not be saved. Please try again.",
    "address": "Add a property address before refreshing.",
    "not_configured": "RentCast is not configured. Add RENTCAST_API_KEY to use estimates.",
    "quota": (
        "RentCast request limit reached for the past 31 days. Try later or enter a manual value."
    ),
    "auth": "RentCast could not authenticate. Check the configured API key.",
    "no_estimate": "RentCast has no estimate for this property. You can enter a manual value.",
    "failed": "The estimate could not be refreshed. Your existing value is unchanged.",
}


def _account(active, account_id):
    account = active.get(Account, account_id)
    if account is None or account.type != "property":
        raise HTTPException(status_code=404, detail="Property account not found.")
    return account


def _redirect(account_id, message):
    return RedirectResponse(f"/account/{account_id}/valuation?message={message}", status_code=303)


@router.get("/account/{id}/valuation")
def property_page(id: int, request: Request):
    today = local_today()
    with session_scope() as active:
        account = _account(active, id)
        status = valuation_status(id, session=active, today=today)
        usage = active.scalar(usage_total(today))
        return templates.TemplateResponse(
            request,
            "property.html",
            {
                "account": account,
                "valuation": status,
                "message": MESSAGES.get(request.query_params.get("message")),
                "quota_reached": usage >= get_settings().rentcast_monthly_limit,
                "liabilities": list(
                    active.scalars(
                        select(Account)
                        .where(
                            Account.is_liability.is_(True),
                            Account.id != id,
                            # A loan already linked stays listed even if hidden, so saving
                            # this form can't silently drop it and overstate equity.
                            or_(
                                and_(
                                    Account.hidden.is_(False),
                                    Account.currency == account.currency,
                                ),
                                Account.id.in_(_linked_ids(status["linked_liability"])),
                            ),
                        )
                        .order_by(Account.name)
                    )
                ),
                "history": list(
                    active.scalars(
                        select(Valuation)
                        .where(Valuation.account_id == id)
                        .order_by(Valuation.date.desc(), Valuation.source)
                    )
                ),
            },
        )


@router.post("/account/{id}/valuation/override", dependencies=[Depends(require_csrf)])
def override(id: int, value: Annotated[str, Form()]):
    with session_scope() as active:
        _account(active, id)
        try:
            text = value.strip().removeprefix("$")
            if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?", text):
                raise ValueError
            set_override(id, Decimal(text.replace(",", "")), today=local_today(), session=active)
        except (ValueError, InvalidOperation):
            return _redirect(id, "invalid_value")
    return _redirect(id, "override")


@router.post("/account/{id}/valuation/refresh", dependencies=[Depends(require_csrf)])
def refresh_property(id: int):
    message = "refreshed"
    try:
        with session_scope() as active:
            _account(active, id)
            refresh(id, today=local_today(), session=active)
    except HTTPException:
        raise
    except NotConfiguredError:
        message = "not_configured"
    except QuotaExceededError:
        message = "quota"
    except RentCastAuthError:
        message = "auth"
    except NoEstimateError:
        message = "no_estimate"
    except ValueError:
        message = "address"
    except RentCastError:
        message = "failed"
    except Exception as exc:
        logger.warning("Property refresh failed for account %s (%s).", id, type(exc).__name__)
        message = "failed"
    return _redirect(id, message)


@router.post("/account/{id}/property-details", dependencies=[Depends(require_csrf)])
def property_details(
    id: int,
    address: Annotated[str, Form()] = "",
    linked_liability: Annotated[list[str] | None, Form()] = None,
):
    choices = list(
        dict.fromkeys(value.strip() for value in (linked_liability or []) if value.strip())
    )
    if "none" in choices and len(choices) > 1:
        return _redirect(id, "paid_off_conflict")
    try:
        # session_scope rolls back SQL errors, including a failed commit.
        with session_scope() as active:
            account = _account(active, id)
            ids = []
            existing = active.get(AccountFact, (id, "linked_liability"))
            already_linked = _linked_ids(existing.value if existing else None)
            if choices != ["none"]:
                for choice in choices:
                    linked = (
                        active.get(Account, int(choice))
                        if choice.isascii()
                        and choice.isdigit()
                        and len(choice) <= 19
                        and int(choice) <= 2**63 - 1
                        else None
                    )
                    if (
                        linked is None
                        or not linked.is_liability
                        or linked.id == id
                        or (
                            linked.id not in already_linked
                            and (linked.hidden or linked.currency != account.currency)
                        )
                    ):
                        return _redirect(id, "invalid_link")
                    ids.append(linked.id)
            link = "none" if choices == ["none"] else ",".join(str(aid) for aid in sorted(set(ids)))
            for key, value in (("address", address.strip()), ("linked_liability", link)):
                statement = insert(AccountFact).values(account_id=id, key=key, value=value or None)
                active.execute(
                    statement.on_conflict_do_update(
                        index_elements=["account_id", "key"],
                        set_={"value": statement.excluded.value},
                    )
                )
    except SQLAlchemyError:
        return _redirect(id, "details_failed")
    return _redirect(id, "saved")


@router.get("/export/account-{id}-valuations.csv")
def valuation_export(id: int):
    with session_scope() as active:
        _account(active, id)
        rows = active.scalars(
            select(Valuation)
            .where(Valuation.account_id == id)
            .order_by(Valuation.date, Valuation.source)
        )
        return csv_response(
            f"account-{id}-valuations.csv",
            ("Date", "Value", "Source"),
            ((r.date, r.value, r.source) for r in rows),
        )
