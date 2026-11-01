from fastapi import APIRouter, Request

from app.csv_export import csv_response
from app.reporting import chart_rows, rollup_csv, trends_start
from app.templating import templates
from core.clock import local_today
from core.db import session_scope
from core.rollups import _data, category_spend, investments_vs_cash, monthly_rollups

router = APIRouter()


def _trends(active, today):
    loaded = _data(active, today)
    start = trends_start(active, today, _loaded=loaded)
    end = today.strftime("%Y-%m")
    return dict(
        months=monthly_rollups(start, end, today=today, session=active, _loaded=loaded)
        if start
        else [],
        categories=category_spend(today=today, session=active, _loaded=loaded),
        mix=investments_vs_cash(start, end, today=today, session=active, _loaded=loaded)
        if start
        else [],
    )


@router.get("/trends")
def trends(request: Request):
    with session_scope() as active:
        data = _trends(active, local_today())
        return templates.TemplateResponse(
            request,
            "trends.html",
            {
                **data,
                "monthly_json": chart_rows(data["months"]),
                "mix_json": chart_rows(data["mix"]),
                "categories_json": chart_rows(data["categories"]),
            },
        )


@router.get("/export/trends-monthly.csv")
def monthly_export():
    with session_scope() as active:
        return rollup_csv("trends-monthly.csv", _trends(active, local_today())["months"])


@router.get("/export/trends-categories.csv")
def categories_export():
    with session_scope() as active:
        rows = category_spend(today=local_today(), session=active)
        return csv_response(
            "trends-categories.csv",
            ("Category", "Spending"),
            ((r["category"], r["spending"]) for r in rows),
        )


@router.get("/export/trends-mix.csv")
def mix_export():
    with session_scope() as active:
        rows = _trends(active, local_today())["mix"]
        return csv_response(
            "trends-mix.csv",
            ("Month", "Investments", "Cash", "Closed"),
            ((r["month"], r["investments"], r["cash"], r["closed"]) for r in rows),
        )
