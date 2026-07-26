import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from core.config import Settings
from database.session import Database
from services.blog_generation_service import BlogGenerationError, BlogGenerationService
from services.blog_service import BlogService
from services.llm_provider import GenerationFailedError

logger = logging.getLogger("webnest.blog_scheduler")

IST = ZoneInfo("Asia/Kolkata")


def create_scheduler() -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone=IST)


def register_blog_jobs(scheduler: AsyncIOScheduler, database: Database, settings: Settings) -> None:
    """Registers the recurring generation, one-off launch, and daily archive
    jobs. Called fresh on every app startup - APScheduler's default job store
    is in-memory, so nothing needs to survive a restart except the DB state
    the jobs themselves read (which is what makes them idempotent)."""
    if not settings.blog_generation_enabled:
        logger.info("Blog generation is disabled (blog_generation_enabled=false) - no scheduler jobs registered")
        return

    scheduler.add_job(
        _recurring_generation_job,
        trigger=CronTrigger(hour=settings.blog_generation_hour_ist, minute=0, timezone=IST),
        args=[database, settings],
        id="blog_recurring_generation",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _archive_expired_job,
        trigger=CronTrigger(hour=0, minute=10, timezone=IST),
        args=[database],
        id="blog_archive_expired",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _schedule_launch_post(scheduler, database, settings)


def _schedule_launch_post(scheduler: AsyncIOScheduler, database: Database, settings: Settings) -> None:
    raw = settings.blog_launch_special_post_at.strip()
    if not raw:
        return
    try:
        target = datetime.fromisoformat(raw)
    except ValueError:
        logger.error(
            "blog_launch_special_post_at=%r is not a valid ISO datetime - skipping the one-off launch post", raw
        )
        return
    if target.tzinfo is None:
        target = target.replace(tzinfo=IST)

    now = datetime.now(IST)
    # If the target time has already passed (e.g. this deploy happened after
    # 6 PM IST today), fire almost immediately as a catch-up rather than
    # silently missing it - the job itself still checks "already published
    # today" so a redeploy a few minutes later can't double-publish.
    run_date = target if target > now else now + timedelta(seconds=15)
    scheduler.add_job(
        _launch_special_post_job,
        trigger=DateTrigger(run_date=run_date, timezone=IST),
        args=[database, settings],
        id="blog_launch_special_post",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled the one-off launch blog post for %s", run_date.isoformat())


async def _recurring_generation_job(database: Database, settings: Settings) -> None:
    try:
        async with database.session_scope() as session:
            service = BlogGenerationService(session, settings)
            if not await service.should_run_recurring():
                logger.info(
                    "Skipping recurring blog generation: last post published less than %s day(s) ago",
                    settings.blog_generation_interval_days,
                )
                return
            await service.generate_and_publish(trigger_source="scheduled-recurring")
    except (BlogGenerationError, GenerationFailedError):
        pass  # already logged/alerted inside the service
    except Exception:
        # Last line of defense: a bug here must never kill the scheduler
        # thread or the app process - just log and let the next cron fire.
        logger.critical("Unexpected error in the recurring blog generation job", exc_info=True)


async def _launch_special_post_job(database: Database, settings: Settings) -> None:
    try:
        async with database.session_scope() as session:
            service = BlogGenerationService(session, settings)
            now_ist = datetime.now(IST)
            if await service.already_published_on_ist_date(now_ist):
                logger.info("Skipping one-off launch post: a post was already published today")
                return
            await service.generate_and_publish(trigger_source="scheduled-launch")
    except (BlogGenerationError, GenerationFailedError):
        pass
    except Exception:
        logger.critical("Unexpected error in the one-off launch blog post job", exc_info=True)


async def _archive_expired_job(database: Database) -> None:
    try:
        async with database.session_scope() as session:
            archived = await BlogService(session).archive_expired()
            if archived:
                logger.info("Archived %s expired blog post(s)", archived)
    except Exception:
        logger.critical("Unexpected error in the blog archive-expired job", exc_info=True)
