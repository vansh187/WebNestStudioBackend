import logging
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import DateTime, case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload

from core.exceptions import ConflictError, DatabaseError, DomainError
from database.base_persistence import BasePersistence
from database.models import (
    CodelabProblemProgress,
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


UPSERT_CONFLICT_MESSAGE = "This course conflicts with existing data (duplicate slug, exam code or module/lesson key)"

# GET /api/learning/progress returns at most this many lessons (one row per
# published lesson per user; 558 lessons exist today).
MAX_USER_PROGRESS_ROWS = 5000
# What a progress row with a NULL updated_at counts as: long ago, so it is in
# every full fetch but never re-sent by a `since` refresh.
UNKNOWN_CHANGE_TIME = datetime(1970, 1, 1, tzinfo=timezone.utc)


logger = logging.getLogger("webnest.learning")


def _as_uuid(value) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _course_lookup(identifier: str | uuid.UUID, include_unpublished: bool = False) -> list:
    """The one rule for finding a course by slug or id (and, unless asked
    otherwise, only a published one). get_course and published_course_id both
    use it, so the two can never disagree about which slugs exist."""
    try:
        as_uuid = _as_uuid(identifier)
        match = Course.slug == str(identifier)
        if as_uuid is not None:
            match = or_(match, Course.id == as_uuid)
        conditions = [match]
        if not include_unpublished:
            conditions.append(Course.status == "published")
        return conditions
    except Exception as exc:
        logger.error("Course lookup could not be built type=%s", type(exc).__name__)
        raise DatabaseError("Could not read data from the database right now. Please try again shortly.") from exc


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
        stmt = (
            select(Course)
            .where(*_course_lookup(identifier, include_unpublished))
            .options(selectinload(Course.modules).selectinload(CourseModule.lessons))
        )
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

    async def ordered_published_lessons(self, course_ids: list[uuid.UUID]) -> list[Lesson]:
        """Published lessons in reading order (module order, then lesson
        order). Only the columns needed for navigation/progress are loaded -
        touching any other attribute would trigger a lazy load."""
        if not course_ids:
            return []
        result = await self._execute(
            select(Lesson)
            .join(CourseModule, CourseModule.id == Lesson.module_id)
            .options(load_only(Lesson.id, Lesson.course_id, Lesson.title, Lesson.estimated_minutes))
            .where(Lesson.course_id.in_(course_ids), Lesson.status == "published")
            .order_by(Lesson.course_id, CourseModule.display_order, Lesson.display_order)
        )
        return list(result.scalars().all())

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

    async def resolve_lessons(self, identifier: str, include_unpublished: bool = False) -> list[Lesson]:
        """Lessons matching a UUID or a site lesson key. A key can appear in
        more than one course (aliases), so this may return several lessons,
        ordered by course display order."""
        as_uuid = _as_uuid(identifier)
        match = Lesson.id == as_uuid if as_uuid is not None else Lesson.key == str(identifier)
        stmt = (
            select(Lesson)
            .join(Course, Course.id == Lesson.course_id)
            .where(match)
            .options(
                selectinload(Lesson.course),
                selectinload(Lesson.practice_links).selectinload(LessonProblemMap.problem),
            )
            .order_by(Course.display_order, Course.slug)
        )
        if not include_unpublished:
            stmt = stmt.where(Lesson.status == "published")
        result = await self._execute(stmt)
        return list(result.scalars().unique().all())

    async def published_lessons_by_refs(self, refs: list[str]) -> list[tuple[uuid.UUID, str | None, uuid.UUID]]:
        """(lesson_id, key, course_id) for published lessons whose id or key is in refs."""
        if not refs:
            return []
        uuids = [u for u in (_as_uuid(r) for r in refs) if u is not None]
        keys = [r for r in refs if _as_uuid(r) is None]
        conditions = []
        if uuids:
            conditions.append(Lesson.id.in_(uuids))
        if keys:
            conditions.append(Lesson.key.in_(keys))
        result = await self._execute(
            select(Lesson.id, Lesson.key, Lesson.course_id).where(Lesson.status == "published", or_(*conditions))
        )
        return [tuple(row) for row in result.all()]

    async def bulk_upsert_progress(self, user_id: uuid.UUID, rows: dict[uuid.UUID, tuple[str, int]]) -> None:
        """One statement for many lessons. Never downgrades: completed stays
        completed (and keeps its completion time), percent only grows."""
        if not rows:
            return
        now = datetime.now(timezone.utc)
        values = [
            {
                "user_id": user_id,
                "lesson_id": lesson_id,
                "status": status,
                "completed_percent": {"completed": 100, "not_started": 0}.get(status, max(0, min(percent, 99))),
                "time_spent_seconds": 0,
                "updated_at": now,
            }
            for lesson_id, (status, percent) in rows.items()
        ]
        insert_stmt = pg_insert(UserLessonProgress).values(values)
        existing, incoming = UserLessonProgress, insert_stmt.excluded
        either_completed = or_(existing.status == "completed", incoming.status == "completed")
        new_status = case(
            (either_completed, "completed"),
            (or_(existing.status == "in_progress", incoming.status == "in_progress"), "in_progress"),
            else_="not_started",
        )
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=[UserLessonProgress.user_id, UserLessonProgress.lesson_id],
            set_={
                "status": new_status,
                "completed_percent": case(
                    (either_completed, 100),
                    else_=func.greatest(existing.completed_percent, incoming.completed_percent),
                ),
                "updated_at": case((existing.status == "completed", existing.updated_at), else_=incoming.updated_at),
            },
        )
        await self._execute(stmt)
        await self._commit()

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

    async def published_course_id(self, identifier: str | uuid.UUID) -> uuid.UUID | None:
        """The id of the published course with this slug (or id), found by the
        same rule as get_course but without loading its modules and lessons."""
        try:
            result = await self._execute(select(Course.id).where(*_course_lookup(identifier)).limit(1))
            return result.scalars().first()
        except DomainError:
            raise
        except Exception as exc:
            logger.error("Course lookup failed type=%s", type(exc).__name__)
            raise DatabaseError("Could not read data from the database right now. Please try again shortly.") from exc

    async def list_user_progress(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        since: datetime | None = None,
        limit: int = MAX_USER_PROGRESS_ROWS,
    ) -> tuple[list[tuple], bool]:
        """((key, lesson_id, course_slug, status, completed_percent, changed_at)
        rows, truncated) for published lessons of published courses. One
        query; the (user_id, lesson_id) unique index makes it an index scan on
        this user's rows.

        Without `since`: lessons started or completed (a full picture).
        With `since`: everything changed at or after it, including lessons
        explicitly un-marked to not_started, so a client applying changes can
        clear them.

        A NULL updated_at (rows written before the column had a default) is
        read as 1970-01-01: part of every full fetch, never re-sent by a
        `since` refresh. One extra row is fetched to tell whether the cap cut
        the result; newest rows are kept."""
        try:
            changed_at = func.coalesce(UserLessonProgress.updated_at, literal(UNKNOWN_CHANGE_TIME, DateTime(timezone=True)))
            statuses = ("in_progress", "completed") if since is None else ("not_started", "in_progress", "completed")
            stmt = (
                select(
                    Lesson.key,
                    UserLessonProgress.lesson_id,
                    Course.slug,
                    UserLessonProgress.status,
                    UserLessonProgress.completed_percent,
                    changed_at.label("changed_at"),
                )
                .join(Lesson, Lesson.id == UserLessonProgress.lesson_id)
                .join(Course, Course.id == Lesson.course_id)
                .where(
                    UserLessonProgress.user_id == user_id,
                    UserLessonProgress.status.in_(statuses),
                    Lesson.status == "published",
                    Course.status == "published",
                )
            )
            if course_id is not None:
                stmt = stmt.where(Lesson.course_id == course_id)
            if since is not None:
                stmt = stmt.where(changed_at >= since)
            cap = max(1, int(limit))
            stmt = stmt.order_by(changed_at.desc(), UserLessonProgress.lesson_id).limit(cap + 1)
            rows = [tuple(row) for row in (await self._execute(stmt)).all()]
            truncated = len(rows) > cap
            if truncated:
                logger.warning("Progress listing hit the cap user=%s cap=%s", user_id, cap)
            return rows[:cap], truncated
        except DomainError:
            raise
        except Exception as exc:
            logger.error("User progress listing failed type=%s", type(exc).__name__)
            raise DatabaseError("Could not read data from the database right now. Please try again shortly.") from exc

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

    async def get_lesson_progress(
        self, user_id: uuid.UUID, lesson_id: uuid.UUID
    ) -> UserLessonProgress | None:
        result = await self._execute(
            select(UserLessonProgress).where(
                UserLessonProgress.user_id == user_id,
                UserLessonProgress.lesson_id == lesson_id,
            )
        )
        return result.scalar_one_or_none()

    async def upsert_lesson_progress(
        self,
        user_id: uuid.UUID,
        lesson_id: uuid.UUID,
        status: str,
        completed_percent: int,
        add_time_seconds: int,
        touch_updated_at: bool = True,
    ) -> UserLessonProgress:
        """touch_updated_at=False leaves updated_at alone on an existing row:
        for completed lessons updated_at doubles as the completion time, so
        revisits must not move it."""
        now = datetime.now(timezone.utc)
        set_ = {
            "status": status,
            "completed_percent": max(0, min(completed_percent, 100)),
            "time_spent_seconds": UserLessonProgress.time_spent_seconds + max(0, add_time_seconds),
        }
        if touch_updated_at:
            set_["updated_at"] = now
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
                set_=set_,
            )
            .returning(UserLessonProgress.id)
        )
        await self._execute(stmt)
        await self._commit()
        result = await self._execute(
            select(UserLessonProgress)
            .where(
                UserLessonProgress.user_id == user_id,
                UserLessonProgress.lesson_id == lesson_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one()

    async def bookmarked_lesson_ids(
        self, user_id: uuid.UUID, lesson_ids: list[uuid.UUID]
    ) -> set[uuid.UUID]:
        if not lesson_ids:
            return set()
        result = await self._execute(
            select(UserLessonBookmark.lesson_id).where(
                UserLessonBookmark.user_id == user_id,
                UserLessonBookmark.lesson_id.in_(lesson_ids),
                UserLessonBookmark.bookmarked.is_(True),
            )
        )
        return set(result.scalars().all())

    async def list_bookmarks(
        self, user_id: uuid.UUID, course_id: uuid.UUID | None = None, limit: int = 200
    ) -> list[tuple]:
        """(lesson_id, lesson_title, estimated_minutes, course_slug,
        course_title, module_title, bookmarked_at), newest first. Lessons or
        courses that were unpublished after bookmarking are left out."""
        stmt = (
            select(
                Lesson.id,
                Lesson.title,
                Lesson.estimated_minutes,
                Course.slug,
                Course.title,
                CourseModule.title,
                UserLessonBookmark.updated_at,
            )
            .join(Lesson, Lesson.id == UserLessonBookmark.lesson_id)
            .join(CourseModule, CourseModule.id == Lesson.module_id)
            .join(Course, Course.id == Lesson.course_id)
            .where(
                UserLessonBookmark.user_id == user_id,
                UserLessonBookmark.bookmarked.is_(True),
                Lesson.status == "published",
                Course.status == "published",
            )
            .order_by(UserLessonBookmark.updated_at.desc())
            .limit(max(1, min(limit, 500)))
        )
        if course_id is not None:
            stmt = stmt.where(Lesson.course_id == course_id)
        result = await self._execute(stmt)
        return [tuple(row) for row in result.all()]

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
    async def quizzes_passed_by_course(
        self, user_id: uuid.UUID, course_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        if not course_ids:
            return {}
        result = await self._execute(
            select(Lesson.course_id, func.count(func.distinct(QuizAttempt.quiz_id)))
            .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .join(Lesson, Lesson.id == Quiz.lesson_id)
            .where(
                QuizAttempt.user_id == user_id,
                QuizAttempt.passed.is_(True),
                Lesson.course_id.in_(course_ids),
            )
            .group_by(Lesson.course_id)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    async def problems_solved_by_course(
        self, user_id: uuid.UUID, course_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Solved CodeLab problems that are linked as practice to a published
        lesson of each course."""
        if not course_ids:
            return {}
        result = await self._execute(
            select(Lesson.course_id, func.count(func.distinct(CodelabProblemProgress.problem_id)))
            .join(LessonProblemMap, LessonProblemMap.problem_id == CodelabProblemProgress.problem_id)
            .join(Lesson, Lesson.id == LessonProblemMap.lesson_id)
            .where(
                CodelabProblemProgress.user_id == user_id,
                CodelabProblemProgress.status == "solved",
                Lesson.status == "published",
                Lesson.course_id.in_(course_ids),
            )
            .group_by(Lesson.course_id)
        )
        return {row[0]: int(row[1]) for row in result.all()}

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

    async def recent_lesson_completions(
        self, user_id: uuid.UUID, limit: int, course_id: uuid.UUID | None = None
    ) -> list[tuple[uuid.UUID, str, str, datetime]]:
        """(lesson_id, lesson_title, course_slug, completed_at), newest first."""
        stmt = (
            select(Lesson.id, Lesson.title, Course.slug, UserLessonProgress.updated_at)
            .join(UserLessonProgress, UserLessonProgress.lesson_id == Lesson.id)
            .join(Course, Course.id == Lesson.course_id)
            .where(
                UserLessonProgress.user_id == user_id,
                UserLessonProgress.status == "completed",
            )
            .order_by(UserLessonProgress.updated_at.desc())
            .limit(max(1, min(limit, 50)))
        )
        if course_id is not None:
            stmt = stmt.where(Lesson.course_id == course_id)
        result = await self._execute(stmt)
        return [tuple(row) for row in result.all()]

    async def recent_quiz_attempts(
        self, user_id: uuid.UUID, limit: int, course_id: uuid.UUID | None = None
    ) -> list[tuple[QuizAttempt, str, uuid.UUID | None, str | None]]:
        """(attempt, quiz_title, lesson_id, course_slug), newest first. Quizzes
        not attached to a lesson have no lesson/course and are dropped when
        filtering by course."""
        stmt = (
            select(QuizAttempt, Quiz.title, Lesson.id, Course.slug)
            .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .outerjoin(Lesson, Lesson.id == Quiz.lesson_id)
            .outerjoin(Course, Course.id == Lesson.course_id)
            .where(QuizAttempt.user_id == user_id)
            .order_by(QuizAttempt.submitted_at.desc())
            .limit(max(1, min(limit, 50)))
        )
        if course_id is not None:
            stmt = stmt.where(Lesson.course_id == course_id)
        result = await self._execute(stmt)
        return [tuple(row) for row in result.all()]

    # ------------------------------------------------------------------ #
    # Admin - course tree (upsert only; never hard-deletes learner-linked rows)
    # ------------------------------------------------------------------ #
    async def get_course_admin(self, slug: str) -> Course | None:
        return await self._load_course_tree(slug=slug)

    async def _load_course_tree(
        self, *, slug: str | None = None, course_id: uuid.UUID | None = None, refresh: bool = False
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
        if refresh:
            # After an upsert the identity map still holds new modules whose
            # lessons collection was created empty; reload it from the rows.
            stmt = stmt.execution_options(populate_existing=True)
        result = await self._execute(stmt.limit(1))
        return result.scalars().first()

    async def upsert_course_tree(
        self, slug: str | None, course_fields: dict, modules: list[dict]
    ) -> Course:
        """See _apply_course_tree. Any unique violation while flushing (a
        duplicate exam code, or a key still held by a row the payload didn't
        match) becomes a 409, never a raw database error."""
        try:
            course_id = await self._apply_course_tree(slug, course_fields, modules)
        except IntegrityError as exc:
            await self._session.rollback()
            logger.warning("Course upsert violated a constraint: %s", exc.orig)
            raise ConflictError(UPSERT_CONFLICT_MESSAGE) from exc
        except SQLAlchemyError as exc:
            await self._session.rollback()
            logger.error("Course upsert failed", exc_info=True)
            raise DatabaseError("Could not save the course right now. Please try again shortly.") from exc
        await self._commit(conflict_message=UPSERT_CONFLICT_MESSAGE)
        reloaded = await self._load_course_tree(course_id=course_id, refresh=True)
        if reloaded is None:
            raise DatabaseError("Saved, but could not reload the course. Please retry your request.")
        return reloaded

    async def _apply_course_tree(self, slug: str | None, course_fields: dict, modules: list[dict]) -> uuid.UUID:
        """Modules and lessons are matched by id, then by the site's `key`,
        then - for rows created before keys existed (key is null) - by title,
        so the first keyed sync adopts existing rows (and their learners'
        progress) instead of duplicating them. A lesson may move between
        modules. When the payload carries keys, keyed rows whose key is no
        longer sent are archived (never deleted)."""
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

        existing_modules = list(course.modules)
        existing_lessons = [lesson for module in existing_modules for lesson in (module.lessons or [])]
        modules_by_id = {m.id: m for m in existing_modules}
        modules_by_key = {m.key: m for m in existing_modules if m.key}
        lessons_by_id = {l.id: l for l in existing_lessons}
        lessons_by_key = {l.key: l for l in existing_lessons if l.key}
        new_lessons: set = set()
        used_modules: set = set()
        used_lessons: set = set()
        # Keys are assigned in one pass at the end (see below), so swapping
        # keys between rows in one payload never trips the unique index.
        wanted_keys: list[tuple[object, str]] = []

        def legacy_match(rows, title: str, used: set):
            folded = (title or "").strip().casefold()
            for row in rows:
                if row.key is None and row.id not in used and (row.title or "").strip().casefold() == folded:
                    return row
            return None

        sent_module_keys = {m.get("key") for m in modules if m.get("key")}
        sent_lesson_keys = {l.get("key") for m in modules for l in m.get("lessons", []) if l.get("key")}

        for m_index, module_payload in enumerate(modules):
            module_key = module_payload.get("key")
            module = modules_by_id.get(module_payload.get("id"))
            if module is None and module_key:
                module = modules_by_key.get(module_key)
            if module is None:
                module = legacy_match(existing_modules, module_payload["title"], used_modules)
            if module is None:
                module = CourseModule(course_id=course.id, lessons=[])
                self._session.add(module)
            if module_key:
                wanted_keys.append((module, module_key))
            module.title = module_payload["title"]
            module.display_order = int(module_payload.get("display_order", m_index))
            module.status = module_payload.get("status", "published")
            await self._session.flush()
            used_modules.add(module.id)

            for l_index, lesson_payload in enumerate(module_payload.get("lessons", [])):
                lesson_key = lesson_payload.get("key")
                lesson = lessons_by_id.get(lesson_payload.get("id"))
                if lesson is None and lesson_key:
                    lesson = lessons_by_key.get(lesson_key)
                if lesson is None:
                    candidates = [l for l in existing_lessons if l.module_id == module.id]
                    lesson = legacy_match(candidates, lesson_payload["title"], used_lessons)
                new_lesson = lesson is None
                if new_lesson:
                    lesson = Lesson(module_id=module.id, course_id=course.id, practice_links=[])
                    self._session.add(lesson)
                elif lesson.module_id != module.id:
                    # Move through the relationship so the old module's
                    # collection lets go of it (and delete-orphan never fires).
                    lesson.module = module
                if lesson_key:
                    wanted_keys.append((lesson, lesson_key))
                lesson.module_id = module.id
                lesson.course_id = course.id
                lesson.title = lesson_payload["title"]
                lesson.content = lesson_payload.get("content")
                lesson.resources = lesson_payload.get("resources") or []
                lesson.estimated_minutes = int(lesson_payload.get("estimated_minutes", 8))
                lesson.status = lesson_payload.get("status", "draft")
                lesson.display_order = int(lesson_payload.get("display_order", l_index))
                await self._session.flush()
                used_lessons.add(lesson.id)
                if new_lesson:
                    new_lessons.add(lesson.id)
                await self._set_lesson_practice(
                    lesson, lesson_payload.get("practice_problem_ids") or [], lesson.id in new_lessons
                )

        changing = [(row, key) for row, key in wanted_keys if row.key != key]
        if changing:
            for row, _ in changing:
                row.key = None
            await self._session.flush()
            for row, key in changing:
                row.key = key
            await self._session.flush()

        if sent_module_keys or sent_lesson_keys:
            for module in existing_modules:
                if module.key and module.id not in used_modules and module.key not in sent_module_keys:
                    module.status = "archived"
                    for lesson in module.lessons or []:
                        if lesson.id not in used_lessons:
                            lesson.status = "archived"
            for lesson in existing_lessons:
                if lesson.key and lesson.id not in used_lessons and lesson.key not in sent_lesson_keys:
                    lesson.status = "archived"
            await self._session.flush()
        return course.id

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
