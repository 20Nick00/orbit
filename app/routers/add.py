"""Add sources, private file review, and manual account forms."""

import json
import re
import secrets
import time
from datetime import date
from pathlib import Path
from threading import RLock
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from aggregators import csv_import, ofx_import
from aggregators.file_store import ImportError, is_plaid_linked, require_new_name, valid_target
from app.formatting import parse_balance
from app.routers.dashboard import get_today
from app.security import require_csrf
from app.templating import templates
from core.db import get_session, session_scope, write_savepoint
from core.manual import create_manual_account, update_manual_account
from core.models import Account, AccountFact, BalanceSnapshot
from core.types import ACCOUNT_TYPES, infer_type, is_liability_type
from paystubs.config import Settings

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]
TodayDep = Annotated[date, Depends(get_today)]
MAX_UPLOAD = 10 * 1024 * 1024
TOKEN = re.compile(r"[0-9a-f]{32}")
LOCK = RLock()


def _render(request, template, **context):
    return templates.TemplateResponse(request, template, context)


def _targets(session):
    return [
        a
        for a in session.scalars(select(Account).order_by(Account.name))
        if valid_target(session, a)
    ]


@router.get("/add")
def add_page(request: Request):
    with LOCK:
        _purge()
    return _render(request, "add.html")


@router.get("/add/import")
def import_page(request: Request, session: SessionDep):
    with LOCK:
        _purge()
    return _render(request, "import.html", accounts=_targets(session), types=ACCOUNT_TYPES)


def _inbox():
    folder = Settings.from_env().data_dir / "inbox" / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _paths(token):
    if not TOKEN.fullmatch(token):
        raise HTTPException(400, "Invalid import token.")
    folder = _inbox()
    return folder / f"{token}.upload", folder / f"{token}.json"


def _purge():
    for path in _inbox().iterdir():
        if path.suffix in {".upload", ".json"} and TOKEN.fullmatch(path.stem):
            if path.is_file() and path.stat().st_mtime < time.time() - 24 * 60 * 60:
                path.unlink(missing_ok=True)


def _hold(data, metadata):
    token = secrets.token_hex(16)
    raw, meta = _paths(token)
    raw.write_bytes(data)
    meta.write_text(json.dumps(metadata), encoding="utf-8")
    return token


def _load(token):
    raw, meta = _paths(token)
    if not raw.is_file() or not meta.is_file() or raw.stat().st_mtime < time.time() - 86400:
        raise ImportError("This import expired or was already completed. Upload the file again.")
    return raw.read_bytes(), json.loads(meta.read_text(encoding="utf-8"))


def _remove(token):
    for path in _paths(token):
        path.unlink(missing_ok=True)


def _csv_target(session, metadata):
    target = metadata.get("account_id", "new")
    if target != "new":
        try:
            account = session.get(Account, int(target))
        except (ValueError, TypeError):
            account = None
        if account is None or not valid_target(session, account):
            raise ImportError("Choose an account that is neither manual nor linked through Plaid.")
        return account
    name = metadata.get("name", "").strip()
    if not name:
        raise ImportError("Enter a name for the new account.")
    require_new_name(session, name)
    kind = metadata.get("type") or infer_type("csv", None, None, name, session=session)
    if kind == "other" and not metadata.get("type"):
        raise ImportError(
            f'Orbit can\'t tell what kind of account "{name}" is. '
            "Choose a type so debts land on the right side."
        )
    if kind not in ACCOUNT_TYPES:
        raise ImportError("Choose a valid account type.")
    return Account(name=name, type=kind, is_liability=is_liability_type(kind), is_manual=False)


def _process(request, data, metadata, token=None, mapping=None, choices=None, confirmed=False):
    with LOCK:
        with session_scope() as session:
            context = dict(
                filename=metadata["filename"], accounts=_targets(session), roles=csv_import.ROLES
            )
            if metadata["kind"] == "csv":
                account = _csv_target(session, metadata)
                analysis = csv_import.analyze(
                    data, account, session=session, mapping=mapping, confirmed=confirmed
                )
                if analysis.ambiguities:
                    context.update(panel="csv", analysis=analysis, account=account)
                else:
                    with write_savepoint(session):
                        session.add(account)
                        session.flush()
                        result = csv_import.import_csv(
                            session, account, data, mapping=analysis.mapping, confirmed=confirmed
                        )
                    context.update(panel="result", results=[result])
            else:
                statements = ofx_import.analyze(data, session=session, choices=choices)
                if any(s.needs_target or s.needs_sign for s in statements):
                    context.update(panel="ofx", statements=statements, choices=choices or {})
                else:
                    context.update(
                        panel="result",
                        results=ofx_import.import_ofx(session, data, choices=choices),
                    )
            if context["panel"] != "result":
                context["token"] = token or _hold(data, metadata)
                if confirmed:
                    context["error"] = (
                        "This mapping still has unreadable or unclear values. "
                        "Check the questions below."
                    )
            # Render while ORM objects are attached, but remove the held file only after commit.
            template = (
                "_import_panel.html"
                if request.headers.get("HX-Request") == "true"
                else "import_response.html"
            )
            response = _render(request, template, **context)
        if context["panel"] == "result" and token:
            try:
                _remove(token)
            except OSError:
                context["results"][0].notices.append(
                    "Imported successfully. The temporary file could not be removed; "
                    "Orbit will retry cleanup after 24 hours on a later upload."
                )
                response = _render(request, template, **context)
        return response


def _error(request, message):
    if request.headers.get("HX-Request") == "true":
        return _render(request, "_import_panel.html", panel="error", error=message)
    with session_scope() as session:
        return _render(
            request,
            "import.html",
            panel="error",
            error=message,
            accounts=_targets(session),
            types=ACCOUNT_TYPES,
        )


@router.post("/add/import", dependencies=[Depends(require_csrf)])
async def upload(request: Request):
    length = request.headers.get("Content-Length", "")
    if length.isdigit() and int(length) > MAX_UPLOAD:
        return _error(request, "Files must be 10 MB or smaller.")
    try:
        return await _upload(request)
    except StarletteHTTPException as exc:
        if exc.status_code not in {400, 413}:
            raise
        return _error(
            request,
            "Files must be 10 MB or smaller."
            if "size" in str(exc.detail).lower() or exc.status_code == 413
            else "Choose a CSV, OFX or QFX file.",
        )


async def _upload(request):
    async with request.form(max_files=1, max_fields=20, max_part_size=MAX_UPLOAD) as form:
        file = form.get("file")
        if file is None or not hasattr(file, "read"):
            return _error(request, "Choose a CSV, OFX or QFX file.")
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            return _error(request, "Files must be 10 MB or smaller.")
        filename = (file.filename or "import").replace("\\", "/").split("/")[-1]
        suffix = Path(filename).suffix.lower()
        if suffix not in {".csv", ".ofx", ".qfx"}:
            return _error(request, "Choose a CSV, OFX or QFX file.")
        metadata = dict(
            filename=filename,
            kind="csv" if suffix == ".csv" else "ofx",
            account_id=str(form.get("account_id", "new")),
            name=str(form.get("name", "")),
            type=str(form.get("type", "")),
        )
    with LOCK:
        _purge()
    try:
        return await run_in_threadpool(_process, request, data, metadata)
    except ImportError as exc:
        return _error(request, str(exc))
    except (SQLAlchemyError, OSError):
        return _error(
            request, "The import could not be saved. No database changes were made. Try again."
        )


@router.post("/add/import/confirm", dependencies=[Depends(require_csrf)])
async def confirm(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    # Validate before touching the filesystem; metadata never comes from the browser.
    if not TOKEN.fullmatch(token):
        raise HTTPException(400, "Invalid import token.")

    def finish():
        with LOCK:
            try:
                data, metadata = _load(token)
            except ImportError as exc:
                return _error(request, str(exc))
            mapping, choices = None, None
            if metadata["kind"] == "csv":
                try:
                    roles = {
                        role: int(form[role])
                        for role in csv_import.ROLES
                        if form.get(role, "") != ""
                    }
                except (ValueError, TypeError):
                    raise HTTPException(400, "Choose valid columns.") from None
                mapping = dict(
                    roles=roles,
                    date_format=str(form.get("date_format", "auto")),
                    decimal=str(form.get("decimal", ".")),
                    liability_amount_sign=form.get("liability_amount_sign") or None,
                    liability_balance_sign=form.get("liability_balance_sign") or None,
                    amounts_signed=form.get("amounts_signed") == "on",
                )
            else:
                choices = {}
                for key, value in form.items():
                    match = re.fullmatch(r"(account_id|liability_balance_sign)_(\d+)", key)
                    if match and value:
                        choices.setdefault(match[2], {})[match[1]] = str(value)
            try:
                return _process(request, data, metadata, token, mapping, choices, True)
            except ImportError as exc:
                return _error(request, str(exc))
            except (SQLAlchemyError, OSError):
                return _error(
                    request,
                    "The import could not be saved. No database changes were made. "
                    "Upload the file again to retry.",
                )

    return await run_in_threadpool(finish)


def _manual_context(account=None, session=None, preset=""):
    values = dict(
        name="",
        type=preset if preset in ACCOUNT_TYPES else "",
        value="",
        note="",
        is_liability=False,
    )
    if account is not None:
        snapshot = session.scalar(
            select(BalanceSnapshot)
            .where(BalanceSnapshot.account_id == account.id)
            .order_by(BalanceSnapshot.date.desc())
            .limit(1)
        )
        note = session.get(AccountFact, (account.id, "note"))
        values.update(
            name=account.name,
            type=account.type,
            value=f"{snapshot.balance:.2f}" if snapshot else "",
            note=note.value if note else "",
            is_liability=account.is_liability,
        )
    return values


def _manual_account(session, account_id):
    account = session.get(Account, account_id)
    if account is None or is_plaid_linked(session, account):
        raise HTTPException(404, "Account not found.")
    return account


@router.get("/add/manual")
def manual_page(request: Request):
    return _render(
        request,
        "manual.html",
        values=_manual_context(preset=request.query_params.get("type", "")),
        types=ACCOUNT_TYPES,
        action="/add/manual",
        account=None,
    )


@router.get("/account/{account_id}/manual")
def manual_edit(request: Request, account_id: int, session: SessionDep):
    account = _manual_account(session, account_id)
    return _render(
        request,
        "manual.html",
        values=_manual_context(account, session),
        types=ACCOUNT_TYPES,
        action=f"/account/{account_id}/manual",
        account=account,
    )


async def _save_manual(request, session, today, account=None):
    form = await request.form()
    values = {key: str(form.get(key, "")) for key in ("name", "type", "value", "note")}
    values["is_liability"] = form.get("is_liability") == "on"
    try:
        kwargs = dict(
            name=values["name"],
            type=values["type"],
            value=parse_balance(values["value"]),
            note=values["note"],
            today=today,
            is_liability=values["is_liability"],
            session=session,
        )
        saved = (
            update_manual_account(account.id, **kwargs)
            if account
            else create_manual_account(**kwargs)
        )
        session.commit()
        return RedirectResponse(f"/account/{saved.id}", status_code=303)
    except ValueError as exc:
        session.rollback()
        error = str(exc)
    except SQLAlchemyError:
        session.rollback()
        error = "The account could not be saved. No changes were made."
    return _render(
        request,
        "manual.html",
        values=values,
        types=ACCOUNT_TYPES,
        action=str(request.url.path),
        account=account,
        error=error,
    )


@router.post("/add/manual", dependencies=[Depends(require_csrf)])
async def manual_create(request: Request, session: SessionDep, today: TodayDep):
    return await _save_manual(request, session, today)


@router.post("/account/{account_id}/manual", dependencies=[Depends(require_csrf)])
async def manual_update(request: Request, account_id: int, session: SessionDep, today: TodayDep):
    return await _save_manual(request, session, today, _manual_account(session, account_id))
