"""Private paycheck views and same-origin import/review forms."""

import re
import tempfile
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.security import require_csrf
from app.templating import templates
from core.db import get_engine
from core.models import Paystub, PaystubJobState, PaystubLine, PaystubPending
from paystubs import analysis
from paystubs.accounts import net_worth_change, spending, uncategorized_count
from paystubs.config import ROOT, Settings, load_env_file
from paystubs.limits import limits_for
from paystubs.parsers.pdf import UnreadablePdf, normalize, parse_pdf
from paystubs.rules import (
    GROSS_YTD_FELL,
    MULTIPLE_STATEMENTS,
    Rule,
    StubLine,
    get_rule,
    load_rules,
    match_rule,
    save_rule,
    suggest_group,
    suggest_limit,
)
from paystubs.service import JOB_NAME
from paystubs.store import (
    LOCK,
    data_path,
    dismiss_pending,
    ingest_file,
    resolve_pending,
    retry_pending,
)

router = APIRouter()
MAX_UPLOAD = 20 * 1024 * 1024
GROUP_OPTIONS = (
    ("gross", "Gross total"),
    ("net", "Net pay"),
    ("tax", "Tax"),
    ("pretax", "Pre-tax savings"),
    ("posttax", "Post-tax"),
    ("benefit", "Benefit"),
    ("employer", "Employer contribution"),
    ("ignore", "Ignore"),
)
GROUP_NAMES = {
    "tax": "Taxes",
    "pretax": "Pre-tax savings",
    "benefit": "Benefits",
    "posttax": "Post-tax",
    "net": "Net pay",
}
MESSAGES = {
    "imported": "Paystub imported.",
    "duplicate": "Already imported. This copy was kept in the duplicates folder.",
    "already_pending": "This file is already waiting for review",
    "failed": "This PDF could not be read. See Needs attention below.",
    "dismissed": "Dismissed. The file is kept in the failed folder.",
    "saved": "Mapping rule saved. Stubs already imported are not changed.",
    "noop": "That file is no longer in the inbox.",
}


def settings_for_request():
    load_env_file(ROOT / ".env")
    return Settings.from_env()


def money(value, cents=False, group=None):
    if value is None:
        return "—"
    if group in {"tax", "pretax", "posttax", "benefit"}:
        value = -value
    signed = group in {"tax", "pretax", "posttax", "benefit", "employer"}
    sign = "−" if value < 0 else "+" if signed else ""
    return sign + (f"${abs(value):,.2f}" if cents else f"${abs(value):,.0f}")


def percent(value):
    return "—" if value is None else f"{value * 100:.1f}%"


def ratio(value, gross):
    return value / gross if value is not None and gross else None


def render(request, name, context, status_code=200):
    return templates.TemplateResponse(
        request,
        name,
        context={"money": money, "percent": percent, **context},
        status_code=status_code,
    )


def _view(session, settings, year):
    records = list(session.scalars(select(Paystub).order_by(Paystub.pay_date, Paystub.id)))
    years = sorted({row.pay_date.year for row in records}, reverse=True)
    year = year if year is not None else (years[0] if years else date.today().year)
    lines = {}
    for line in session.scalars(select(PaystubLine).order_by(PaystubLine.id)):
        lines.setdefault(line.paystub_id, []).append(
            StubLine(line.label, line.group_, line.amount, line.ytd_amount, line.limit_bucket)
        )
    stubs = [
        analysis.Stub(
            row.employer,
            row.pay_date,
            row.period_start,
            row.period_end,
            row.gross,
            row.net,
            row.gross_ytd,
            row.net_ytd,
            lines.get(row.id, []),
            row.reconcile_diff,
        )
        for row in records
    ]
    pending = [
        {
            "id": row.id,
            "name": Path(row.source_file).name,
            "reason": row.reason,
            "status": row.status,
        }
        for row in session.scalars(
            select(PaystubPending)
            .where(PaystubPending.status != "dismissed")
            .order_by(PaystubPending.created_at, PaystubPending.id)
        )
    ]
    latest = analysis.latest_stub(stubs, year)
    context = {"year": year, "years": years, "pending": pending, "latest": latest}
    context["mailbox_enabled"] = settings.imap_enabled
    context["mailbox"] = session.get(PaystubJobState, JOB_NAME) if settings.imap_enabled else None
    context["mismatches"] = [
        stub for stub in stubs if stub.pay_date.year == year and stub.reconcile_diff is not None
    ]
    if latest is None:
        return context
    record = records[stubs.index(latest)]
    totals = analysis.ytd_totals(stubs, year)
    start = date(year, 1, 1)
    spent = spending(session, start, latest.pay_date)
    change = net_worth_change(session, date(year - 1, 12, 31), latest.pay_date)
    flow = analysis.waterfall(totals, spent.value, change.value)
    reasons = []
    if spent.reason:
        reasons.append(f"Spending: {spent.reason}")
    if spent.late_accounts:
        reasons.append(
            f"Spending may be understated: {spent.late_accounts} account(s) "
            "have their first transaction after Jan 31."
        )
    uncategorized = uncategorized_count(session, start, latest.pay_date)
    if uncategorized:
        reasons.append(
            f"Spending: {uncategorized:,} transactions have no category, so any transfers or "
            "income among them are counted in Spending and Market & home is off by the same amount."
        )
    if change.reason:
        reasons.append(f"Net worth change: {change.reason}.")
    if flow[7].missing:
        reasons.append("Market & home: needs every pay, spending and net-worth input.")
    for step in flow:
        if step.missing and step.label not in {"Spending", "Net worth change", "Market & home"}:
            reasons.append(f"{step.label}: missing paystub amounts.")
    lookup = {
        (normalize(row.employer), row.group, normalize(row.label)): row.amount
        for row in totals.lines
    }
    check_lines = [StubLine("Gross", "gross", latest.gross, latest.gross_ytd)]
    for group in analysis.GROUPS:
        check_lines.extend(line for line in latest.lines if line.group == group)
    check_lines.append(StubLine("Net", "net", latest.net, latest.net_ytd))
    table = [
        {
            "label": {"gross": "Gross pay", "net": "Net pay"}.get(line.group, line.label),
            "group": line.group,
            "amount": line.amount,
            "share": ratio(line.amount, latest.gross),
            "ytd": lookup.get((normalize(latest.employer), line.group, normalize(line.label))),
        }
        for line in check_lines
    ]
    ytd_rows = [
        {
            "label": row.label,
            "employer": row.employer,
            "amount": row.amount,
            "annualized": analysis.annualized(row.amount, latest.pay_date),
            "share": ratio(row.amount, totals.totals["gross"]),
        }
        for row in totals.lines
    ]
    context.update(
        facts=analysis.facts(stubs, year, limits_for(year), settings.hsa_coverage),
        coverage=settings.hsa_coverage,
        frequency=analysis.pay_frequency(latest),
        segments=analysis.segments(latest),
        group_names=GROUP_NAMES,
        opacities={"tax": ".9", "pretax": ".55", "benefit": ".3", "posttax": ".18", "net": "1"},
        table=table,
        ytd_rows=ytd_rows,
        multi_employer=len({normalize(row.employer) for row in totals.lines}) > 1,
        warnings=totals.warnings,
        flow=flow,
        reasons=reasons,
        unbaselined=change.unbaselined_accounts,
        source=Path(record.source_file).name if record.source_file else "Source file unavailable",
        data=[
            {
                "l": step.label,
                "l2": step.label2,
                "v": str(step.value) if step.value is not None else None,
                "total": step.is_total,
                "missing": step.missing,
            }
            for step in flow
        ],
    )
    return context


def paycheck_page(request, year=None, message=None, status_code=200):
    with Session(get_engine()) as session:
        context = _view(session, settings_for_request(), year)
    context["message"] = message or MESSAGES.get(request.query_params.get("message"))
    return render(request, "paycheck.html", context, status_code)


@router.get("/paycheck", response_class=HTMLResponse)
def paycheck(request: Request, year: int | None = None):
    if year is not None and not 2 <= year <= 9999:
        raise HTTPException(422, "Choose a valid year")
    return paycheck_page(request, year)


@router.post("/paycheck/upload", dependencies=[Depends(require_csrf)])
async def upload(request: Request, file: UploadFile):
    try:
        payload = await file.read(MAX_UPLOAD + 1)
    finally:
        await file.close()
    return await run_in_threadpool(_upload, request, payload, file.filename)


def _upload(request, payload, filename):
    if len(payload) > MAX_UPLOAD:
        return paycheck_page(request, message="Choose a PDF no larger than 20 MB.", status_code=400)
    if not payload.startswith(b"%PDF-"):
        return paycheck_page(request, message="Choose a PDF file.", status_code=400)
    settings = settings_for_request()
    folder = data_path(settings.data_dir.resolve() / "paystubs" / "uploads", settings)
    folder.mkdir(parents=True, exist_ok=True)
    name = (filename or "paystub.pdf").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip(".")[:80] or "paystub.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    with tempfile.NamedTemporaryFile(dir=folder, suffix="_" + name, delete=False) as stream:
        stream.write(payload)
        path = Path(stream.name)
    try:
        outcome = ingest_file(path, settings)
    except Exception:
        return paycheck_page(
            request,
            message="The file was saved, but could not be processed yet. "
            "Orbit will retry it on the next folder sweep.",
            status_code=503,
        )
    if outcome.status in {"needs_mapping", "needs_review"}:
        return RedirectResponse(f"/paycheck/review/{outcome.pending_id}", status_code=303)
    return RedirectResponse(f"/paycheck?message={outcome.status}", status_code=303)


def _pending_record(pending_id):
    with Session(get_engine()) as session:
        row = session.get(PaystubPending, pending_id)
        if row is None:
            raise HTTPException(404, "Review not found")
        session.expunge(row)
        return row


def _review_context(row, settings, form=None, error=None):
    parsed = None
    try:
        parsed = parse_pdf(data_path(row.source_file, settings))
    except UnreadablePdf as exc:
        error = error or exc.reason
    rule = match_rule(load_rules(settings.rules_dir), parsed.text_for_matching) if parsed else None
    matching_text = (
        next(text for text in rule.match if normalize(text) in normalize(parsed.text_for_matching))
        if rule
        else ""
    )
    values = {
        "employer": rule.employer if rule else (row.suggested_employer or ""),
        "match_text": matching_text,
        "pay_date": parsed.pay_date.isoformat() if parsed and parsed.pay_date else "",
        "period_start": parsed.period_start.isoformat() if parsed and parsed.period_start else "",
        "period_end": parsed.period_end.isoformat() if parsed and parsed.period_end else "",
    }
    if form is not None:
        values.update({key: form.get(key, "") for key in values})
    rows = []
    for i, line in enumerate(parsed.lines if parsed else []):
        key = normalize(line.label)
        group = rule.labels.get(key) if rule else None
        group = group or suggest_group(line.label) or ""
        limit = (
            rule.limits.get(key, "")
            if rule and key in rule.labels
            else (suggest_limit(line.label, group) or "")
        )
        rows.append(
            {
                "label": line.label,
                "amount": line.amount,
                "ytd": line.ytd,
                "group": form.get(f"group_{i}", "") if form is not None else group,
                "limit": form.get(f"limit_{i}", "") if form is not None else limit,
            }
        )
    return {
        "pending": row,
        "filename": Path(row.source_file).name,
        "values": values,
        "rows": rows,
        "groups": GROUP_OPTIONS,
        "error": error,
        "mismatch": any(
            text in (error or row.reason)
            for text in ("off by $", GROSS_YTD_FELL, MULTIPLE_STATEMENTS, "appears twice")
        ),
        "can_map": parsed is not None and row.status in {"needs_mapping", "needs_review"},
        "data": [str(row["amount"]) if row["amount"] is not None else None for row in rows],
    }


@router.get("/paycheck/review/{pending_id}", response_class=HTMLResponse)
def review(request: Request, pending_id: int):
    return render(
        request,
        "paycheck_review.html",
        _review_context(_pending_record(pending_id), settings_for_request()),
    )


def _form_mappings(form, expected):
    labels, limits = {}, {}
    for i, label in enumerate(expected):
        if form.get(f"label_{i}") != label:
            raise ValueError("The labels changed. Reload the review and try again.")
        group = form.get(f"group_{i}", "")
        if group not in dict(GROUP_OPTIONS):
            raise ValueError("Choose a group for every label.")
        labels[label] = group
        limit = form.get(f"limit_{i}", "")
        if limit:
            limits[label] = limit
    return labels, limits


@router.post("/paycheck/review/{pending_id}", dependencies=[Depends(require_csrf)])
async def save_review(request: Request, pending_id: int):
    form = await request.form()
    return await run_in_threadpool(_save_review, request, pending_id, form)


def _save_review(request, pending_id, form):
    settings = settings_for_request()
    with LOCK:
        row = _pending_record(pending_id)
        context = _review_context(row, settings, form)
        try:
            labels, limits = _form_mappings(form, [line["label"] for line in context["rows"]])
            dates = [
                date.fromisoformat(form[key]) if form.get(key) else None
                for key in ("pay_date", "period_start", "period_end")
            ]
            outcome = resolve_pending(
                pending_id,
                form.get("employer", ""),
                form.get("match_text", ""),
                labels,
                limits,
                *dates,
                form.get("accept_mismatch") == "on",
                settings,
            )
            error = outcome.reason if outcome.status == "error" else None
        except ValueError as exc:
            error = (
                str(exc)
                if str(exc).startswith(("The labels changed.", "Choose a group"))
                else "Check the dates and choose a group for every label."
            )
        if error:
            return render(
                request, "paycheck_review.html", _review_context(row, settings, form, error), 400
            )
    return RedirectResponse(f"/paycheck?message={outcome.status}", status_code=303)


@router.post("/paycheck/review/{pending_id}/dismiss", dependencies=[Depends(require_csrf)])
def dismiss(pending_id: int):
    _pending_record(pending_id)
    dismiss_pending(pending_id, settings_for_request())
    return RedirectResponse("/paycheck?message=dismissed", status_code=303)


@router.get("/paycheck/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    settings = settings_for_request()
    rules = []
    for path in sorted(settings.rules_dir.glob("*.yaml")):
        if path.name == "example.yaml":
            continue
        try:
            rule = get_rule(settings.rules_dir, path.stem)
        except ValueError:
            rule = None
        rules.append({"slug": path.stem, "file": path.name, "rule": rule})
    return render(request, "paycheck_rules.html", {"rules": rules, "editing": False})


def _rule(slug, settings):
    try:
        rule = get_rule(settings.rules_dir, slug)
    except ValueError:
        rule = None
    if rule is None:
        raise HTTPException(404, "Mapping rule not found")
    return rule


def _rule_context(slug, rule, form=None, error=None):
    return {
        "editing": True,
        "slug": slug,
        "rule": rule,
        "error": error,
        "groups": GROUP_OPTIONS,
        "rows": [
            {
                "label": label,
                "group": form.get(f"group_{i}", "") if form is not None else group,
                "limit": (
                    form.get(f"limit_{i}", "") if form is not None else rule.limits.get(label, "")
                ),
            }
            for i, (label, group) in enumerate(rule.labels.items())
        ],
    }


@router.get("/paycheck/rules/{slug}", response_class=HTMLResponse)
def edit_rule(request: Request, slug: str):
    return render(
        request, "paycheck_rules.html", _rule_context(slug, _rule(slug, settings_for_request()))
    )


@router.post("/paycheck/rules/{slug}", dependencies=[Depends(require_csrf)])
async def update_rule(request: Request, slug: str):
    form = await request.form()
    return await run_in_threadpool(_update_rule, request, slug, form)


def _update_rule(request, slug, form):
    settings = settings_for_request()
    with LOCK:
        rule = _rule(slug, settings)
        try:
            labels, limits = _form_mappings(form, rule.labels)
            save_rule(
                settings.rules_dir,
                Rule(rule.employer, rule.match, labels, limits),
                slug=slug,
                replace_mappings=True,
            )
        except ValueError as exc:
            return render(
                request, "paycheck_rules.html", _rule_context(slug, rule, form, str(exc)), 400
            )
        except OSError:
            return render(
                request,
                "paycheck_rules.html",
                _rule_context(
                    slug,
                    rule,
                    form,
                    "Could not save the mapping rule. Check that the rules folder is writable.",
                ),
                400,
            )
        retry_pending(settings)
    return RedirectResponse("/paycheck?message=saved", status_code=303)
