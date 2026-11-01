from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError

from app.csv_export import csv_response
from app.reporting import VALUE_FIELDS, VALUE_HEADERS, csv_rate, rollup_csv
from app.security import require_csrf
from app.templating import templates
from core.clock import local_today
from core.db import session_scope
from core.models import AlmanacNote
from core.rollups import _data, almanac_records, monthly_rollups, yearly_rollups

router = APIRouter()


def _months(active, today, loaded=None):
    loaded = loaded if loaded is not None else _data(active, today)
    return [
        row
        for row in monthly_rollups(
            f"{today.year}-01",
            today.strftime("%Y-%m"),
            today=today,
            session=active,
            _loaded=loaded,
        )
        if row.month in loaded.data_months
    ]


@router.get("/almanac")
def almanac(request: Request):
    today = local_today()
    with session_scope() as active:
        loaded = _data(active, today)
        years = yearly_rollups(today=today, session=active, _loaded=loaded)
        return templates.TemplateResponse(
            request,
            "almanac.html",
            {
                "today": today,
                "years": years,
                "months": _months(active, today, loaded) if years else [],
                "records": almanac_records(today=today, session=active, _loaded=loaded),
                "fields": VALUE_FIELDS,
                "headers": VALUE_HEADERS,
                "ytd": years[-1] if years else None,
            },
        )


@router.post("/almanac/notes/{year}", dependencies=[Depends(require_csrf)])
def save_note(year: int, request: Request, note: Annotated[str, Form()] = ""):
    note = note.replace("\r\n", "\n")
    error = None
    if not 1900 <= year <= 2200 or len(note) > 2000:
        error = "Use a year from 1900–2200 and at most 2000 characters."
    else:
        try:
            # session_scope rolls back even if the commit itself fails.
            with session_scope() as active:
                statement = insert(AlmanacNote).values(year=year, note=note)
                active.execute(
                    statement.on_conflict_do_update(
                        index_elements=["year"],
                        set_={"note": note, "updated_at": func.current_timestamp()},
                    )
                )
        except SQLAlchemyError:
            error = "Your note could not be saved. Please try again."
    if error or request.headers.get("HX-Request") == "true":
        # HTMX swaps successful responses by default; retain the text and show
        # the error inline for both HTMX and ordinary form submissions.
        return templates.TemplateResponse(
            request,
            "partials/almanac_note.html",
            dict(year=year, note=note, saved=error is None, error=error),
        )
    return RedirectResponse("/almanac", status_code=303)


@router.get("/export/almanac-years.csv")
def years_export():
    return rollup_csv("almanac-years.csv", yearly_rollups(today=local_today()), yearly=True)


@router.get("/export/almanac-months.csv")
def months_export():
    with session_scope() as active:
        today = local_today()
        rows = _months(active, today)
        return rollup_csv("almanac-months.csv", rows)


@router.get("/export/almanac-records.csv")
def records_export():
    records = almanac_records(today=local_today())
    rows = [
        ("All-time high", records["all_time_high"], records["all_time_high_date"]),
        ("Best month", records["best_month_change"], records["best_month"]),
        ("Years tracked", records["years_tracked"], None),
        ("Average savings rate", csv_rate(records["average_savings_rate"]), None),
    ]
    rows.extend(("Milestone", r["threshold"], r["date"]) for r in records["milestones"])
    return csv_response("almanac-records.csv", ("Record", "Value", "Date"), rows)
