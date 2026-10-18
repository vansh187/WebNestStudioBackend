"""The course exam view and the start gate (plan section 4.1)."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from exams.errors import CourseNotFound
from exams.protocols import AttemptStore, CertificateStore, Clock, CourseReader, ProgressReader
from exams.records import AttemptRecord, CertificateRecord, CourseRecord
from exams.services.attempt_lifecycle import AttemptLifecycle
from exams.services.common import guarded
from exams.services.pool_service import PoolService


@dataclass
class Eligibility:
    reasons: list[str] = field(default_factory=list)
    lessons_remaining: int = 0
    cooldown_until: datetime | None = None
    active: AttemptRecord | None = None
    certificate: CertificateRecord | None = None


@dataclass
class ExamView:
    enabled: bool
    eligible: bool
    reasons: list[str]
    lessons_remaining: int
    cooldown_until: datetime | None
    active_attempt_id: uuid.UUID | None
    passed: bool
    certificate_id: str | None
    question_count: int
    duration_minutes: int
    pass_percent: int


class EligibilityService:
    def __init__(
        self,
        courses: CourseReader,
        pool: PoolService,
        lifecycle: AttemptLifecycle,
        attempts: AttemptStore,
        certificates: CertificateStore,
        progress: ProgressReader,
        clock: Clock,
        settings,
    ) -> None:
        self._courses = courses
        self._pool = pool
        self._lifecycle = lifecycle
        self._attempts = attempts
        self._certificates = certificates
        self._progress = progress
        self._clock = clock
        self._settings = settings

    async def evaluate(self, course: CourseRecord, user_id: uuid.UUID, resolved_active: bool = False) -> Eligibility:
        """resolved_active=True: the caller just resolved the active attempt
        and found none, so skip resolving (and possibly grading) it again."""
        active = None if resolved_active else await self._lifecycle.resolve_active(user_id, course.id)
        certificate = await self._certificates.valid_for(user_id, course.id)
        remaining = await self._progress.remaining_lessons(user_id, course.id)
        cooldown_until = None
        latest = await self._attempts.latest_graded(user_id, course.id)
        if latest is not None and latest.passed is False and latest.submitted_at is not None:
            retake_at = latest.submitted_at + timedelta(hours=self._settings.exam_retake_hours)
            if retake_at > self._clock.now():
                cooldown_until = retake_at
        reasons = []
        if certificate is not None:
            reasons.append("already_passed")
        if active is not None:
            reasons.append("attempt_active")
        if remaining > 0:
            reasons.append("lessons_incomplete")
        if cooldown_until is not None:
            reasons.append("cooldown")
        return Eligibility(
            reasons=reasons,
            lessons_remaining=remaining,
            cooldown_until=cooldown_until,
            active=active,
            certificate=certificate,
        )

    @guarded("exam view")
    async def get_exam_view(self, slug: str, user_id: uuid.UUID | None) -> ExamView:
        course = await self._courses.get_published_course(slug)
        if course is None:
            raise CourseNotFound()
        view = ExamView(
            enabled=False,
            eligible=False,
            reasons=["exam_disabled"],
            lessons_remaining=0,
            cooldown_until=None,
            active_attempt_id=None,
            passed=False,
            certificate_id=None,
            question_count=self._settings.exam_question_count,
            duration_minutes=self._settings.exam_duration_minutes,
            pass_percent=self._settings.exam_pass_percent,
        )
        if not await self._pool.is_visible(course):
            return view
        view.enabled = True
        if user_id is None:
            view.reasons = ["login_required"]
            return view
        result = await self.evaluate(course, user_id)
        view.reasons = result.reasons
        view.eligible = not result.reasons
        view.lessons_remaining = result.lessons_remaining
        view.cooldown_until = result.cooldown_until
        view.active_attempt_id = result.active.id if result.active else None
        view.passed = result.certificate is not None
        view.certificate_id = result.certificate.certificate_id if result.certificate else None
        return view
