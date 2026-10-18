"""Grading, expiry and lost-attempt handling (plan 4.3-4.5, 6.6). The only
code that moves an attempt out of `active`, always under a row lock:

    SELECT ... FOR UPDATE -> grade from the cache key -> store compact results
    -> issue the certificate if passed -> delete the cache -> COMMIT

all in one transaction, so a crash can never leave a graded attempt with its
key still cached, or a deleted cache with no grade."""

import logging
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

from exams.domain.grading import GradeOutcome, KeyEntry, grade
from exams.protocols import AttemptCache, AttemptStore, Clock, CourseReader, UnitOfWork
from exams.records import AttemptRecord
from exams.services.certificate_service import CertificateService

logger = logging.getLogger("webnest.exams.attempts")


def parse_key(payload: list) -> list[KeyEntry] | None:
    """The grading key from a cache payload, or None if it is unusable."""
    if not isinstance(payload, list) or not payload:
        return None
    key: list[KeyEntry] = []
    try:
        for entry in payload:
            permutation = tuple(int(i) for i in entry["permutation"])
            correct = int(entry["correct_index"])
            if sorted(permutation) != [0, 1, 2, 3] or not 0 <= correct <= 3:
                return None
            key.append(
                KeyEntry(
                    question_id=str(entry["question_id"]),
                    module_title=str(entry.get("module_title") or "General"),
                    permutation=permutation,
                    correct_index=correct,
                )
            )
    except (KeyError, TypeError, ValueError):
        return None
    return key


class AttemptLifecycle:
    def __init__(
        self,
        attempts: AttemptStore,
        cache: AttemptCache,
        courses: CourseReader,
        certificates: CertificateService,
        uow: UnitOfWork,
        clock: Clock,
        settings,
    ) -> None:
        self._attempts = attempts
        self._cache = cache
        self._courses = courses
        self._certificates = certificates
        self._uow = uow
        self._clock = clock
        self._settings = settings

    @property
    def grace(self) -> timedelta:
        return timedelta(seconds=self._settings.exam_submit_grace_seconds)

    def is_overdue(self, attempt: AttemptRecord, now: datetime) -> bool:
        return now > attempt.expires_at + self.grace

    async def grade(self, attempt_id: uuid.UUID, skip_locked: bool = False) -> AttemptRecord | None:
        """Grades (or marks lost) an active attempt and commits. Returns the
        attempt's final state; a graded/lost attempt is returned unchanged.
        None when missing, or locked elsewhere with skip_locked."""
        try:
            attempt = await self._attempts.get_for_update(attempt_id, skip_locked=skip_locked)
            if attempt is None or attempt.status != "active":
                await self._uow.rollback()
                return attempt
            now = self._clock.now()
            payload = await self._cache.get(attempt.id, attempt.user_id)
            key = parse_key(payload) if payload is not None else None
            if key is None:
                reason = "cache_missing" if payload is None else "cache_corrupt"
                await self._attempts.mark_lost(attempt.id, reason, now)
                await self._cache.delete(attempt.id, attempt.user_id)
                await self._uow.commit()
                logger.warning("Attempt %s (user %s) lost at grading: %s", attempt.id, attempt.user_id, reason)
                return replace(attempt, status="lost", lost_reason=reason)

            outcome: GradeOutcome = grade(key, attempt.answers, self._settings.exam_pass_percent)
            graded = replace(
                attempt,
                status="expired" if self.is_overdue(attempt, now) else "submitted",
                submitted_at=now,
                score=outcome.score,
                total=outcome.total,
                passed=outcome.passed,
                module_breakdown=outcome.module_breakdown,
                results=outcome.results,
                answers={},
            )
            await self._attempts.mark_graded(graded)
            if outcome.passed:
                course = await self._courses.get_course_by_id(attempt.course_id)
                if course is None:
                    raise RuntimeError("course of a graded attempt disappeared")
                await self._certificates.issue_in_transaction(graded, course, outcome.score, outcome.total, now)
            await self._cache.delete(attempt.id, attempt.user_id)
            await self._uow.commit()
            logger.info(
                "Graded attempt %s (user %s, course %s): %s %d/%d",
                attempt.id, attempt.user_id, attempt.course_id, graded.status, outcome.score, outcome.total,
            )  # fmt: skip
            return graded
        except Exception:
            await self._uow.rollback()
            raise

    async def mark_lost(self, attempt: AttemptRecord, reason: str) -> None:
        try:
            changed = await self._attempts.mark_lost(attempt.id, reason, self._clock.now())
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise
        if changed:
            logger.warning("Attempt %s (user %s) marked lost: %s", attempt.id, attempt.user_id, reason)

    async def resolve_active(self, user_id: uuid.UUID, course_id: uuid.UUID) -> AttemptRecord | None:
        """The user's truly running attempt, after lazily grading an overdue
        one and marking one whose questions are gone as lost."""
        attempt = await self._attempts.active_for(user_id, course_id)
        if attempt is None:
            return None
        if self.is_overdue(attempt, self._clock.now()):
            final = await self.grade(attempt.id)
            return final if final is not None and final.status == "active" else None
        if not await self._cache.exists(attempt.id, attempt.user_id):
            await self.mark_lost(attempt, "cache_missing")
            return None
        return attempt
