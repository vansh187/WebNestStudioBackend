"""Scheduled exam jobs (plan section 4.4 / 6.2 / 6.7). Every run opens its own
session and catches everything: a failing job is logged, never fatal."""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import Settings
from database.session import Database
from exams.dependencies import ExamServices
from services.blog_scheduler import IST

logger = logging.getLogger("webnest.exams.jobs")

FIRST_FILL_DELAY = timedelta(minutes=2)

# One refill per course per process at a time (the database also allows only
# one running job per course).
_running_fills: set[uuid.UUID] = set()


async def run_refill(database: Database, settings: Settings, course_id: uuid.UUID, trigger: str, job_id: uuid.UUID | None = None) -> None:
    """Runs (or creates and runs) the refill job for one course."""
    if course_id in _running_fills:
        return
    _running_fills.add(course_id)
    try:
        async with database.session_scope() as session:
            services = ExamServices(session, settings)
            generation = services.generation_service()
            job = await services.jobs.get(job_id) if job_id else None
            if job is None or job.status != "running":
                job, created = await generation.start_job(course_id, trigger)
                if job is None or not created:
                    return
            await generation.run_job(job)
    except Exception:
        logger.critical("Unexpected error in the exam refill for course %s", course_id, exc_info=True)
    finally:
        _running_fills.discard(course_id)


async def expire_overdue_attempts(database: Database, settings: Settings) -> None:
    try:
        async with database.session_scope() as session:
            summary = await ExamServices(session, settings).maintenance_service().expire_overdue()
            if summary["graded"] or summary["lost"] or summary["swept"]:
                logger.info("Exam expiry sweep: %s", summary)
    except Exception:
        logger.critical("Unexpected error in the exam expiry job", exc_info=True)


async def fill_pools(database: Database, settings: Settings) -> None:
    try:
        if settings.exam_enabled:
            # Automatic exams: switch on every published course, then pick up
            # lesson changes (re-grounding questions they no longer support),
            # even for courses whose pool is already full.
            async with database.session_scope() as session:
                services = ExamServices(session, settings)
                await services.auto_exam_service().enable_all()
                provider = services.context_provider()
                for course in await services.courses.list_enabled_courses():
                    try:
                        await provider.refresh(course)
                        await services.uow.commit()
                    except Exception:
                        await services.uow.rollback()
                        logger.error("Could not refresh the lesson material of %s", course.slug, exc_info=True)
        async with database.session_scope() as session:
            due = await ExamServices(session, settings).maintenance_service().courses_needing_fill()
        for course in due:
            await run_refill(database, settings, course.id, "replace")
    except Exception:
        logger.critical("Unexpected error in the exam pool fill job", exc_info=True)


async def question_stats(database: Database, settings: Settings) -> None:
    try:
        async with database.session_scope() as session:
            await ExamServices(session, settings).maintenance_service().question_stats()
    except Exception:
        logger.critical("Unexpected error in the exam statistics job", exc_info=True)


async def weekly_report(database: Database, settings: Settings) -> None:
    try:
        async with database.session_scope() as session:
            await ExamServices(session, settings).maintenance_service().weekly_report()
    except Exception:
        logger.critical("Unexpected error in the exam weekly report job", exc_info=True)


def register_exam_jobs(scheduler: AsyncIOScheduler, database: Database, settings: Settings) -> None:
    for name in ("exam_pool_target", "exam_ready_target"):
        value = getattr(settings, name)
        if value < settings.exam_question_count:
            logger.warning(
                "%s (%d) is below exam_question_count (%d); using %d so exams can still appear",
                name.upper(), value, settings.exam_question_count, settings.exam_question_count,
            )  # fmt: skip
    if settings.exam_pool_target < 2 * settings.exam_question_count:
        logger.warning(
            "EXAM_POOL_TARGET (%d) is under twice EXAM_QUESTION_COUNT (%d): a retake will repeat questions from the "
            "learner's previous attempt", settings.exam_pool_target, settings.exam_question_count,
        )  # fmt: skip
    common = {"args": [database, settings], "replace_existing": True, "max_instances": 1, "coalesce": True}
    scheduler.add_job(
        expire_overdue_attempts, trigger=IntervalTrigger(minutes=5), id="exam_expire_attempts",
        misfire_grace_time=300, **common,
    )  # fmt: skip
    scheduler.add_job(
        question_stats, trigger=CronTrigger(hour=2, minute=30, timezone=IST), id="exam_question_stats",
        misfire_grace_time=3600, **common,
    )  # fmt: skip
    scheduler.add_job(
        weekly_report, trigger=CronTrigger(day_of_week="mon", hour=8, minute=0, timezone=IST), id="exam_weekly_report",
        misfire_grace_time=6 * 3600, **common,
    )  # fmt: skip
    if settings.exam_generation_enabled:
        # First run shortly after startup, so a fresh deploy doesn't wait 30 minutes.
        scheduler.add_job(
            fill_pools, trigger=IntervalTrigger(minutes=30), id="exam_fill_pools", misfire_grace_time=900,
            next_run_time=datetime.now(timezone.utc) + FIRST_FILL_DELAY, **common,
        )  # fmt: skip
    elif settings.exam_enabled:
        logger.warning(
            "EXAM_ENABLED is true but EXAM_GENERATION_ENABLED is false: exams can't get questions, so none will appear"
        )
    else:
        logger.info("Exam pool refills are disabled (exam_generation_enabled=false)")


def start_refill_in_background(database: Database, settings: Settings, course_id: uuid.UUID, trigger: str, job_id: uuid.UUID | None) -> None:
    """Fire-and-forget from a request (the task outlives it, with its own session)."""
    task = asyncio.get_running_loop().create_task(run_refill(database, settings, course_id, trigger, job_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


_background_tasks: set[asyncio.Task] = set()
