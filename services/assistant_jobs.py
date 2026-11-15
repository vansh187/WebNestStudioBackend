"""Daily cleanup of old assistant_call_log rows and of spent VS Code logins. A failing run is
logged, never fatal."""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database.assistant_usage_persistence import AssistantUsageStore
from services.extension_auth_service import ExtensionAuthService

logger = logging.getLogger("webnest.assistant")

FIRST_RUN_DELAY = timedelta(minutes=3)


async def purge_call_log(store: AssistantUsageStore, retention_days: int) -> None:
    try:
        removed = await store.purge_call_log(datetime.now(timezone.utc) - timedelta(days=retention_days))
        if removed:
            logger.info("Assistant call log purge removed %s rows", removed)
    except Exception as exc:
        logger.error("Assistant call log purge failed: %s", type(exc).__name__)


def register_assistant_jobs(scheduler: AsyncIOScheduler, store: AssistantUsageStore, retention_days: int) -> None:
    """Zero or less means keep every row, so there is nothing to schedule."""
    if retention_days <= 0:
        return
    scheduler.add_job(
        purge_call_log,
        IntervalTrigger(hours=24),
        args=[store, retention_days],
        id="assistant_call_log_purge",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + FIRST_RUN_DELAY,
        misfire_grace_time=3600,
        coalesce=True,
    )


async def purge_extension_sessions(auth: ExtensionAuthService) -> None:
    """VS Code login codes and refresh tokens that expired over a day ago, and sessions that
    ended over 30 days ago. Never fatal."""
    try:
        removed = await auth.purge()
        if removed:
            logger.info("VS Code session purge removed %s rows", removed)
    except Exception as exc:
        logger.error("VS Code session purge failed: %s", type(exc).__name__)


def register_extension_jobs(scheduler: AsyncIOScheduler, auth: ExtensionAuthService) -> None:
    scheduler.add_job(
        purge_extension_sessions,
        IntervalTrigger(hours=24),
        args=[auth],
        id="extension_session_purge",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + FIRST_RUN_DELAY,
        misfire_grace_time=3600,
        coalesce=True,
    )
