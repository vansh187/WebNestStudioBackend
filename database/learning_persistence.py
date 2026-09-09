import uuid
from datetime import date, datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.base_persistence import BasePersistence
from database.models import (
    Course,
    CourseModule,
    Lesson,
    LessonProblemMap,
    Quiz,
    QuizAttempt,
    UserCourseProgress,
    UserLessonBookmark,
    UserLessonNote,
    UserLessonProgress,
)


def _as_uuid(value) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


class LearningPersistence(BasePersistence):
    """DB access for the study-material side: courses, modules, lessons,
    quizzes, and per-user progress / bookmarks / notes."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    # ------------------------------------------------------------------ #
    # Courses
    # ------------------------------------------------------------------ #
    async def list_published_courses(self) -> list[Course]:
        result = await self._execute(
            select(Course)
            .where(Course.status == "published")
            .order_by(Course.display_order, Course.title)
        )
        return list(result.scalars().all())

    async def get_course(
        self, identifier: str | uuid.UUID, include_unpublished: bool = False
    ) -> Course | None:
        as_uuid = _as_uuid(identifier)
        match = Course.slug == str(identifier)
        if as_uuid is not None:
            match = or_(match, Course.id == as_uuid)
        stmt = (
            select(Course)
            .where(match)
            .options(selectinload(Course.modules).selectinload(CourseModule.lessons))
        )
        if not include_unpublished:
            stmt = stmt.where(Course.status == "published")
        result = await self._execute(stmt.limit(1))
        return result.scalars().first()

    async def published_lessons_count(self, course_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not course_ids:
            return {}
        result = await self._execute(
            select(Lesson.course_id, func.count(Lesson.id))
            .where(Lesson.course_id.in_(course_ids), Lesson.status == "published")
            .group_by(Lesson.course_id)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    # ------------------------------------------------------------------ #
    # Lessons
    # ------------------------------------------------------------------ #
    async def get_lesson(
        self, lesson_id: uuid.UUID, include_unpublished: bool = False
    ) -> Lesson | None:
        stmt = (
            select(Lesson)
            .where(Lesson.id == lesson_id)
            .options(
                selectinload(Lesson.course),
                selectinload(Lesson.practice_links).selectinload(LessonProblemMap.problem),
            )
        )
        if not include_unpublished:
            stmt = stmt.where(Lesson.status == "published")
        result = await self._execute(stmt)
        return result.scalar_one_or_none()

    async def lesson_progress_map(
        self, user_id: uuid.UUID, lesson_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, UserLessonProgress]:
        if not lesson_ids:
            return {}
        result = await self._execute(
            select(UserLessonProgress).where(
                UserLessonProgress.user_id == user_id,
                UserLessonProgress.lesson_id.in_(lesson_ids),
            )
        )
        return {row.lesson_id: row for row in result.scalars().all()}

    async def lesson_user_state(
        self, user_id: uuid.UUID, lesson_id: uuid.UUID
    ) -> tuple[UserLessonProgress | None, UserLessonBookmark | None, UserLessonNote | None]:
        progress = (
            await self._execute(
                select(UserLessonProgress).where(
                    UserLessonProgress.user_id == user_id,
                    UserLessonProgress.lesson_id == lesson_id,
                )
            )
        ).scalar_one_or_none()
        bookmark = (
            await self._execute(
                select(UserLessonBookmark).where(
                    UserLessonBookmark.user_id == user_id,
                    UserLessonBookmark.lesson_id == lesson_id,
                )
            )
        ).scalar_one_or_none()
        note = (
            await self._execute(
                select(UserLessonNote).where(
                    UserLessonNote.user_id == user_id,
                    UserLessonNote.lesson_id == lesson_id,
                )
            )
        ).scalar_one_or_none()
        return progress, bookmark, note

    async def upsert_lesson_progress(
        self,
        user_id: uuid.UUID,
        lesson_id: uuid.UUID,
        status: str,
        completed_percent: int,
        add_time_seconds: int,
    ) -> UserLessonProgress:
        now = datetime.now(timezone.utc)
        stmt = (
            pg_insert(UserLessonProgress)
            .values(
                user_id=user_id,
                lesson_id=lesson_id,
                status=status,
                completed_percent=max(0, min(completed_percent, 100)),
                time_spent_seconds=max(0, add_time_seconds),
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[UserLessonProgress.user_id, UserLessonProgress.lesson_id],
                set_={
                    "status": status,
                    "completed_percent": max(0, min(completed_percent, 100)),
                    "time_spent_seconds": UserLessonProgress.time_spent_seconds + max(0, add_time_seconds),
                    "updated_at": now,
                },
            )
            .returning(UserLessonProgress.id)
        )
        await self._execute(stmt)
        await self._commit()
        result = await self._execute(
            select(UserLessonProgress).where(
                UserLessonProgress.user_id == user_id,
                UserLessonProgress.lesson_id == lesson_id,
            )
        )
        return result.scalar_one()

    async def upsert_bookmark(
        self, user_id: uuid.UUID, lesson_id: uuid.UUID, bookmarked: bool
    ) -> UserLessonBookmark:
        now = datetime.now(timezone.utc)
        stmt = (
            pg_insert(UserLessonBookmark)
            .values(user_id=user_id, lesson_id=lesson_id, bookmarked=bookmarked, updated_at=now)
            .on_conflict_do_update(
                index_elements=[UserLessonBookmark.user_id, UserLessonBookmark.lesson_id],
                set_={"bookmarked": bookmarked, "updated_at": now},
            )
        )
        await self._execute(stmt)
        await self._commit()
        result = await self._execute(
            select(UserLessonBookmark).where(
                UserLessonBookmark.user_id == user_id,
                UserLessonBookmark.lesson_id == lesson_id,
            )
        )
        return result.scalar_one()

    async def upsert_note(
        self, user_id: uuid.UUID, lesson_id: uuid.UUID, note: str
    ) -> UserLessonNote:
        now = datetime.now(timezone.utc)
        stmt = (
            pg_insert(UserLessonNote)
            .values(user_id=user_id, lesson_id=lesson_id, note=note, updated_at=now)
            .on_conflict_do_update(
                index_elements=[UserLessonNote.user_id, UserLessonNote.lesson_id],
                set_={"note": note, "updated_at": now},
            )
        )
        await self._execute(stmt)
        await self._commit()
        result = await self._execute(
            select(UserLessonNote).where(
                UserLessonNote.user_id == user_id,
                UserLessonNote.lesson_id == lesson_id,
            )
        )
        return result.scalar_one()

    async def recompute_course_progress(
        self, user_id: uuid.UUID, course_id: uuid.UUID
    ) -> int:
        total = int(
            (
                await self._execute(
                    select(func.count(Lesson.id)).where(
                        Lesson.course_id == course_id, Lesson.status == "published"
                    )
                )
            ).scalar_one()
            or 0
        )
        done = int(
            (
                await self._execute(
                    select(func.count(UserLessonProgress.id))
                    .join(Lesson, Lesson.id == UserLessonProgress.lesson_id)
                    .where(
                        UserLessonProgress.user_id == user_id,
                        Lesson.course_id == course_id,
                        Lesson.status == "published",
                        UserLessonProgress.status == "completed",
                    )
                )
            ).scalar_one()
            or 0
        )
        percent = int(round(done / total * 100)) if total else 0
        now = datetime.now(timezone.utc)
        await self._execute(
            pg_insert(UserCourseProgress)
            .values(user_id=user_id, course_id=course_id, completion_percent=percent, updated_at=now)
            .on_conflict_do_update(
                index_elements=[UserCourseProgress.user_id, UserCourseProgress.course_id],
                set_={"completion_percent": percent, "updated_at": now},
            )
        )
        await self._commit()
        return percent

    # ------------------------------------------------------------------ #
    # Quizzes
    # ------------------------------------------------------------------ #
    async def get_quiz(
        self, quiz_id: uuid.UUID, include_unpublished: bool = False
    ) -> Quiz | None:
        stmt = (
            select(Quiz).where(Quiz.id == quiz_id).options(selectinload(Quiz.questions))
        )
        if not include_unpublished:
            stmt = stmt.where(Quiz.status == "published")
        result = await self._execute(stmt)
        return result.scalar_one_or_none()

    async def has_passed_quiz(self, user_id: uuid.UUID, quiz_id: uuid.UUID) -> bool:
        result = await self._execute(
            select(QuizAttempt.id)
            .where(
                QuizAttempt.user_id == user_id,
                QuizAttempt.quiz_id == quiz_id,
                QuizAttempt.passed.is_(True),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def add_quiz_attempt(
        self,
        user_id: uuid.UUID,
        quiz_id: uuid.UUID,
        score: int,
        total: int,
        passed: bool,
        answers: list,
    ) -> QuizAttempt:
        attempt = QuizAttempt(
            user_id=user_id,
            quiz_id=quiz_id,
            score=max(0, score),
            total=max(0, total),
            passed=passed,
            answers=answers,
        )
        self._session.add(attempt)
        await self._commit()
        await self._refresh(attempt)
        return attempt

    async def count_passed_quizzes(self, user_id: uuid.UUID) -> int:
        result = await self._execute(
            select(func.count(func.distinct(QuizAttempt.quiz_id))).where(
                QuizAttempt.user_id == user_id, QuizAttempt.passed.is_(True)
            )
        )
        return int(result.scalar_one() or 0)

    # ------------------------------------------------------------------ #
    # Learning dashboard aggregates
    # ------------------------------------------------------------------ #
    async def count_courses_with_progress(self, user_id: uuid.UUID) -> int:
        result = await self._execute(
            select(func.count(UserCourseProgress.id)).where(
                UserCourseProgress.user_id == user_id
            )
        )
        return int(result.scalar_one() or 0)

    async def count_completed_lessons(self, user_id: uuid.UUID) -> int:
        result = await self._execute(
            select(func.count(UserLessonProgress.id)).where(
                UserLessonProgress.user_id == user_id,
                UserLessonProgress.status == "completed",
            )
        )
        return int(result.scalar_one() or 0)

    async def list_course_progress(
        self, user_id: uuid.UUID
    ) -> list[tuple[Course, int]]:
        result = await self._execute(
            select(Course, UserCourseProgress.completion_percent)
            .join(UserCourseProgress, UserCourseProgress.course_id == Course.id)
            .where(UserCourseProgress.user_id == user_id, Course.status == "published")
            .order_by(UserCourseProgress.updated_at.desc())
        )
        return [(row[0], int(row[1] or 0)) for row in result.all()]

    async def pick_continue_lesson(self, user_id: uuid.UUID) -> Lesson | None:
        result = await self._execute(
            select(Lesson)
            .join(UserLessonProgress, UserLessonProgress.lesson_id == Lesson.id)
            .options(selectinload(Lesson.course))
            .where(
                UserLessonProgress.user_id == user_id,
                UserLessonProgress.status == "in_progress",
                Lesson.status == "published",
            )
            .order_by(UserLessonProgress.updated_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def recent_lesson_completions(
        self, user_id: uuid.UUID, limit: int
    ) -> list[tuple[Lesson, datetime]]:
        result = await self._execute(
            select(Lesson, UserLessonProgress.updated_at)
            .join(UserLessonProgress, UserLessonProgress.lesson_id == Lesson.id)
            .where(
                UserLessonProgress.user_id == user_id,
                UserLessonProgress.status == "completed",
            )
            .order_by(UserLessonProgress.updated_at.desc())
            .limit(max(1, min(limit, 50)))
        )
        return [(row[0], row[1]) for row in result.all()]

    async def recent_quiz_attempts(
        self, user_id: uuid.UUID, limit: int
    ) -> list[QuizAttempt]:
        result = await self._execute(
            select(QuizAttempt)
            .where(QuizAttempt.user_id == user_id)
            .order_by(QuizAttempt.submitted_at.desc())
            .limit(max(1, min(limit, 50)))
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------ #
    # Admin - course tree (upsert only; never hard-deletes learner-linked rows)
    # ------------------------------------------------------------------ #
    async def get_course_admin(self, slug: str) -> Course | None:
        return await self._load_course_tree(slug=slug)

    async def _load_course_tree(
        self, *, slug: str | None = None, course_id: uuid.UUID | None = None
    ) -> Course | None:
        """Fully eager-loaded course -> modules -> lessons -> practice_links so
        the admin upsert never triggers a lazy load in the async session."""
        stmt = select(Course).options(
            selectinload(Course.modules)
            .selectinload(CourseModule.lessons)
            .selectinload(Lesson.practice_links)
            .selectinload(LessonProblemMap.problem)
        )
        if course_id is not None:
            stmt = stmt.where(Course.id == course_id)
        else:
            stmt = stmt.where(Course.slug == str(slug))
        result = await self._execute(stmt.limit(1))
        return result.scalars().first()

    async def upsert_course_tree(
        self, slug: str | None, course_fields: dict, modules: list[dict]
    ) -> Course:
        target_slug = slug or course_fields.get("slug")
        course = await self._load_course_tree(slug=target_slug) if target_slug else None

        if course is None:
            # New objects get their child collections initialised explicitly;
            # touching an un-loaded relationship here would attempt lazy IO.
            course = Course(**course_fields, modules=[])
            self._session.add(course)
            await self._session.flush()
        else:
            for key, value in course_fields.items():
                setattr(course, key, value)
            await self._session.flush()

        existing_modules = {m.id: m for m in course.modules}
        for m_index, module_payload in enumerate(modules):
            module = existing_modules.get(module_payload.get("id"))
            new_module = module is None
            if new_module:
                module = CourseModule(course_id=course.id, lessons=[])
                self._session.add(module)
            module.title = module_payload["title"]
            module.display_order = int(module_payload.get("display_order", m_index))
            module.status = module_payload.get("status", "published")
            await self._session.flush()

            existing_lessons = {} if new_module else {l.id: l for l in (module.lessons or [])}
            for l_index, lesson_payload in enumerate(module_payload.get("lessons", [])):
                lesson = existing_lessons.get(lesson_payload.get("id"))
                new_lesson = lesson is None
                if new_lesson:
                    lesson = Lesson(module_id=module.id, course_id=course.id, practice_links=[])
                    self._session.add(lesson)
                lesson.module_id = module.id
                lesson.course_id = course.id
                lesson.title = lesson_payload["title"]
                lesson.content = lesson_payload.get("content")
                lesson.resources = lesson_payload.get("resources") or []
                lesson.estimated_minutes = int(lesson_payload.get("estimated_minutes", 8))
                lesson.status = lesson_payload.get("status", "draft")
                lesson.display_order = int(lesson_payload.get("display_order", l_index))
                await self._session.flush()
                await self._set_lesson_practice(
                    lesson, lesson_payload.get("practice_problem_ids") or [], new_lesson
                )

        await self._commit(conflict_message="A course with this slug already exists")
        reloaded = await self._load_course_tree(course_id=course.id)
        return reloaded if reloaded is not None else course

    async def _set_lesson_practice(
        self, lesson: Lesson, problem_ids: list[uuid.UUID], is_new_lesson: bool = False
    ) -> None:
        wanted = {pid for pid in problem_ids}
        current = {} if is_new_lesson else {link.problem_id: link for link in (lesson.practice_links or [])}
        for pid in wanted - set(current):
            self._session.add(LessonProblemMap(lesson_id=lesson.id, problem_id=pid))
        for pid, link in current.items():
            if pid not in wanted:
                await self._session.delete(link)
        await self._session.flush()

    async def resolve_problem_ids_by_slug(self, slugs: list[str]) -> dict[str, uuid.UUID]:
        from database.models import CodelabProblem

        if not slugs:
            return {}
        result = await self._execute(
            select(CodelabProblem.slug, CodelabProblem.id).where(CodelabProblem.slug.in_(slugs))
        )
        return {row[0]: row[1] for row in result.all()}

    async def practice_slugs_for_lessons(
        self, lesson_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[str]]:
        from database.models import CodelabProblem

        if not lesson_ids:
            return {}
        result = await self._execute(
            select(LessonProblemMap.lesson_id, CodelabProblem.slug)
            .join(CodelabProblem, CodelabProblem.id == LessonProblemMap.problem_id)
            .where(LessonProblemMap.lesson_id.in_(lesson_ids))
        )
        out: dict[uuid.UUID, list[str]] = {}
        for lesson_id, slug in result.all():
            out.setdefault(lesson_id, []).append(slug)
        return out
