"""exam_attempts: the permanent, compact record of every attempt."""

import uuid
from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from exams.errors import ActiveAttemptExists, ExamStoreUnavailable
from exams.infra.sql_base import SqlStore, constraint_name, logger
from exams.models import ExamAttempt
from exams.records import AttemptRecord

GRADED = ("submitted", "expired")


def _answers(raw) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, int) and not isinstance(value, bool):
            clean[str(key)] = value
    return clean


def _record(a: ExamAttempt) -> AttemptRecord:
    return AttemptRecord(
        id=a.id,
        user_id=a.user_id,
        course_id=a.course_id,
        recipient_name=a.recipient_name,
        question_ids=list(a.question_ids or []),
        answers=_answers(a.answers),
        tab_switches=a.tab_switches or 0,
        status=a.status,
        started_at=a.started_at,
        expires_at=a.expires_at,
        submitted_at=a.submitted_at,
        score=a.score,
        total=a.total,
        passed=a.passed,
        module_breakdown=list(a.module_breakdown) if a.module_breakdown is not None else None,
        results=list(a.results) if a.results is not None else None,
        lost_reason=a.lost_reason,
    )


class SqlAttemptStore(SqlStore):
    async def _first(self, statement, context: str) -> AttemptRecord | None:
        result = await self._execute(statement.execution_options(populate_existing=True), context)
        row = result.scalars().first()
        return _record(row) if row is not None else None

    async def get(self, attempt_id: uuid.UUID) -> AttemptRecord | None:
        return await self._first(select(ExamAttempt).where(ExamAttempt.id == attempt_id), "get attempt")

    async def get_for_update(self, attempt_id: uuid.UUID, skip_locked: bool = False) -> AttemptRecord | None:
        return await self._first(
            select(ExamAttempt).where(ExamAttempt.id == attempt_id).with_for_update(skip_locked=skip_locked),
            "lock attempt",
        )

    async def active_for(self, user_id: uuid.UUID, course_id: uuid.UUID) -> AttemptRecord | None:
        return await self._first(
            select(ExamAttempt).where(
                ExamAttempt.user_id == user_id, ExamAttempt.course_id == course_id, ExamAttempt.status == "active"
            ),
            "get active attempt",
        )

    async def latest_graded(self, user_id: uuid.UUID, course_id: uuid.UUID) -> AttemptRecord | None:
        return await self._first(
            select(ExamAttempt)
            .where(
                ExamAttempt.user_id == user_id,
                ExamAttempt.course_id == course_id,
                ExamAttempt.status.in_(GRADED),
            )
            .order_by(ExamAttempt.submitted_at.desc().nulls_last())
            .limit(1),
            "get latest graded attempt",
        )

    async def seen_questions(self, user_id: uuid.UUID, course_id: uuid.UUID) -> dict[uuid.UUID, datetime]:
        result = await self._execute(
            select(ExamAttempt.question_ids, ExamAttempt.started_at).where(
                ExamAttempt.user_id == user_id, ExamAttempt.course_id == course_id
            ),
            "load seen questions",
        )
        seen: dict[uuid.UUID, datetime] = {}
        for question_ids, started_at in result.all():
            for qid in question_ids or []:
                if qid not in seen or started_at > seen[qid]:
                    seen[qid] = started_at
        return seen

    async def insert(self, attempt: AttemptRecord) -> None:
        try:
            await self._session.execute(
                insert(ExamAttempt).values(
                    id=attempt.id,
                    user_id=attempt.user_id,
                    course_id=attempt.course_id,
                    recipient_name=attempt.recipient_name,
                    question_ids=attempt.question_ids,
                    answers={},
                    tab_switches=0,
                    status="active",
                    started_at=attempt.started_at,
                    expires_at=attempt.expires_at,
                    total=attempt.total,
                )
            )
        except IntegrityError as exc:
            if "uq_exam_attempts_user_course_active" in constraint_name(exc):
                raise ActiveAttemptExists() from exc
            logger.error("Attempt insert violated a constraint: %s", constraint_name(exc))
            raise ExamStoreUnavailable() from exc
        except SQLAlchemyError as exc:
            logger.error("Attempt insert failed", exc_info=True)
            raise ExamStoreUnavailable() from exc

    async def save_answers(
        self, attempt_id: uuid.UUID, user_id: uuid.UUID, answers: dict[str, int], tab_switches: int, now: datetime
    ) -> bool:
        """`now` is the cutoff: the attempt must expire after it (the service
        passes the clock minus the save grace)."""
        result = await self._execute(
            update(ExamAttempt)
            .where(
                ExamAttempt.id == attempt_id,
                ExamAttempt.user_id == user_id,
                ExamAttempt.status == "active",
                ExamAttempt.expires_at > now,
            )
            .values(answers=answers, tab_switches=func.greatest(ExamAttempt.tab_switches, tab_switches))
            .returning(ExamAttempt.id),
            "save answers",
        )
        return result.first() is not None

    async def mark_graded(self, attempt: AttemptRecord) -> None:
        await self._execute(
            update(ExamAttempt)
            .where(ExamAttempt.id == attempt.id, ExamAttempt.status == "active")
            .values(
                status=attempt.status,
                submitted_at=attempt.submitted_at,
                score=attempt.score,
                total=attempt.total,
                passed=attempt.passed,
                module_breakdown=attempt.module_breakdown,
                results=attempt.results,
                answers={},
            ),
            "mark attempt graded",
        )

    async def mark_lost(self, attempt_id: uuid.UUID, reason: str, now: datetime) -> bool:
        result = await self._execute(
            update(ExamAttempt)
            .where(ExamAttempt.id == attempt_id, ExamAttempt.status == "active")
            .values(status="lost", lost_reason=reason[:200], submitted_at=now)
            .returning(ExamAttempt.id),
            "mark attempt lost",
        )
        return result.first() is not None

    async def update_grade(
        self, attempt_id: uuid.UUID, score: int, total: int, passed: bool, module_breakdown: list[dict]
    ) -> None:
        await self._execute(
            update(ExamAttempt)
            .where(ExamAttempt.id == attempt_id)
            .values(score=score, total=total, passed=passed, module_breakdown=module_breakdown),
            "update attempt grade",
        )

    async def overdue_ids(self, cutoff: datetime, limit: int) -> list[uuid.UUID]:
        result = await self._execute(
            select(ExamAttempt.id)
            .where(ExamAttempt.status == "active", ExamAttempt.expires_at < cutoff)
            .order_by(ExamAttempt.expires_at)
            .limit(limit),
            "list overdue attempts",
        )
        return list(result.scalars().all())

    async def graded_containing(self, question_id: uuid.UUID) -> list[AttemptRecord]:
        result = await self._execute(
            select(ExamAttempt)
            .where(ExamAttempt.question_ids.contains([question_id]), ExamAttempt.status.in_(GRADED))
            .execution_options(populate_existing=True),
            "list attempts containing question",
        )
        return [_record(a) for a in result.scalars().all()]

    async def graded_results(self, course_id: uuid.UUID) -> list[tuple[int, int, list[dict]]]:
        result = await self._execute(
            select(ExamAttempt.score, ExamAttempt.total, ExamAttempt.results).where(
                ExamAttempt.course_id == course_id,
                ExamAttempt.status.in_(GRADED),
                ExamAttempt.results.is_not(None),
            ),
            "load graded results",
        )
        return [(int(s or 0), int(t or 0), list(r or [])) for s, t, r in result.all()]

    async def count_lost_since(self, since: datetime) -> int:
        result = await self._execute(
            select(func.count()).where(ExamAttempt.status == "lost", ExamAttempt.submitted_at >= since),
            "count lost attempts",
        )
        return int(result.scalar_one() or 0)
