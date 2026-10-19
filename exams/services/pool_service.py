"""Pool targets, readiness and visibility (plan section 6.2)."""

import logging
import uuid
from dataclasses import dataclass

from exams.domain.pool import module_targets, pool_is_full
from exams.protocols import Clock, CourseReader, QuestionStore, UnitOfWork
from exams.records import CourseRecord, ModuleRecord

logger = logging.getLogger("webnest.exams.pool")


@dataclass(frozen=True)
class PoolState:
    modules: list[ModuleRecord]
    targets: dict[uuid.UUID, int]
    live: dict[uuid.UUID, int]

    @property
    def full(self) -> bool:
        return pool_is_full(self.targets, self.live)

    @property
    def total_live(self) -> int:
        return sum(self.live.get(m, 0) for m in self.targets)

    @property
    def total_target(self) -> int:
        return sum(self.targets.values())

    def gaps(self) -> dict[uuid.UUID, int]:
        return {m: t - self.live.get(m, 0) for m, t in self.targets.items() if self.live.get(m, 0) < t}


class PoolService:
    def __init__(self, courses: CourseReader, questions: QuestionStore, uow: UnitOfWork, clock: Clock, settings) -> None:
        self._courses = courses
        self._questions = questions
        self._uow = uow
        self._clock = clock
        self._settings = settings

    def pool_size(self) -> int:
        """Generation target, never below one full exam."""
        return max(int(self._settings.exam_pool_target), self._settings.exam_question_count)

    def ready_size(self) -> int:
        """Live questions a course needs before its exam first appears: at
        least one full exam, at most the pool target."""
        ready = getattr(self._settings, "exam_ready_target", None) or self._settings.exam_pool_target
        return min(max(int(ready), self._settings.exam_question_count), self.pool_size())

    async def state(self, course_id: uuid.UUID, size: int | None = None) -> PoolState:
        """Module targets for `size` live questions (default: the generation target)."""
        modules = [m for m in await self._courses.list_exam_modules(course_id) if m.published_lessons > 0]
        targets = module_targets(
            {m.id: m.published_lessons for m in modules},
            size if size is not None else self.pool_size(),
            self._settings.exam_pool_min_per_module,
        )
        live = await self._questions.live_counts(course_id)
        return PoolState(modules=modules, targets=targets, live=live)

    async def is_visible(self, course: CourseRecord) -> bool:
        """Learners see the exam once every module first reached its share;
        after that it stays visible while the pool can still supply a full
        draw (a quarantined question doesn't hide a launched exam)."""
        if not course.exam_enabled:
            return False
        state = await self.state(course.id, self.ready_size())
        if state.total_live < self._settings.exam_question_count:
            return False
        if course.exam_ready_at is not None:
            return True
        if state.full:
            await self.mark_ready(course.id)
            return True
        return False

    async def mark_ready(self, course_id: uuid.UUID) -> None:
        try:
            await self._courses.mark_ready(course_id, self._clock.now())
            await self._uow.commit()
            logger.info("Exam pool of course %s is ready; the exam is now visible", course_id)
        except Exception:
            await self._uow.rollback()
            # Visibility is recomputed on the next request; never fail the caller.
            logger.warning("Could not record exam readiness for course %s", course_id, exc_info=True)
