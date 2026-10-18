"""The exam question pool. Every pool query is filtered by course_id - no
cross-course pool query exists (plan section 4.2, enforcement layer 1)."""

import uuid
from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from exams.errors import DuplicateQuestion, ExamStoreUnavailable
from exams.infra.sql_base import SqlStore, constraint_name, logger
from exams.models import ExamQuestion, ExamQuestionReport, ExamQuestionStats
from exams.records import CandidateRow, DedupeRow, NewQuestion, QuestionRecord, ServeRow

_UPDATABLE = frozenset(
    {
        "concept", "kind", "difficulty", "prompt", "options", "correct_index", "explanation",
        "why_others_wrong", "source_fact", "code", "code_language", "expected_output", "status",
        "reject_reason", "prompt_norm", "prompt_hash", "embedding", "checks", "lesson_id", "module_id",
    }
)  # fmt: skip


def _record(q: ExamQuestion) -> QuestionRecord:
    return QuestionRecord(
        id=q.id,
        course_id=q.course_id,
        module_id=q.module_id,
        lesson_id=q.lesson_id,
        concept=q.concept,
        kind=q.kind,
        difficulty=q.difficulty,
        prompt=q.prompt,
        options=list(q.options or []),
        correct_index=q.correct_index,
        explanation=q.explanation or "",
        why_others_wrong=list(q.why_others_wrong) if q.why_others_wrong is not None else None,
        source_fact=q.source_fact,
        code=q.code,
        code_language=q.code_language,
        expected_output=q.expected_output,
        status=q.status,
        source=q.source,
        reject_reason=q.reject_reason,
        prompt_norm=q.prompt_norm,
        prompt_hash=q.prompt_hash,
        embedding=list(q.embedding) if q.embedding is not None else None,
        checks=dict(q.checks) if q.checks is not None else None,
        generator_model=q.generator_model,
        generation_job_id=q.generation_job_id,
        times_served=q.times_served or 0,
        created_at=q.created_at,
        updated_at=q.updated_at,
    )


class SqlQuestionStore(SqlStore):
    async def _records(self, statement, context: str) -> list[QuestionRecord]:
        result = await self._execute(statement.execution_options(populate_existing=True), context)
        return [_record(q) for q in result.scalars().all()]

    async def live_candidates(self, course_id: uuid.UUID) -> list[CandidateRow]:
        result = await self._execute(
            select(
                ExamQuestion.id, ExamQuestion.course_id, ExamQuestion.module_id,
                ExamQuestion.lesson_id, ExamQuestion.difficulty, ExamQuestion.concept,
            ).where(ExamQuestion.course_id == course_id, ExamQuestion.status == "live"),
            "load live candidates",
        )  # fmt: skip
        return [CandidateRow(*row) for row in result.all()]

    async def load_for_serving(self, course_id: uuid.UUID, question_ids: list[uuid.UUID]) -> list[ServeRow]:
        if not question_ids:
            return []
        result = await self._execute(
            select(
                ExamQuestion.id, ExamQuestion.course_id, ExamQuestion.module_id,
                ExamQuestion.prompt, ExamQuestion.options, ExamQuestion.correct_index,
            ).where(
                ExamQuestion.course_id == course_id,
                ExamQuestion.status == "live",
                ExamQuestion.id.in_(question_ids),
            ),
            "load questions for serving",
        )  # fmt: skip
        return [
            ServeRow(id=r[0], course_id=r[1], module_id=r[2], prompt=r[3], options=list(r[4]), correct_index=r[5])
            for r in result.all()
        ]

    async def increment_served(self, question_ids: list[uuid.UUID]) -> None:
        if question_ids:
            await self._execute(
                update(ExamQuestion)
                .where(ExamQuestion.id.in_(question_ids))
                .values(times_served=ExamQuestion.times_served + 1),
                "increment times served",
            )

    async def status_counts(self, course_id: uuid.UUID) -> dict[uuid.UUID, dict[str, int]]:
        result = await self._execute(
            select(ExamQuestion.module_id, ExamQuestion.status, func.count())
            .where(ExamQuestion.course_id == course_id)
            .group_by(ExamQuestion.module_id, ExamQuestion.status),
            "count questions by status",
        )
        counts: dict[uuid.UUID, dict[str, int]] = {}
        for module_id, status, count in result.all():
            counts.setdefault(module_id, {})[status] = int(count)
        return counts

    async def live_counts(self, course_id: uuid.UUID) -> dict[uuid.UUID, int]:
        result = await self._execute(
            select(ExamQuestion.module_id, func.count())
            .where(ExamQuestion.course_id == course_id, ExamQuestion.status == "live")
            .group_by(ExamQuestion.module_id),
            "count live questions",
        )
        return {module_id: int(count) for module_id, count in result.all()}

    async def list_questions(
        self, course_id: uuid.UUID, status: str | None, module_id: uuid.UUID | None, limit: int
    ) -> list[QuestionRecord]:
        statement = select(ExamQuestion).where(ExamQuestion.course_id == course_id)
        if status:
            statement = statement.where(ExamQuestion.status == status)
        if module_id:
            statement = statement.where(ExamQuestion.module_id == module_id)
        statement = statement.order_by(ExamQuestion.updated_at.desc()).limit(max(1, min(limit, 1000)))
        return await self._records(statement, "list questions")

    async def get(self, question_id: uuid.UUID) -> QuestionRecord | None:
        rows = await self._records(select(ExamQuestion).where(ExamQuestion.id == question_id), "get question")
        return rows[0] if rows else None

    async def get_for_update(self, question_id: uuid.UUID) -> QuestionRecord | None:
        rows = await self._records(
            select(ExamQuestion).where(ExamQuestion.id == question_id).with_for_update(), "lock question"
        )
        return rows[0] if rows else None

    async def insert(self, question: NewQuestion) -> uuid.UUID:
        values = {key: getattr(question, key) for key in NewQuestion.__dataclass_fields__}
        try:
            result = await self._session.execute(insert(ExamQuestion).values(**values).returning(ExamQuestion.id))
        except IntegrityError as exc:
            if "uq_exam_questions_course_hash" in constraint_name(exc):
                raise DuplicateQuestion() from exc
            logger.error("Exam question insert violated a constraint: %s", constraint_name(exc))
            raise ExamStoreUnavailable() from exc
        except SQLAlchemyError as exc:
            logger.error("Exam question insert failed", exc_info=True)
            raise ExamStoreUnavailable() from exc
        return result.scalar_one()

    async def update(self, question_id: uuid.UUID, fields: dict) -> None:
        values = {k: v for k, v in fields.items() if k in _UPDATABLE}
        if not values:
            return
        values["updated_at"] = func.now()
        try:
            await self._session.execute(update(ExamQuestion).where(ExamQuestion.id == question_id).values(**values))
        except IntegrityError as exc:
            if "uq_exam_questions_course_hash" in constraint_name(exc):
                raise DuplicateQuestion() from exc
            logger.error("Exam question update violated a constraint: %s", constraint_name(exc))
            raise ExamStoreUnavailable() from exc
        except SQLAlchemyError as exc:
            logger.error("Exam question update failed", exc_info=True)
            raise ExamStoreUnavailable() from exc

    async def set_status(self, question_ids: list[uuid.UUID], status: str, from_statuses: tuple[str, ...]) -> int:
        if not question_ids:
            return 0
        result = await self._execute(
            update(ExamQuestion)
            .where(ExamQuestion.id.in_(question_ids), ExamQuestion.status.in_(from_statuses))
            .values(status=status, updated_at=func.now())
            .returning(ExamQuestion.id),
            "set question status",
        )
        return len(result.all())

    async def dedupe_rows(self, course_id: uuid.UUID, exclude_id: uuid.UUID | None = None) -> list[DedupeRow]:
        statement = select(
            ExamQuestion.id, ExamQuestion.lesson_id, ExamQuestion.concept,
            ExamQuestion.prompt_norm, ExamQuestion.prompt_hash, ExamQuestion.embedding,
        ).where(ExamQuestion.course_id == course_id, ExamQuestion.status != "rejected")  # fmt: skip
        if exclude_id is not None:
            statement = statement.where(ExamQuestion.id != exclude_id)
        result = await self._execute(statement, "load dedupe rows")
        return [
            DedupeRow(id=r[0], lesson_id=r[1], concept=r[2], prompt_norm=r[3], prompt_hash=r[4],
                      embedding=list(r[5]) if r[5] is not None else None)
            for r in result.all()
        ]  # fmt: skip

    async def ids_with_status(self, course_id: uuid.UUID, status: str) -> set[uuid.UUID]:
        result = await self._execute(
            select(ExamQuestion.id).where(ExamQuestion.course_id == course_id, ExamQuestion.status == status),
            "load question ids by status",
        )
        return set(result.scalars().all())

    async def report_counts(self, question_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not question_ids:
            return {}
        result = await self._execute(
            select(ExamQuestionReport.question_id, func.count())
            .where(ExamQuestionReport.question_id.in_(question_ids))
            .group_by(ExamQuestionReport.question_id),
            "count reports",
        )
        return {qid: int(count) for qid, count in result.all()}

    async def stats_for(self, question_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
        if not question_ids:
            return {}
        result = await self._execute(
            select(ExamQuestionStats).where(ExamQuestionStats.question_id.in_(question_ids)),
            "load question stats",
        )
        return {
            s.question_id: {"answered": s.answered, "correct": s.correct, "discrimination": s.discrimination}
            for s in result.scalars().all()
        }

    async def live_by_module(self, module_id: uuid.UUID) -> list[QuestionRecord]:
        return await self._records(
            select(ExamQuestion).where(ExamQuestion.module_id == module_id, ExamQuestion.status == "live"),
            "load live questions of module",
        )

    async def unchecked_seed_questions(self, course_id: uuid.UUID, limit: int) -> list[QuestionRecord]:
        return await self._records(
            select(ExamQuestion)
            .where(
                ExamQuestion.course_id == course_id,
                ExamQuestion.status == "live",
                ExamQuestion.checks["checker"].astext.in_(("skipped_no_quota", "deferred")),
            )
            .limit(max(1, limit)),
            "load unchecked seed questions",
        )

    async def over_served(self, course_id: uuid.UUID, max_serves: int) -> list[uuid.UUID]:
        result = await self._execute(
            select(ExamQuestion.id).where(
                ExamQuestion.course_id == course_id,
                ExamQuestion.status == "live",
                ExamQuestion.times_served >= max_serves,
            ),
            "load over-served questions",
        )
        return list(result.scalars().all())

    async def upsert_stats(self, rows: list[dict], now: datetime) -> None:
        if not rows:
            return
        statement = pg_insert(ExamQuestionStats).values(
            [{**row, "updated_at": now} for row in rows]
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ExamQuestionStats.question_id],
            set_={
                "answered": statement.excluded.answered,
                "correct": statement.excluded.correct,
                "discrimination": statement.excluded.discrimination,
                "updated_at": statement.excluded.updated_at,
            },
        )
        await self._execute(statement, "upsert question stats")
