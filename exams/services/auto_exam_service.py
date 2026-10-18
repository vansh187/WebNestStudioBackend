"""Fully automatic exams (EXAM_ENABLED=true): every published course gets its
exam switched on with an exam code, no admin steps. Courses listed in
EXAM_AUTO_EXCLUDE stay off. The refill job then builds each pool from the
lesson content in the database, and the exam appears to learners once the
pool is ready; each learner can take it after completing 100% of the course."""

import logging

from exams.domain.certificate_id import EXAM_CODE_PATTERN
from exams.errors import ExamConflict
from exams.protocols import CourseReader, UnitOfWork
from exams.records import CourseRecord
from exams.services.certificate_service import fallback_exam_code
from exams.services.common import audit

logger = logging.getLogger("webnest.exams.auto")

# Contract section 10: course slugs and their certificate codes.
COURSE_EXAM_CODES = {
    "java-core": "JAVA",
    "advanced-java": "ADVJAVA",
    "spring-framework": "SPRING",
    "spring-boot": "BOOT",
    "python": "PY",
    "html": "HTML",
    "css": "CSS",
    "javascript": "JS",
    "react": "REACT",
    "database-sql": "SQL",
    "postgresql": "PG",
    "database-design": "DBDESIGN",
}


def excluded_slugs(settings) -> set[str]:
    """EXAM_AUTO_EXCLUDE as lower-cased slugs; compare with is_excluded()."""
    return {s.strip().lower() for s in (getattr(settings, "exam_auto_exclude", "") or "").split(",") if s.strip()}


def is_excluded(slug: str, settings) -> bool:
    return (slug or "").strip().lower() in excluded_slugs(settings)


def pick_exam_code(slug: str, current: str | None, used: set[str]) -> str:
    """The course's own code if it has a valid one, else the contract's code,
    else one derived from the slug - made unique among `used` (the other
    courses' codes)."""
    if current and EXAM_CODE_PATTERN.fullmatch(current):
        return current
    base = COURSE_EXAM_CODES.get((slug or "").lower()) or fallback_exam_code(slug)
    if base not in used:
        return base
    for n in range(2, 1000):
        suffix = str(n)
        candidate = base[: 12 - len(suffix)] + suffix
        if candidate not in used and EXAM_CODE_PATTERN.fullmatch(candidate):
            return candidate
    raise RuntimeError(f"no free exam code for {slug}")


class AutoExamService:
    def __init__(self, courses: CourseReader, uow: UnitOfWork, settings) -> None:
        self._courses = courses
        self._uow = uow
        self._settings = settings

    async def enable_all(self) -> list[CourseRecord]:
        """Switches on every published course whose exam is off, except those
        an admin switched off (exam_admin_disabled) or listed in
        EXAM_AUTO_EXCLUDE. Returns the courses it switched on. Never raises:
        a problem with one course is logged and the rest still go ahead."""
        if not self._settings.exam_enabled:
            return []
        try:
            courses = await self._courses.list_published_courses()
            used = set(await self._courses.used_exam_codes())
        except Exception:
            await self._uow.rollback()
            logger.error("Automatic exams: could not list courses", exc_info=True)
            return []
        switched: list[CourseRecord] = []
        for course in courses:
            if course.exam_enabled or course.exam_admin_disabled or is_excluded(course.slug, self._settings):
                continue
            try:
                code = pick_exam_code(course.slug, course.exam_code, used)
                async with self._uow.savepoint():
                    await self._courses.set_exam_settings(course.id, True, code)
                await self._uow.commit()
            except ExamConflict:
                # Another process took the code at the same moment; next run retries.
                await self._uow.rollback()
                logger.warning("Automatic exams: the code for %s was taken concurrently; will retry", course.slug)
                continue
            except Exception:
                await self._uow.rollback()
                logger.error("Automatic exams: could not switch on %s", course.slug, exc_info=True)
                continue
            used.add(code)
            audit("exam_auto_enable", "system", course=course.slug, code=code)
            switched.append(course)
        if switched:
            logger.info("Automatic exams switched on for: %s", ", ".join(c.slug for c in switched))
        return switched
