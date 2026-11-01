from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from core.clock import local_timezone
from jobs import avm_refresh, backup, month_end, nightly


def build_scheduler(settings):
    timezone = local_timezone(settings)
    scheduler = BackgroundScheduler(timezone=timezone)
    common = dict(coalesce=True, max_instances=1, misfire_grace_time=None)
    # Use the same job for startup and cron so max_instances also prevents overlap.
    startup = datetime.now(timezone) + timedelta(seconds=5)
    scheduler.add_job(
        backup.run,
        "cron",
        id="backup",
        hour=1,
        minute=30,
        next_run_time=startup,
        **common,
    )
    scheduler.add_job(
        month_end.run,
        "cron",
        id="month_end",
        day=1,
        hour=2,
        minute=30,
        next_run_time=startup,
        **common,
    )
    scheduler.add_job(
        avm_refresh.run,
        "cron",
        id="avm_refresh",
        day=1,
        hour=3,
        minute=0,
        next_run_time=startup,
        **common,
    )
    if settings.plaid_configured:
        # The startup run catches up when the PC was off at 02:00.
        scheduler.add_job(
            nightly.run,
            "cron",
            id="plaid_nightly",
            hour=2,
            minute=0,
            kwargs={"skip_synced_today": True},
            next_run_time=startup,
            **common,
        )
    return scheduler
