import logging
from contextlib import nullcontext

from sqlalchemy import func, select

from core.clock import local_today
from core.db import session_scope, write_savepoint
from core.models import BalanceSnapshot, MonthEndReopen, MonthEndSnapshot
from core.snapshots import close_month

logger = logging.getLogger(__name__)


def close_due_months(today, session=None):
    with nullcontext(session) if session is not None else session_scope() as active:
        months_with_data = set(
            active.scalars(
                select(func.strftime("%Y-%m", BalanceSnapshot.date))
                .where(BalanceSnapshot.date < today.replace(day=1))
                .distinct()
            )
        )
        existing = set(active.scalars(select(MonthEndSnapshot.month).distinct()))
        # A reopened month waits for the person correcting it to close it again.
        existing |= set(active.scalars(select(MonthEndReopen.month).distinct()))
    closed = []
    for month in sorted(months_with_data - existing):
        try:
            if session is None:
                count = close_month(month, today=today, skip_incomplete_files=True)
            else:
                with write_savepoint(session):
                    count = close_month(
                        month, today=today, session=session, skip_incomplete_files=True
                    )
            if count > 0:
                closed.append(month)
        except Exception as exc:
            logger.warning("Month close failed for %s (%s).", month, type(exc).__name__)
    return closed


def run():
    try:
        close_due_months(local_today())
    except Exception as exc:
        logger.warning("Month close job failed (%s).", type(exc).__name__)
