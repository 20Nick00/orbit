import re
from datetime import date
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.formatting import parse_balance
from app.reporting import money  # noqa: F401 -- registers shared template filters
from app.routers.dashboard import get_today
from app.security import require_csrf
from app.templating import templates
from core.db import get_session
from core.detail import DETAIL_RANGES, read_account, read_type
from core.models import Account
from core.rollups import SchemaOutOfDateError
from core.snapshots import (
    MixedCurrencyError,
    MonthEndConflictError,
    MonthEndLockedError,
    MonthNotClosedError,
    MonthNotOverError,
    SnapshotNotFoundError,
    _month_end,
    close_month,
    edit_snapshot,
    reopen_month,
)
from property.valuation import home_equity, valuation_status

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]
TodayDep = Annotated[date, Depends(get_today)]
NOTICES = {
    "saved": "Balance saved.",
    "unchanged": "Balance unchanged.",
    "closed": "Month closed for all accounts.",
    "reopened": "Month reopened for all accounts. Close it again after making corrections.",
}
ERRORS = {
    "balance": "Enter a valid balance with at most two decimal places.",
    "month": "Choose a valid month.",
    "date": "Choose a valid history date.",
    "missing": "No balance was recorded on that day.",
    "locked": "Reopen the month before editing its month-end balance.",
    "not_closed": "That month is not closed.",
    "not_over": "The month has not ended yet.",
    "conflict": "The closed balance differs from daily history. Reopen the month first.",
    "confirm": "The confirmation must match the month exactly.",
    "database": "The change could not be saved. No changes were made.",
    "empty": "Nothing to close: the month is already closed or has no balances.",
}


def _page(request, session, today, template, reader, key, extra=None, **kwargs):
    notice = None
    try:
        data = reader(session, key, today, **kwargs)
        if data is None:
            raise HTTPException(status_code=404, detail="Not found")
    except MixedCurrencyError as exc:
        data = None
        notice = f"Orbit can't add up accounts in different currencies yet. {exc}."
    except SchemaOutOfDateError:
        session.rollback()
        data = None
        notice = "The database needs updating. Run: alembic upgrade head"
    except OperationalError as exc:
        if "no such table" not in str(exc.orig):
            raise
        session.rollback()
        data = None
        notice = "The database needs updating. Run: alembic upgrade head"
    return templates.TemplateResponse(
        request,
        template,
        {
            "detail": data,
            "today": today,
            "ranges": DETAIL_RANGES,
            "notice": notice,
            "message": NOTICES.get(request.query_params.get("notice")),
            "error": ERRORS.get(request.query_params.get("error")),
            **(extra or {}),
        },
    )


@router.get("/account/{account_id}", response_class=HTMLResponse)
def account_page(
    request: Request,
    account_id: int,
    session: SessionDep,
    today: TodayDep,
    month: str | None = None,
):
    extra = None
    try:
        account = session.get(Account, account_id)
        if account is not None and account.type == "property":
            extra = {
                "valuation": valuation_status(account_id, session=session, today=today),
                "equity": home_equity(account_id, session=session, today=today),
            }
    except OperationalError:
        # On a database not yet upgraded, _page shows the "needs updating" notice.
        session.rollback()
    return _page(
        request, session, today, "account.html", read_account, account_id, extra=extra, month=month
    )


@router.get("/type/{type_key}", response_class=HTMLResponse)
def type_page(request: Request, type_key: str, session: SessionDep, today: TodayDep):
    return _page(request, session, today, "type.html", read_type, type_key)


def _chart_response(request, session, today, reader, key, range_key):
    if range_key not in DETAIL_RANGES:
        raise HTTPException(status_code=400, detail="Unknown chart range")
    notice = None
    chart = {
        "range": range_key,
        "monthly": range_key in ("1Y", "All"),
        "dates": [],
        "series": [],
        "h": 190,
        "label": "Balance over time",
    }
    try:
        data = reader(session, key, today, range_key=range_key)
        if data is None:
            raise HTTPException(status_code=404, detail="Not found")
        chart = data["chart"]
    except MixedCurrencyError:
        pass
    except SchemaOutOfDateError:
        session.rollback()
        notice = "The database needs updating. Run: alembic upgrade head"
    except OperationalError as exc:
        if "no such table" not in str(exc.orig):
            raise
        session.rollback()
        notice = "The database needs updating. Run: alembic upgrade head"
    return templates.TemplateResponse(request, "_chart.html", {"chart": chart, "notice": notice})


@router.get("/account/{account_id}/chart", response_class=HTMLResponse)
def account_chart(
    request: Request,
    account_id: int,
    session: SessionDep,
    today: TodayDep,
    range_key: Annotated[str, Query(alias="range")] = "3M",
):
    return _chart_response(request, session, today, read_account, account_id, range_key)


@router.get("/type/{type_key}/chart", response_class=HTMLResponse)
def type_chart(
    request: Request,
    type_key: str,
    session: SessionDep,
    today: TodayDep,
    range_key: Annotated[str, Query(alias="range")] = "3M",
):
    return _chart_response(request, session, today, read_type, type_key, range_key)


def _post(session, account_id, month, action, invalid_code):
    if session.get(Account, account_id) is None:
        raise HTTPException(status_code=404, detail="Not found")
    safe_month = None
    try:
        safe_month, _ = _month_end(month)
    except ValueError:
        session.rollback()
        code, kind = "month", "error"
    else:
        try:
            code = action()
            session.commit()
            kind = "error" if code in ERRORS else "notice"
        except (
            MonthEndLockedError,
            SnapshotNotFoundError,
            MonthNotClosedError,
            MonthNotOverError,
            MonthEndConflictError,
            ValueError,
            SQLAlchemyError,
        ) as exc:
            session.rollback()
            kind = "error"
            code = next(
                (
                    value
                    for cls, value in (
                        (MonthEndLockedError, "locked"),
                        (SnapshotNotFoundError, "missing"),
                        (MonthNotClosedError, "not_closed"),
                        (MonthNotOverError, "not_over"),
                        (MonthEndConflictError, "conflict"),
                        (SQLAlchemyError, "database"),
                    )
                    if isinstance(exc, cls)
                ),
                invalid_code,
            )
    params = {kind: code}
    if safe_month:
        params["month"] = safe_month
    return RedirectResponse(f"/account/{account_id}?{urlencode(params)}#history", status_code=303)


@router.post("/account/{account_id}/history/close", dependencies=[Depends(require_csrf)])
def history_close(
    account_id: int, session: SessionDep, today: TodayDep, month: Annotated[str, Form()] = ""
):
    def action():
        return "closed" if close_month(month, today=today, session=session) else "empty"

    return _post(session, account_id, month, action, "month")


@router.post("/account/{account_id}/history/reopen", dependencies=[Depends(require_csrf)])
def history_reopen(
    account_id: int,
    session: SessionDep,
    month: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
):
    def action():
        if confirm != month:
            raise ValueError("Confirmation mismatch")
        reopen_month(month, reason=reason or None, session=session)
        return "reopened"

    return _post(session, account_id, month, action, "confirm")


@router.post("/account/{account_id}/history/{day}", dependencies=[Depends(require_csrf)])
def history_edit(
    account_id: int,
    day: str,
    session: SessionDep,
    balance: Annotated[str, Form()] = "",
    month: Annotated[str, Form()] = "",
):
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
            raise ValueError("Invalid date")
        parsed = date.fromisoformat(day)
    except ValueError:
        parsed = None

    def action():
        if parsed is None:
            raise ValueError("Invalid date")
        changed = edit_snapshot(account_id, parsed, parse_balance(balance), session=session)
        return "saved" if changed else "unchanged"

    return _post(session, account_id, month, action, "date" if parsed is None else "balance")
