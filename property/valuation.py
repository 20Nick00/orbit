"""Property automation, explicit overrides, and linked home equity."""

import logging
from contextlib import nullcontext
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from core.clock import local_today
from core.config import get_settings
from core.db import session_scope, write_savepoint
from core.equity import equity_summary
from core.models import (
    Account,
    AccountFact,
    BalanceSnapshot,
    RentcastAttempts,
    RentcastCache,
    Valuation,
    _money,
)
from core.snapshots import _check_currency, record_daily
from property.rentcast import (
    NotConfiguredError,
    QuotaExceededError,
    RentCastAuthError,
    RentCastClient,
    _store_estimate,
    address_key,
    usage_total,
)

logger = logging.getLogger(__name__)


def _property(active, account_id):
    account = active.get(Account, account_id)
    if account is None or account.type != "property":
        raise ValueError("Property account not found.")
    return account


def _fact(active, account_id, key):
    row = active.get(AccountFact, (account_id, key))
    return row.value if row else None


def _latest(active, account_id, today=None):
    today = today or local_today()
    query = select(BalanceSnapshot).where(
        BalanceSnapshot.account_id == account_id, BalanceSnapshot.date <= today
    )
    return active.scalar(
        query.order_by(BalanceSnapshot.date.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )


def _overridden(active, account_id, snapshot, today=None):
    today = today or local_today()
    return bool(
        snapshot is not None
        and snapshot.source != "rentcast"
        and snapshot.date <= today
        and active.scalar(
            select(Valuation.account_id).where(
                Valuation.account_id == account_id, Valuation.date <= today
            )
        )
        is not None
    )


def _write(active, account_id, value, today, source):
    statement = insert(Valuation).values(
        account_id=account_id, date=today, source=source, value=value
    )
    active.scalars(
        statement.on_conflict_do_update(
            index_elements=["account_id", "date", "source"],
            set_={"value": statement.excluded.value},
        ).returning(Valuation),
        execution_options={"populate_existing": True},
    ).one()
    record_daily(account_id, today, value, source=source, session=active)


def set_override(account_id, value, *, today, session=None):
    value = _money(value)
    if value <= 0:
        raise ValueError("Enter a value greater than zero.")
    with nullcontext(session) if session is not None else session_scope() as active:
        _property(active, account_id)
        with write_savepoint(active):
            _write(active, account_id, value, today, "manual")


def refresh(account_id, *, today, client=None, session=None, _job=False):
    with nullcontext(session) if session is not None else session_scope() as active:
        _property(active, account_id)
        address = _fact(active, account_id, "address")
        if not address or not address.strip():
            raise ValueError("Add a property address before refreshing.")
        client = client or RentCastClient(get_settings().rentcast_api_key)
        # Reserve quota before opening a SQLite write/savepoint transaction.
        # No valuation or balance is touched until a complete estimate is available.
        if isinstance(client, RentCastClient):
            estimate = client._estimate(address, today=today, session=active, persist=False)
        else:
            estimate = client.estimate(address, today=today, session=active)
        with write_savepoint(active):
            if _job and _overridden(active, account_id, _latest(active, account_id, today), today):
                return None
            _store_estimate(active, address, today, estimate)
            _write(active, account_id, estimate.price, today, "rentcast")
        return estimate


@dataclass
class RefreshSummary:
    refreshed: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    stopped: str | None = None


def _reserve_attempt(active, address, today):
    """Commit job throttling separately, so a failed request cannot undo it."""
    bind = active.get_bind()
    with Session(getattr(bind, "engine", bind)) as attempts, attempts.begin():
        statement = insert(RentcastAttempts).values(
            address_key=address_key(address),
            month=today.strftime("%Y-%m"),
            attempts=1,
            last_attempt=today,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["address_key", "month"],
            set_={"attempts": RentcastAttempts.attempts + 1, "last_attempt": today},
            where=(RentcastAttempts.attempts < 3) & (RentcastAttempts.last_attempt < today),
        ).returning(RentcastAttempts.attempts)
        return attempts.scalar(statement) is not None


def refresh_due(today, *, client=None):
    summary = RefreshSummary()
    if client is None and not get_settings().rentcast_api_key:
        logger.warning("RentCast is not configured; property refresh skipped.")
        summary.stopped = "not_configured"
        return summary
    with session_scope() as active:
        ids = list(
            active.scalars(
                select(Account.id)
                .where(Account.hidden.is_(False), Account.type == "property")
                .order_by(Account.id)
            )
        )
    for account_id in ids:
        try:
            with session_scope() as active:
                address = _fact(active, account_id, "address")
                already = active.scalar(
                    select(Valuation.account_id).where(
                        Valuation.account_id == account_id,
                        Valuation.source == "rentcast",
                        Valuation.date >= today.replace(day=1),
                        Valuation.date <= today,
                    )
                )
                if (
                    not address
                    or not address.strip()
                    or already is not None
                    or _overridden(active, account_id, _latest(active, account_id, today), today)
                ):
                    summary.skipped.append(account_id)
                    continue
                # At the ceiling no request can be sent, so don't use up an attempt.
                limit = min(50, get_settings().rentcast_monthly_limit)
                if active.scalar(usage_total(today)) >= limit:
                    raise QuotaExceededError("RentCast request limit reached.")
                if not _reserve_attempt(active, address, today):
                    summary.skipped.append(account_id)
                    continue
                result = refresh(account_id, today=today, client=client, session=active, _job=True)
                if result is None:
                    summary.skipped.append(account_id)
                    continue
            summary.refreshed.append(account_id)
        except (QuotaExceededError, NotConfiguredError, RentCastAuthError) as exc:
            summary.stopped = (
                "quota"
                if isinstance(exc, QuotaExceededError)
                else "auth"
                if isinstance(exc, RentCastAuthError)
                else "not_configured"
            )
            logger.warning(
                "Property refresh stopped for account %s (%s).", account_id, type(exc).__name__
            )
            break
        except Exception as exc:
            summary.failed.append(account_id)
            logger.warning(
                "Property refresh failed for account %s (%s).", account_id, type(exc).__name__
            )
    return summary


def valuation_status(account_id, session=None, today=None):
    today = today or local_today()
    with nullcontext(session) if session is not None else session_scope() as active:
        _property(active, account_id)
        snapshot = _latest(active, account_id, today)
        overridden = _overridden(active, account_id, snapshot, today)
        address = _fact(active, account_id, "address")
        cache = None
        if snapshot and snapshot.source == "rentcast" and address:
            cache = active.get(
                RentcastCache, (address_key(address), snapshot.date.strftime("%Y-%m"))
            )
        label = None
        if snapshot:
            label = (
                "RentCast estimate"
                if snapshot.source == "rentcast"
                else "Manual override"
                if overridden
                else "Manual value"
            )
        return dict(
            value=snapshot.balance if snapshot else None,
            date=snapshot.date if snapshot else None,
            source=snapshot.source if snapshot else None,
            label=label,
            is_override=overridden,
            low=cache.price_low if cache else None,
            high=cache.price_high if cache else None,
            address=address,
            linked_liability=_fact(active, account_id, "linked_liability"),
            configured=bool(
                get_settings().rentcast_api_key
                and get_settings().rentcast_api_key.get_secret_value()
            ),
        )


def home_equity(account_id, session=None, today=None):
    with nullcontext(session) if session is not None else session_scope() as active:
        _property(active, account_id)
        summary = home_equity_summary(active, today, _account_id=account_id)
        return next((row for row in summary["rows"] if row["account_id"] == account_id), None)


def home_equity_summary(session=None, today=None, *, _account_id=None):
    today = today or local_today()
    with nullcontext(session) if session is not None else session_scope() as active:
        accounts = {
            a.id: a for a in active.scalars(select(Account).where(Account.hidden.is_(False)))
        }
        links = dict(
            active.execute(
                select(AccountFact.account_id, AccountFact.value).where(
                    AccountFact.key == "linked_liability"
                )
            ).all()
        )
        balances = {}
        for row in active.execute(
            select(BalanceSnapshot.account_id, BalanceSnapshot.balance)
            .where(BalanceSnapshot.date <= today)
            .order_by(BalanceSnapshot.date)
        ):
            balances[row.account_id] = row.balance
        if _account_id is not None:
            accounts = {
                aid: a for aid, a in accounts.items() if aid == _account_id or a.type != "property"
            }
        result = equity_summary(accounts, balances, links)
        _check_currency([r["account_id"] for r in result["rows"] if r["status"] == "ok"], accounts)
        return result
