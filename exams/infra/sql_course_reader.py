"""Course, module, lesson and progress reads for the exams."""

import uuid
from datetime import datetime

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.orm import aliased

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from database.models import Course, CourseModule, Lesson, Quiz, QuizQuestion, UserLessonProgress
from exams.errors import ExamConflict, ExamStoreUnavailable
from exams.infra.sql_base import SqlStore, constraint_name
from exams.records import CourseRecord, LessonMaterial, LessonRecord, ModuleRecord

_COURSE_COLUMNS = (
    Course.id,
    Course.slug,
    Course.title,
    Course.status,
    Course.exam_code,
    Course.exam_enabled,
    Course.exam_ready_at,
    Course.exam_admin_disabled,
)


def _course(row) -> CourseRecord:
    return CourseRecord(
        id=row.id,
        slug=row.slug,
        title=row.title,
        status=row.status,
        exam_code=row.exam_code,
        exam_enabled=bool(row.exam_enabled),
        exam_ready_at=row.exam_ready_at,
        exam_admin_disabled=bool(row.exam_admin_disabled),
    )


class SqlCourseReader(SqlStore):
    async def _one(self, condition, context: str) -> CourseRecord | None:
        result = await self._execute(select(*_COURSE_COLUMNS).where(condition).limit(1), context)
        row = result.first()
        return _course(row) if row is not None else None

    async def get_published_course(self, slug: str) -> CourseRecord | None:
        return await self._one(and_(Course.slug == slug, Course.status == "published"), "get published course")

    async def get_course(self, slug: str) -> CourseRecord | None:
        return await self._one(Course.slug == slug, "get course")

    async def get_course_by_id(self, course_id: uuid.UUID) -> CourseRecord | None:
        return await self._one(Course.id == course_id, "get course by id")

    async def list_exam_modules(self, course_id: uuid.UUID) -> list[ModuleRecord]:
        lesson_count = func.count(Lesson.id).filter(Lesson.status == "published")
        result = await self._execute(
            select(CourseModule.id, CourseModule.key, CourseModule.title, CourseModule.display_order, lesson_count)
            .outerjoin(Lesson, Lesson.module_id == CourseModule.id)
            .where(CourseModule.course_id == course_id)
            .group_by(CourseModule.id)
            .order_by(CourseModule.display_order, CourseModule.title),
            "list exam modules",
        )
        return [
            ModuleRecord(id=r[0], key=r[1], title=r[2], display_order=r[3], published_lessons=int(r[4] or 0))
            for r in result.all()
        ]

    async def list_lessons(self, course_id: uuid.UUID) -> list[LessonRecord]:
        result = await self._execute(
            select(Lesson.id, Lesson.key, Lesson.module_id, Lesson.course_id, Lesson.title, Lesson.status)
            .where(Lesson.course_id == course_id)
            .order_by(Lesson.display_order),
            "list lessons",
        )
        return [
            LessonRecord(id=r[0], key=r[1], module_id=r[2], course_id=r[3], title=r[4], status=r[5])
            for r in result.all()
        ]

    async def set_exam_settings(
        self, course_id: uuid.UUID, enabled: bool, exam_code: str | None, admin_disabled: bool | None = None
    ) -> None:
        values = {"exam_enabled": enabled, "exam_code": exam_code}
        if admin_disabled is not None:
            values["exam_admin_disabled"] = admin_disabled
        try:
            await self._session.execute(update(Course).where(Course.id == course_id).values(**values))
        except IntegrityError as exc:
            if "uq_courses_exam_code" in constraint_name(exc):
                raise ExamConflict("This exam code is already used by another course.") from exc
            raise ExamStoreUnavailable() from exc
        except SQLAlchemyError as exc:
            raise ExamStoreUnavailable() from exc

    async def mark_ready(self, course_id: uuid.UUID, now: datetime) -> None:
        await self._execute(
            update(Course).where(Course.id == course_id, Course.exam_ready_at.is_(None)).values(exam_ready_at=now),
            "mark exam ready",
        )

    async def list_published_courses(self) -> list[CourseRecord]:
        result = await self._execute(
            select(*_COURSE_COLUMNS).where(Course.status == "published").order_by(Course.display_order),
            "list published courses",
        )
        return [_course(row) for row in result.all()]

    async def used_exam_codes(self) -> set[str]:
        result = await self._execute(select(Course.exam_code).where(Course.exam_code.is_not(None)), "list exam codes")
        return set(result.scalars().all())

    async def lesson_materials(self, course_id: uuid.UUID) -> list[LessonMaterial]:
        lessons = (
            await self._execute(
                select(Lesson.id, Lesson.key, Lesson.module_id, Lesson.title, Lesson.content)
                .where(Lesson.course_id == course_id, Lesson.status == "published")
                .order_by(Lesson.display_order),
                "load lesson materials",
            )
        ).all()
        prompts: dict = {}
        if lessons:
            rows = (
                await self._execute(
                    select(Quiz.lesson_id, QuizQuestion.prompt)
                    .join(QuizQuestion, QuizQuestion.quiz_id == Quiz.id)
                    .where(Quiz.lesson_id.in_([row[0] for row in lessons]), Quiz.status == "published"),
                    "load lesson quiz prompts",
                )
            ).all()
            for lesson_id, prompt in rows:
                prompts.setdefault(lesson_id, []).append(prompt)
        return [
            LessonMaterial(
                lesson_id=row[0], key=row[1], module_id=row[2], title=row[3],
                content=row[4] if isinstance(row[4], dict) else None, quiz_prompts=prompts.get(row[0], []),
            )  # fmt: skip
            for row in lessons
        ]

    async def list_enabled_courses(self) -> list[CourseRecord]:
        result = await self._execute(
            select(*_COURSE_COLUMNS).where(Course.exam_enabled.is_(True)).order_by(Course.display_order),
            "list enabled courses",
        )
        return [_course(row) for row in result.all()]


class SqlProgressReader(SqlStore):
    async def remaining_lessons(self, user_id: uuid.UUID, course_id: uuid.UUID) -> int:
        """A lesson counts as completed when this user completed it, or a
        lesson with the same key in another course (aliases)."""
        alias = aliased(Lesson)
        completed_here = exists().where(
            UserLessonProgress.user_id == user_id,
            UserLessonProgress.lesson_id == Lesson.id,
            UserLessonProgress.status == "completed",
        )
        completed_alias = (
            exists()
            .where(
                UserLessonProgress.user_id == user_id,
                UserLessonProgress.status == "completed",
                UserLessonProgress.lesson_id == alias.id,
                alias.key == Lesson.key,
            )
            .correlate(Lesson)
        )
        result = await self._execute(
            select(func.count(Lesson.id)).where(
                Lesson.course_id == course_id,
                Lesson.status == "published",
                ~completed_here,
                ~and_(Lesson.key.is_not(None), completed_alias),
            ),
            "count remaining lessons",
        )
        return int(result.scalar_one() or 0)
