from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.templating import templates
from core.clock import local_today
from core.db import get_session
from core.rollups import RANGES, SchemaOutOfDateError, read_chart, read_dashboard
from core.snapshots import MixedCurrencyError

router = APIRouter()


def get_today() -> date:
    return local_today()


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    today: Annotated[date, Depends(get_today)],
):
    data, notice = None, None
    try:
        data = read_dashboard(session, today)
    except MixedCurrencyError as exc:
        notice = f"Orbit can't add up accounts in different currencies yet. {exc}."
    except SchemaOutOfDateError:
        notice = "The database needs updating. Run: alembic upgrade head"
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"dashboard": data, "notice": notice, "today": today, "ranges": RANGES},
    )


@router.get("/dashboard/chart", response_class=HTMLResponse)
def dashboard_chart(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    today: Annotated[date, Depends(get_today)],
    range_key: Annotated[str, Query(alias="range")] = "1Y",
):
    if range_key not in RANGES:
        raise HTTPException(status_code=400, detail="Unknown chart range")
    try:
        chart = read_chart(session, today, range_key)
    except (MixedCurrencyError, SchemaOutOfDateError):
        chart = None
    if chart is None:
        chart = {
            "range": range_key,
            "monthly": range_key in ("1Y", "All"),
            "dates": [],
            "series": [],
        }
    return templates.TemplateResponse(request, "_chart.html", {"chart": chart})
