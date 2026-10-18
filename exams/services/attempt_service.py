"""Learner attempt endpoints (plan section 4.3). Every endpoint reads the
exam_attempts row first and acts on its status; the cache is consulted only
for an active attempt."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from exams.domain.draw import DrawCandidate, DrawModule, draw
from exams.domain.grading import percent_of
from exams.domain.validation import clean_recipient_name
from exams.errors import (
    ActiveAttemptExists,
    AttemptClosed,
    AttemptLost,
    AttemptNotFound,
    CourseNotFound,
    CrossCourseQuestion,
    ExamConflict,
    ExamDisabled,
    ExamValidationError,
    NotEligible,
    PoolNotReady,
    RateLimited,
)
from exams.protocols import (
    AttemptCache,
    AttemptStore,
    CertificateStore,
    Clock,
    CourseReader,
    IdGenerator,
    QuestionStore,
    RandomSource,
    RateLimiter,
    ReportStore,
    UnitOfWork,
)
from exams.records import AttemptRecord
from exams.services.attempt_lifecycle import AttemptLifecycle
from exams.services.common import guarded
from exams.services.eligibility_service import EligibilityService
from exams.services.pool_service import PoolService

logger = logging.getLogger("webnest.exams.attempts")

START_LIMIT = (5, 3600)  # new attempts
RESUME_LIMIT = (60, 60)  # re-opening a running attempt through the start endpoint
START_CALLS_LIMIT = (60, 60)  # every call, so refused starts can't be spammed either
SAVE_LIMIT = (60, 60)
SUBMIT_LIMIT = (10, 60)
REPORT_LIMIT = (30, 3600)
CACHE_MARGIN = timedelta(minutes=10)
REPORT_REASONS = ("wrong_answer", "unclear", "typo", "other")


@dataclass
class ActiveAttemptView:
    attempt_id: uuid.UUID
    status: str
    started_at: datetime
    expires_at: datetime
    server_now: datetime
    questions: list[dict]
    answers: list[dict]


@dataclass
class ResultView:
    score: int
    total: int
    percent: int
    passed: bool
    module_breakdown: list[dict]
    certificate_id: str | None
    retake_after: datetime | None


@dataclass
class GradedAttemptView:
    attempt_id: uuid.UUID
    status: str
    result: ResultView


@dataclass
class PingView:
    status: str
    expires_at: datetime
    server_now: datetime


class AttemptService:
    def __init__(
        self,
        courses: CourseReader,
        questions: QuestionStore,
        attempts: AttemptStore,
        cache: AttemptCache,
        certificates: CertificateStore,
        reports: ReportStore,
        pool: PoolService,
        eligibility: EligibilityService,
        lifecycle: AttemptLifecycle,
        uow: UnitOfWork,
        clock: Clock,
        ids: IdGenerator,
        random_source: RandomSource,
        limiter: RateLimiter,
        settings,
    ) -> None:
        self._courses = courses
        self._questions = questions
        self._attempts = attempts
        self._cache = cache
        self._certificates = certificates
        self._reports = reports
        self._pool = pool
        self._eligibility = eligibility
        self._lifecycle = lifecycle
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._random = random_source
        self._limiter = limiter
        self._settings = settings

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _limit(self, name: str, user_id: uuid.UUID, rule: tuple[int, int]) -> None:
        retry_after = self._limiter.hit(f"{name}:{user_id}", rule[0], rule[1])
        if retry_after:
            raise RateLimited(retry_after)

    def _active_view(self, attempt: AttemptRecord, payload: list[dict]) -> ActiveAttemptView:
        questions = [
            {
                "id": entry["question_id"],
                "prompt": entry["prompt"],
                "options": list(entry["options"]),
                "module_title": entry.get("module_title") or "General",
            }
            for entry in payload
        ]
        order = {str(qid): i for i, qid in enumerate(attempt.question_ids)}
        answers = [
            {"question_id": qid, "option_index": index}
            for qid, index in sorted(attempt.answers.items(), key=lambda item: order.get(item[0], len(order)))
            if qid in order
        ]
        return ActiveAttemptView(
            attempt_id=attempt.id,
            status="active",
            started_at=attempt.started_at,
            expires_at=attempt.expires_at,
            server_now=self._clock.now(),
            questions=questions,
            answers=answers,
        )

    async def _result(self, attempt: AttemptRecord) -> ResultView:
        certificate = await self._certificates.valid_for_attempt(attempt.id) if attempt.passed else None
        retake_after = None
        if not attempt.passed and attempt.submitted_at is not None:
            retake_after = attempt.submitted_at + timedelta(hours=self._settings.exam_retake_hours)
        score, total = attempt.score or 0, attempt.total or 0
        return ResultView(
            score=score,
            total=total,
            percent=percent_of(score, total),
            passed=bool(attempt.passed),
            module_breakdown=list(attempt.module_breakdown or []),
            certificate_id=certificate.certificate_id if certificate else None,
            retake_after=retake_after,
        )

    async def _owned(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> AttemptRecord:
        attempt = await self._attempts.get(attempt_id)
        if attempt is None or attempt.user_id != user_id:
            raise AttemptNotFound()
        return attempt

    async def _open_active(self, attempt: AttemptRecord) -> tuple[AttemptRecord, list[dict] | None]:
        """For an active attempt: grades it if overdue, else loads its cache.
        Raises AttemptLost when its questions are gone."""
        if self._lifecycle.is_overdue(attempt, self._clock.now()):
            final = await self._lifecycle.grade(attempt.id)
            if final is None:
                raise AttemptNotFound()
            if final.status == "lost":
                raise AttemptLost()
            return final, None
        payload = await self._cache.get(attempt.id, attempt.user_id)
        if payload is None:
            await self._lifecycle.mark_lost(attempt, "cache_missing")
            raise AttemptLost()
        return attempt, payload

    # ------------------------------------------------------------------ #
    # Start
    # ------------------------------------------------------------------ #
    @guarded("start attempt")
    async def start(self, slug: str, user_id: uuid.UUID, recipient_name: str) -> tuple[ActiveAttemptView, bool]:
        """(view, created): created is False when an active attempt was resumed."""
        try:
            name = clean_recipient_name(recipient_name)
        except ValueError as exc:
            raise ExamValidationError(f"recipient_name: {exc}") from exc
        self._limit("start_calls", user_id, START_CALLS_LIMIT)

        course = await self._courses.get_published_course(slug)
        if course is None:
            raise CourseNotFound()
        if not course.exam_enabled:
            raise ExamDisabled()

        visible = False
        for _ in range(2):
            # A running attempt always resumes: its questions are frozen in its
            # cache, whatever happened to the pool since.
            active = await self._lifecycle.resolve_active(user_id, course.id)
            if active is not None:
                payload = await self._cache.get(active.id, user_id)
                if payload is not None:
                    # Resuming never uses up the new-attempt budget.
                    self._limit("resume", user_id, RESUME_LIMIT)
                    return self._active_view(active, payload), False
                await self._lifecycle.mark_lost(active, "cache_missing")

            if not visible:
                if not await self._pool.is_visible(course):
                    raise PoolNotReady()
                visible = True

            # The active attempt was just resolved (None here): don't resolve it again.
            eligibility = await self._eligibility.evaluate(course, user_id, resolved_active=True)
            reasons = [r for r in eligibility.reasons if r != "attempt_active"]
            if reasons:
                extra = {"reason": reasons[0]}
                if eligibility.cooldown_until is not None:
                    extra["cooldown_until"] = eligibility.cooldown_until
                if eligibility.lessons_remaining:
                    extra["lessons_remaining"] = eligibility.lessons_remaining
                raise NotEligible(**extra)

            self._limit("start", user_id, START_LIMIT)
            try:
                return await self._create_attempt(course, user_id, name), True
            except ActiveAttemptExists:
                # A concurrent start (double click) won: return that one.
                await self._uow.rollback()
                continue
        raise ExamConflict("Another exam start is in progress. Please try again.")

    async def _create_attempt(self, course, user_id: uuid.UUID, name: str) -> ActiveAttemptView:
        state = await self._pool.state(course.id)
        module_ids = {m.id for m in state.modules}
        titles = {m.id: m.title for m in state.modules}
        rows = await self._questions.live_candidates(course.id)
        # Questions of modules without published lessons can't be drawn; any
        # row of another course is kept so the draw guard refuses it loudly.
        candidates = [
            DrawCandidate(id=r.id, course_id=r.course_id, module_id=r.module_id, difficulty=r.difficulty, concept=r.concept)
            for r in rows
            if r.course_id != course.id or r.module_id in module_ids
        ]
        seen = await self._attempts.seen_questions(user_id, course.id)
        count = self._settings.exam_question_count
        try:
            drawn = draw(
                course.id,
                [DrawModule(id=m.id, lesson_count=m.published_lessons) for m in state.modules],
                candidates,
                seen,
                count,
                self._random.draw_random(),
            )
        except CrossCourseQuestion:
            logger.critical("Draw guard: a question outside course %s reached the draw", course.id)
            raise
        ids = [d.question_id for d in drawn]
        serve_rows = {r.id: r for r in await self._questions.load_for_serving(course.id, ids)}
        # Start guard: every drawn id is a live question of this course.
        if len(serve_rows) != len(ids) or any(r.course_id != course.id for r in serve_rows.values()):
            logger.critical(
                "Start guard: %d of %d drawn questions are not live questions of course %s",
                len(ids) - len(serve_rows), len(ids), course.id,
            )  # fmt: skip
            raise CrossCourseQuestion()

        payload = []
        for d in drawn:
            row = serve_rows[d.question_id]
            payload.append(
                {
                    "question_id": str(row.id),
                    "prompt": row.prompt,
                    "options": [row.options[original] for original in d.permutation],
                    "permutation": list(d.permutation),
                    "correct_index": row.correct_index,
                    "module_title": titles.get(row.module_id, "General"),
                }
            )
        now = self._clock.now()
        attempt = AttemptRecord(
            id=self._ids.new_uuid(),
            user_id=user_id,
            course_id=course.id,
            recipient_name=name,
            question_ids=ids,
            answers={},
            tab_switches=0,
            status="active",
            started_at=now,
            expires_at=now + timedelta(minutes=self._settings.exam_duration_minutes),
            submitted_at=None,
            score=None,
            total=len(ids),
            passed=None,
            module_breakdown=None,
            results=None,
        )
        try:
            async with self._uow.savepoint():
                await self._attempts.insert(attempt)
            await self._cache.put(
                attempt.id,
                user_id,
                payload,
                attempt.expires_at + timedelta(seconds=self._settings.exam_submit_grace_seconds) + CACHE_MARGIN,
            )
            await self._questions.increment_served(ids)
            await self._uow.commit()
        except ActiveAttemptExists:
            raise
        except Exception:
            # The attempt row never committed, so any dictionary entry for its
            # random id is unreachable (reads check the row first) and expires.
            await self._uow.rollback()
            raise
        logger.info("Started attempt %s (user %s, course %s)", attempt.id, user_id, course.id)
        return self._active_view(attempt, payload)

    # ------------------------------------------------------------------ #
    # Get / ping
    # ------------------------------------------------------------------ #
    @guarded("get attempt")
    async def get(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> ActiveAttemptView | GradedAttemptView:
        attempt = await self._owned(attempt_id, user_id)
        if attempt.status == "lost":
            raise AttemptLost()
        if attempt.status == "active":
            attempt, payload = await self._open_active(attempt)
            if payload is not None:
                return self._active_view(attempt, payload)
        return GradedAttemptView(attempt_id=attempt.id, status=attempt.status, result=await self._result(attempt))

    @guarded("ping attempt")
    async def ping(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> PingView:
        attempt = await self._owned(attempt_id, user_id)
        if attempt.status == "lost":
            raise AttemptLost()
        if attempt.status == "active":
            if self._lifecycle.is_overdue(attempt, self._clock.now()):
                final = await self._lifecycle.grade(attempt.id)
                if final is None or final.status == "lost":
                    raise AttemptLost()
                attempt = final
            elif not await self._cache.exists(attempt.id, user_id):
                await self._lifecycle.mark_lost(attempt, "cache_missing")
                raise AttemptLost()
        return PingView(status=attempt.status, expires_at=attempt.expires_at, server_now=self._clock.now())

    # ------------------------------------------------------------------ #
    # Save
    # ------------------------------------------------------------------ #
    @guarded("save answers")
    async def save(
        self, attempt_id: uuid.UUID, user_id: uuid.UUID, answers: list[tuple[uuid.UUID, int]], tab_switches: int
    ) -> datetime:
        self._limit("save", user_id, SAVE_LIMIT)
        attempt = await self._owned(attempt_id, user_id)
        now = self._clock.now()
        if attempt.status == "lost":
            raise AttemptLost()
        save_grace = timedelta(seconds=self._settings.exam_save_grace_seconds)
        if attempt.status != "active" or now >= attempt.expires_at + save_grace:
            raise AttemptClosed()

        allowed = {str(q) for q in attempt.question_ids}
        if len(answers) > len(allowed):
            raise ExamValidationError("answers: more answers than questions")
        clean: dict[str, int] = {}
        for question_id, option_index in answers:
            key = str(question_id)
            if key not in allowed:
                raise ExamValidationError("answers: question_id does not belong to this attempt")
            if key in clean:
                raise ExamValidationError("answers: question_id appears more than once")
            if not 0 <= option_index <= 3:
                raise ExamValidationError("answers: option_index must be 0-3")
            clean[key] = option_index

        try:
            saved = await self._attempts.save_answers(attempt.id, user_id, clean, max(0, tab_switches), now - save_grace)
            if not saved:
                await self._uow.rollback()
                latest = await self._attempts.get(attempt.id)
                if latest is None or latest.user_id != user_id:
                    raise AttemptNotFound()
                if latest.status == "lost":
                    raise AttemptLost()
                raise AttemptClosed()
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise
        return now

    # ------------------------------------------------------------------ #
    # Submit
    # ------------------------------------------------------------------ #
    @guarded("submit attempt")
    async def submit(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> ResultView:
        attempt = await self._owned(attempt_id, user_id)
        if attempt.status == "lost":
            raise AttemptLost()
        if attempt.graded:
            return await self._result(attempt)  # idempotent, never rate limited
        if not self._lifecycle.is_overdue(attempt, self._clock.now()):
            # Time-up submits always grade (contract rule 4); only early ones
            # are limited.
            self._limit("submit", user_id, SUBMIT_LIMIT)
        final = await self._lifecycle.grade(attempt.id)
        if final is None:
            raise AttemptNotFound()
        if final.status == "lost":
            raise AttemptLost()
        return await self._result(final)

    # ------------------------------------------------------------------ #
    # Reports
    # ------------------------------------------------------------------ #
    @guarded("report question")
    async def report(
        self, attempt_id: uuid.UUID, user_id: uuid.UUID, question_id: uuid.UUID, reason: str, comment: str
    ) -> bool:
        """True when the report caused the question to be quarantined."""
        self._limit("report", user_id, REPORT_LIMIT)
        attempt = await self._owned(attempt_id, user_id)
        if attempt.status == "lost":
            raise AttemptLost()
        if attempt.status != "active":
            raise AttemptClosed()
        if question_id not in set(attempt.question_ids):
            raise ExamValidationError("question_id does not belong to this attempt")
        if reason not in REPORT_REASONS:
            raise ExamValidationError("reason is not supported")
        quarantined = False
        try:
            added = await self._reports.add(question_id, attempt.id, user_id, reason, (comment or "")[:500])
            if added:
                reporters = await self._reports.distinct_open_reporters(question_id)
                if reporters >= self._settings.exam_reports_to_quarantine:
                    quarantined = bool(await self._questions.set_status([question_id], "quarantined", ("live",)))
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise
        if quarantined:
            logger.warning("Question %s quarantined after %d learner reports", question_id, reporters)
        return quarantined
