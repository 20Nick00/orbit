"""Daily Plaid synchronization with independent, committed product steps."""

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select

from aggregators.locks import item_lock
from aggregators.plaid import PlaidAggregator, PlaidError
from core.clock import local_today
from core.crypto import CryptoError
from core.db import session_scope
from core.models import PlaidItem
from core.snapshots import record_daily

logger = logging.getLogger(__name__)


def _failure(item_id, exc):
    with session_scope() as session:
        row = session.get(PlaidItem, item_id)
        if row is None:
            return
        code = (
            exc.error_code
            if isinstance(exc, PlaidError)
            else "TOKEN_UNREADABLE"
            if isinstance(exc, CryptoError)
            else "SYNC_FAILED"
        )
        row.status = (
            "login_required"
            if code
            in {
                "ITEM_LOGIN_REQUIRED",
                "ITEM_LOCKED",
                "INVALID_CREDENTIALS",
                "INVALID_MFA",
                "USER_PERMISSION_REVOKED",
                "ACCESS_NOT_GRANTED",
            }
            else "error"
        )
        row.last_error_code = code
    # Provider metadata and request IDs are untrusted, even if they look alphanumeric.
    logger.warning("Plaid item=%s error_code=%s failed: %s", item_id, code, type(exc).__name__)


def sync_item(item_id, today=None, client=None):
    today = today or local_today()
    aggregator = PlaidAggregator(client)
    with item_lock(item_id):
        try:
            with session_scope() as session:
                if session.get(PlaidItem, item_id) is None:
                    return False
                balances = aggregator.sync_accounts(item_id, session=session, record_balances=False)
                for balance in balances:
                    record_daily(
                        balance.account_id, today, balance.balance, source="plaid", session=session
                    )
            aggregator.sync_transactions(item_id)
            aggregator.sync_holdings(item_id, today=today)
            aggregator.sync_liabilities(item_id)
            with session_scope() as session:
                row = session.get(PlaidItem, item_id)
                row.status = "ok"
                row.last_error_code = None
                row.last_synced_at = dt.datetime.combine(today, dt.datetime.now().time())
            return True
        except Exception as exc:
            _failure(item_id, exc)
            return False


def sync_transactions(item_id, client=None):
    """Webhook work shares the same item lock as full sync and removal."""
    with item_lock(item_id):
        try:
            with session_scope() as session:
                if session.get(PlaidItem, item_id) is None:
                    return
                PlaidAggregator(client).sync_transactions(item_id, session=session)
        except Exception as exc:
            _failure(item_id, exc)


@dataclass
class SyncSummary:
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0


def run(today=None, client=None, skip_synced_today=False):
    today = today or local_today()
    summary = SyncSummary()
    with session_scope() as session:
        items = list(session.execute(select(PlaidItem.id, PlaidItem.last_synced_at)))
    for item_id, synced in items:
        if skip_synced_today and synced is not None and synced.date() == today:
            summary.skipped += 1
            continue
        try:
            success = sync_item(item_id, today=today, client=client)
        except Exception:
            success = False
        if success:
            summary.succeeded += 1
        else:
            summary.failed += 1
    logger.info(
        "Plaid sync succeeded=%s failed=%s skipped=%s",
        summary.succeeded,
        summary.failed,
        summary.skipped,
    )
    return summary
