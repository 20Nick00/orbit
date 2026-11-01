"""Serialized ingestion. Commit ownership before moving; never overwrite a PDF."""

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.db import get_engine
from core.models import Paystub, PaystubLine, PaystubPending

from .config import Settings
from .parsers.pdf import UnreadablePdf, normalize, parse_pdf
from .rules import (
    GENERIC_MATCHES,
    GROSS_YTD_FELL,
    Rule,
    classify,
    load_rules,
    match_rule,
    reviewable_problem,
    save_rule,
)

LOCK = threading.RLock()
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestOutcome:
    status: str
    paystub_id: int | None = None
    pending_id: int | None = None
    reason: str | None = None


def data_path(source_file, settings):
    root = settings.data_dir.resolve()
    path = (root / source_file).resolve()
    if not path.is_relative_to(root):
        raise ValueError("File must stay inside the data folder")
    return path


def _relative(path, settings):
    return path.resolve().relative_to(settings.data_dir.resolve()).as_posix()


def _target(path, folder, digest, settings):
    folder = data_path(folder.resolve(), settings)
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", path.name).strip(".") or "paystub.pdf"
    name = re.sub(r"^[a-f0-9]{12}_", "", name)
    name = f"{digest[:12]}_{name[:100]}"
    target = folder / name
    if target.resolve() == path.resolve():
        return target
    while target.exists():
        target = folder / f"{digest[:12]}_{uuid4().hex[:12]}_{name[13:]}"
    return target


def _move(path, folder, digest, settings, session, owner=None):
    target = _target(path, folder, digest, settings)
    if owner is not None:
        # Both the old and intended location are recoverable if either commit or rename fails.
        owner.source_file = _relative(target, settings)
        session.commit()
    if path.resolve() != target.resolve():
        try:
            path.rename(target)
        except OSError:
            if owner is not None:
                owner.source_file = _relative(path, settings)
                session.commit()
            raise
    return target


def known_sha(sha):
    with LOCK, Session(get_engine()) as session:
        return any(
            session.scalar(select(model.id).where(model.file_sha256 == sha)) is not None
            for model in (Paystub, PaystubPending)
        )


def _reason(result):
    reasons = list(result.problems)
    if not result.reconciles and result.reconcile_diff is not None:
        reasons.append(f"Lines don't add up: off by ${abs(result.reconcile_diff):,.2f}")
    return "; ".join(reasons) or "The amounts needed to check this stub are missing"


def _classify(session, parsed, rule):
    previous = None
    if parsed.pay_date:
        candidates = session.scalars(
            select(Paystub)
            .where(
                Paystub.pay_date >= date(parsed.pay_date.year, 1, 1),
                Paystub.pay_date < parsed.pay_date,
            )
            .order_by(Paystub.pay_date.desc(), Paystub.gross_ytd.desc())
        )
        previous = next(
            (row for row in candidates if normalize(row.employer) == normalize(rule.employer)),
            None,
        )
    previous_ytd = {}
    if previous:
        previous_ytd = {
            normalize(line.label): line.ytd_amount
            for line in session.scalars(
                select(PaystubLine).where(PaystubLine.paystub_id == previous.id)
            )
            if line.ytd_amount is not None
        }
    result = classify(parsed, rule, previous_ytd)
    if (
        previous is not None
        and previous.gross_ytd is not None
        and result.gross_ytd is not None
        and result.gross_ytd < previous.gross_ytd
    ):
        result.problems.append(GROSS_YTD_FELL)
    return result


def _pending(session, path, digest, status, reason, employer, settings, pending=None):
    row = pending or PaystubPending(file_sha256=digest)
    row.file_sha256 = digest
    row.source_file = _relative(path, settings)
    row.status, row.reason, row.suggested_employer = status, reason, employer
    session.add(row)
    session.commit()
    folder = settings.failed_dir if status == "failed" else settings.pending_dir
    _move(path, folder, digest, settings, session, row)
    return IngestOutcome(status, pending_id=row.id, reason=reason)


def _store(session, path, digest, parsed, rule, result, settings, pending=None, accepted=False):
    existing = session.scalar(
        select(Paystub).where(
            Paystub.employer == rule.employer,
            Paystub.pay_date == parsed.pay_date,
            Paystub.gross == result.gross,
            Paystub.net == result.net,
            Paystub.gross_ytd == result.gross_ytd,
        )
    )
    if existing is not None:
        _move(path, settings.duplicates_dir, digest, settings, session, pending)
        if pending is not None:
            session.delete(pending)
            session.commit()
        return IngestOutcome("duplicate", paystub_id=existing.id)
    stub = Paystub(
        employer=rule.employer,
        pay_date=parsed.pay_date,
        period_start=parsed.period_start,
        period_end=parsed.period_end,
        gross=result.gross,
        net=result.net,
        gross_ytd=result.gross_ytd,
        net_ytd=result.net_ytd,
        file_sha256=digest,
        reconcile_diff=result.reconcile_diff if accepted and not result.reconciles else None,
        source_file=_relative(path, settings),
    )
    session.add(stub)
    session.flush()
    session.add_all(
        PaystubLine(
            paystub_id=stub.id,
            label=line.label,
            group_=line.group,
            amount=line.amount,
            ytd_amount=line.ytd,
            limit_bucket=line.limit,
        )
        for line in result.lines
    )
    session.commit()
    _move(path, settings.archive_dir / str(parsed.pay_date.year), digest, settings, session, stub)
    if pending is not None:
        session.delete(pending)
        session.commit()
    return IngestOutcome("imported", paystub_id=stub.id)


def _ingest(path, settings, session, pending=None):
    path = data_path(path, settings)
    if not path.is_file():
        if pending is not None:
            stored = session.scalar(
                select(Paystub).where(Paystub.file_sha256 == pending.file_sha256)
            )
            if stored is not None and data_path(stored.source_file, settings).is_file():
                session.delete(pending)
                session.commit()
                return IngestOutcome("duplicate", paystub_id=stored.id)
        return IngestOutcome("noop", reason="File is no longer in the inbox")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    owners = [
        session.scalar(select(model).where(model.file_sha256 == digest))
        for model in (Paystub, PaystubPending)
    ]
    existing = next((owner for owner in owners if owner is not None and owner is not pending), None)
    if isinstance(existing, PaystubPending) and existing.status in {"failed", "dismissed"}:
        pending, existing = existing, None
    if existing is not None:
        # Repair a commit-before-move interruption without leaving a broken source link.
        source = data_path(existing.source_file, settings) if existing.source_file else None
        repair = source is None or not source.exists() or source == path
        _move(
            path, settings.duplicates_dir, digest, settings, session, existing if repair else None
        )
        if pending is not None:
            session.delete(pending)
            session.commit()
        if isinstance(existing, PaystubPending):
            return IngestOutcome(
                "already_pending",
                pending_id=existing.id,
                reason="This file is already waiting for review",
            )
        return IngestOutcome("duplicate")
    try:
        parsed = parse_pdf(path)
    except UnreadablePdf as exc:
        return _pending(session, path, digest, "failed", exc.reason, None, settings, pending)
    rule = match_rule(load_rules(settings.rules_dir), parsed.text_for_matching)
    if rule is None:
        employer = next(iter(parsed.header_candidates), "")
        return _pending(
            session, path, digest, "needs_mapping", "New employer", employer, settings, pending
        )
    result = _classify(session, parsed, rule)
    if result.problems or not result.reconciles:
        return _pending(
            session, path, digest, "needs_review", _reason(result), rule.employer, settings, pending
        )
    return _store(session, path, digest, parsed, rule, result, settings, pending)


def ingest_file(path, settings):
    with LOCK, Session(get_engine()) as session:
        return _ingest(Path(path).resolve(), settings, session)


def retry_pending(settings):
    """Keep each old queue row until its replacement has been safely committed."""
    with LOCK, Session(get_engine()) as session:
        rows = list(
            session.scalars(
                select(PaystubPending).where(
                    PaystubPending.status.in_(("needs_mapping", "needs_review"))
                )
            )
        )
        for row in rows:
            try:
                _ingest(data_path(row.source_file, settings), settings, session, row)
            except Exception as exc:
                session.rollback()
                LOG.warning("Paystub retry: %s", type(exc).__name__)


def resolve_pending(
    pending_id,
    employer,
    match_text,
    labels,
    limits,
    pay_date,
    period_start,
    period_end,
    accept_mismatch,
    settings,
):
    with LOCK, Session(get_engine()) as session:
        pending = session.get(PaystubPending, pending_id)
        if pending is None:
            return IngestOutcome("error", reason="This review no longer exists")
        if pending.status not in {"needs_mapping", "needs_review"}:
            return IngestOutcome(
                "error", pending_id=pending_id, reason="This file cannot be mapped"
            )
        path = data_path(pending.source_file, settings)
        try:
            parsed = parse_pdf(path)
        except UnreadablePdf as exc:
            return IngestOutcome("error", pending_id=pending_id, reason=exc.reason)
        if len(normalize(match_text)) < 4:
            return IngestOutcome(
                "error",
                pending_id=pending_id,
                reason="Employer match text must be at least 4 characters long.",
            )
        if normalize(match_text) in GENERIC_MATCHES:
            return IngestOutcome(
                "error",
                pending_id=pending_id,
                reason="That match text is a generic statement or payroll provider name. "
                "Use text printed only on this employer's stubs, usually the company name.",
            )
        if normalize(match_text) not in normalize(parsed.text_for_matching):
            return IngestOutcome(
                "error",
                pending_id=pending_id,
                reason="That text isn't on this stub, so future stubs wouldn't be recognised",
            )
        parsed.pay_date = pay_date or parsed.pay_date
        parsed.period_start = period_start or parsed.period_start
        parsed.period_end = period_end or parsed.period_end
        if parsed.period_start and parsed.period_end and parsed.period_start > parsed.period_end:
            return IngestOutcome("error", reason="Period start must be on or before period end")
        rule = Rule(employer, [match_text], labels, limits)
        try:
            result = _classify(session, parsed, rule)
        except ValueError as exc:
            return IngestOutcome("error", pending_id=pending_id, reason=str(exc))
        blocking = [p for p in result.problems if not (accept_mismatch and reviewable_problem(p))]
        if blocking or (not result.reconciles and not accept_mismatch):
            return IngestOutcome("error", pending_id=pending_id, reason=_reason(result))
        try:
            rule = save_rule(settings.rules_dir, rule)
        except ValueError as exc:
            return IngestOutcome("error", pending_id=pending_id, reason=str(exc))
        except OSError:
            return IngestOutcome(
                "error",
                pending_id=pending_id,
                reason="Could not save the mapping rule. Check that the rules folder is writable.",
            )
        outcome = _store(
            session,
            path,
            pending.file_sha256,
            parsed,
            rule,
            result,
            settings,
            pending,
            accept_mismatch,
        )
        retry_pending(settings)
        return outcome


def dismiss_pending(pending_id, settings=None):
    settings = settings or Settings.from_env()
    with LOCK, Session(get_engine()) as session:
        row = session.get(PaystubPending, pending_id)
        if row is None:
            return IngestOutcome("noop")
        path = data_path(row.source_file, settings)
        row.status = "dismissed"
        session.commit()
        if path.exists():
            _move(path, settings.failed_dir, row.file_sha256, settings, session, row)
        return IngestOutcome("dismissed", pending_id=row.id)
