"""Daily cleanup of relay quota rows older than 30 days (section 5). A failing
run is logged, never fatal."""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database.session import Database
from codelab_relay.dependencies import purge_old_usage

logger = logging.getLogger("webnest.codelab_relay")

FIRST_RUN_DELAY = timedelta(minutes=3)


async def purge_relay_usage(database: Database) -> None:
    try:
        removed = await purge_old_usage(database)
        if removed:
            logger.info("relay usage purge removed %s rows", removed)
    except Exception:
        logger.error("relay usage purge job failed", exc_info=True)


def register_relay_jobs(scheduler: AsyncIOScheduler, database: Database) -> None:
    try:
        scheduler.add_job(
            purge_relay_usage,
            IntervalTrigger(hours=24),
            args=[database],
            id="codelab_relay_usage_purge",
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc) + FIRST_RUN_DELAY,
        )
    except Exception:
        logger.critical("could not register the relay usage purge job", exc_info=True)
        raise
