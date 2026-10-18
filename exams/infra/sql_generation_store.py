"""exam_module_context, exam_generation_jobs and exam_generation_logs."""

import uuid
from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from exams.errors import ExamStoreUnavailable, JobAlreadyRunning
from exams.infra.sql_base import SqlStore, constraint_name, logger
from exams.models import ExamGenerationJob, ExamGenerationLog, ExamModuleContext
from exams.records import GenerationLogEntry, JobRecord, ModuleContextRecord, ProviderUsage


def _job(j: ExamGenerationJob) -> JobRecord:
    return JobRecord(
        id=j.id,
        course_id=j.course_id,
        trigger=j.trigger,
        status=j.status,
        progress=dict(j.progress or {}),
        calls=j.calls or 0,
        created=j.created or 0,
        rejected=dict(j.rejected or {}),
        errors=list(j.errors or []),
        paused_until=j.paused_until,
        started_at=j.started_at,
        heartbeat_at=j.heartbeat_at,
        finished_at=j.finished_at,
        created_at=j.created_at,
    )


class SqlContextStore(SqlStore):
    async def get_many(self, course_id: uuid.UUID) -> dict[uuid.UUID, ModuleContextRecord]:
        result = await self._execute(
            select(ExamModuleContext).where(ExamModuleContext.course_id == course_id), "load module contexts"
        )
        return {
            c.module_id: ModuleContextRecord(
                module_id=c.module_id, course_id=c.course_id, context=dict(c.context or {}), context_hash=c.context_hash
            )
            for c in result.scalars().all()
        }

    async def upsert(self, module_id: uuid.UUID, course_id: uuid.UUID, context: dict, context_hash: str, now: datetime) -> None:
        statement = pg_insert(ExamModuleContext).values(
            module_id=module_id, course_id=course_id, context=context, context_hash=context_hash, updated_at=now
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ExamModuleContext.module_id],
            set_={
                "course_id": statement.excluded.course_id,
                "context": statement.excluded.context,
                "context_hash": statement.excluded.context_hash,
                "updated_at": statement.excluded.updated_at,
            },
        )
        await self._execute(statement, "save module context")


class SqlJobStore(SqlStore):
    async def _first(self, statement, context: str) -> JobRecord | None:
        result = await self._execute(statement.execution_options(populate_existing=True), context)
        row = result.scalars().first()
        return _job(row) if row is not None else None

    async def create_running(self, course_id: uuid.UUID, trigger: str, now: datetime) -> JobRecord:
        """Raises JobAlreadyRunning (call inside a savepoint: the failed insert
        must roll the savepoint back, so it is raised, never swallowed)."""
        try:
            result = await self._session.execute(
                insert(ExamGenerationJob)
                .values(course_id=course_id, trigger=trigger, status="running", started_at=now, heartbeat_at=now)
                .returning(ExamGenerationJob.id)
            )
        except IntegrityError as exc:
            if "uq_exam_generation_jobs_course_running" in constraint_name(exc):
                raise JobAlreadyRunning() from exc
            logger.error("Job insert violated a constraint: %s", constraint_name(exc))
            raise ExamStoreUnavailable() from exc
        except SQLAlchemyError as exc:
            logger.error("Job insert failed", exc_info=True)
            raise ExamStoreUnavailable() from exc
        return await self.get(result.scalar_one())

    async def get(self, job_id: uuid.UUID) -> JobRecord | None:
        return await self._first(select(ExamGenerationJob).where(ExamGenerationJob.id == job_id), "get job")

    async def running_for(self, course_id: uuid.UUID) -> JobRecord | None:
        return await self._first(
            select(ExamGenerationJob).where(
                ExamGenerationJob.course_id == course_id, ExamGenerationJob.status == "running"
            ),
            "get running job",
        )

    async def latest_for(self, course_id: uuid.UUID) -> JobRecord | None:
        return await self._first(
            select(ExamGenerationJob)
            .where(ExamGenerationJob.course_id == course_id)
            .order_by(ExamGenerationJob.created_at.desc())
            .limit(1),
            "get latest job",
        )

    async def save(self, job: JobRecord) -> None:
        await self._execute(
            update(ExamGenerationJob)
            .where(ExamGenerationJob.id == job.id)
            .values(
                status=job.status,
                progress=job.progress,
                calls=job.calls,
                created=job.created,
                rejected=job.rejected,
                errors=job.errors[-50:],
                paused_until=job.paused_until,
                heartbeat_at=job.heartbeat_at,
                finished_at=job.finished_at,
            ),
            "save job",
        )

    async def fail_stale(self, heartbeat_before: datetime, now: datetime) -> int:
        result = await self._execute(
            update(ExamGenerationJob)
            .where(ExamGenerationJob.status == "running", ExamGenerationJob.heartbeat_at < heartbeat_before)
            .values(status="failed", finished_at=now)
            .returning(ExamGenerationJob.id),
            "fail stale jobs",
        )
        return len(result.all())


class SqlGenerationLogStore(SqlStore):
    async def add(self, entry: GenerationLogEntry) -> None:
        await self._execute(
            insert(ExamGenerationLog).values(
                job_id=entry.job_id,
                course_id=entry.course_id,
                module_id=entry.module_id,
                purpose=entry.purpose,
                provider=entry.provider,
                model=entry.model,
                requested_count=entry.requested_count,
                accepted=entry.accepted,
                rejected=entry.rejected,
                prompt_tokens=entry.prompt_tokens,
                completion_tokens=entry.completion_tokens,
                rate_limits=entry.rate_limits,
                error=(entry.error or None) and entry.error[:500],
            ),
            "add generation log",
        )

    async def calls_since(self, since: datetime, purposes: tuple[str, ...]) -> int:
        result = await self._execute(
            select(func.count()).where(ExamGenerationLog.created_at >= since, ExamGenerationLog.purpose.in_(purposes)),
            "count generation calls",
        )
        return int(result.scalar_one() or 0)

    async def usage_since(self, since: datetime, hour_ago: datetime) -> list[ProviderUsage]:
        tokens = ExamGenerationLog.prompt_tokens + ExamGenerationLog.completion_tokens
        recent = ExamGenerationLog.created_at >= hour_ago
        result = await self._execute(
            select(
                ExamGenerationLog.provider,
                func.count(),
                func.coalesce(func.sum(tokens), 0),
                func.count().filter(recent),
                func.coalesce(func.sum(tokens).filter(recent), 0),
            )
            .where(ExamGenerationLog.created_at >= since, ExamGenerationLog.provider.is_not(None))
            .group_by(ExamGenerationLog.provider),
            "aggregate provider usage",
        )
        rows = result.all()
        usage: list[ProviderUsage] = []
        for provider, requests, used_tokens, hour_requests, hour_tokens in rows:
            latest = await self._execute(
                select(ExamGenerationLog.rate_limits)
                .where(ExamGenerationLog.provider == provider, ExamGenerationLog.rate_limits.is_not(None))
                .order_by(ExamGenerationLog.created_at.desc())
                .limit(1),
                "latest rate limits",
            )
            usage.append(
                ProviderUsage(
                    provider=provider,
                    requests=int(requests),
                    tokens=int(used_tokens),
                    last_hour_requests=int(hour_requests),
                    last_hour_tokens=int(hour_tokens),
                    latest_rate_limits=latest.scalar(),
                )
            )
        return usage
