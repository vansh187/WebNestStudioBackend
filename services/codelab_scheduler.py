import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from core.config import Settings
from database.session import Database
from services.blog_scheduler import IST
from services.codelab_generation_service import DIFFICULTIES, CodelabGenerationService

logger = logging.getLogger("webnest.codelab_scheduler")


def register_codelab_jobs(scheduler: AsyncIOScheduler, database: Database, settings: Settings) -> None:
    """Registers the daily problem-generation job. The cron fires every day;
    the job itself decides which difficulties are due from the generation
    log, so codelab_generation_interval_days (1 = daily, 2 = every other day)
    is honoured and a restart or a failed difficulty never double-publishes."""
    if not settings.codelab_generation_enabled:
        logger.info("CodeLab generation is disabled (codelab_generation_enabled=false) - no scheduler job registered")
        return
    scheduler.add_job(
        run_scheduled_generation,
        trigger=CronTrigger(hour=settings.codelab_generation_hour_ist, minute=0, timezone=IST),
        args=[database, settings],
        id="codelab_problem_generation",
        replace_existing=True,
        misfire_grace_time=3600,
    )


async def run_scheduled_generation(database: Database, settings: Settings) -> None:
    try:
        async with database.session_scope() as session:
            service = CodelabGenerationService(session, settings)
            due = await service.due_difficulties()
            if not due:
                logger.info("Skipping CodeLab generation: every difficulty already has a recent problem")
                return
            await service.generate_set("scheduled", due)
    except Exception:
        # Last line of defense: a bug here must never kill the scheduler.
        logger.critical("Unexpected error in the CodeLab generation job", exc_info=True)


async def run_manual_generation(database: Database, settings: Settings, difficulties: list[str] | None = None) -> None:
    """Background task behind the admin "generate now" endpoint. Runs in its
    own session because it outlives the request that started it."""
    try:
        async with database.session_scope() as session:
            await CodelabGenerationService(session, settings).generate_set("manual-admin", difficulties or DIFFICULTIES)
    except Exception:
        logger.critical("Unexpected error in the manual CodeLab generation task", exc_info=True)
