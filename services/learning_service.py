import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import BadRequestError, NotFoundError
from database.codelab_persistence import CodelabPersistence
from database.learning_persistence import LearningPersistence
from database.models import Course, Lesson, User
from schemas.learning_schemas import (
    AdminCourseLessonView,
    AdminCourseModuleView,
    AdminCourseResponse,
    AdminCourseUpsertRequest,
    BookmarkItem,
    BookmarkListResponse,
    CourseDetailResponse,
    CourseLessonRef,
    CourseListItem,
    CourseListResponse,
    CourseModuleView,
    LearningActivityItem,
    LearningContinue,
    LearningCourseProgress,
    LearningDashboardResponse,
    LearningSummary,
    LessonBookmarkResponse,
    LessonContent,
    LessonDetailResponse,
    LessonLink,
    LessonNoteResponse,
    LessonPracticeRef,
    LessonProgressUpdateRequest,
    LessonProgressUpdateResponse,
    LessonProgressView,
    QuizDetailResponse,
    QuizFeedbackItem,
    QuizQuestionView,
    QuizSubmitRequest,
    QuizSubmitResponse,
)


def _normalise_answer(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "false"):
            return low == "true"
        return value.strip()
    return value


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _lesson_link(lesson: Lesson | None) -> LessonLink | None:
    if lesson is None:
        return None
    return LessonLink(id=lesson.id, title=lesson.title, estimated_minutes=lesson.estimated_minutes)


@dataclass
class _CourseStats:
    lessons_total: int = 0
    lessons_completed: int = 0
    lessons_in_progress: int = 0
    bookmarks: int = 0
    time_spent_seconds: int = 0
    quizzes_passed: int = 0
    problems_solved: int = 0
    last_activity_at: datetime | None = None
    continue_lesson: Lesson | None = None

    @property
    def completion_percent(self) -> int:
        if not self.lessons_total:
            return 0
        return int(round(self.lessons_completed / self.lessons_total * 100))

    @property
    def started(self) -> bool:
        return self.last_activity_at is not None or self.bookmarks > 0

    def to_view(self, course: Course) -> LearningCourseProgress:
        return LearningCourseProgress(
            course_id=course.id,
            course_slug=course.slug,
            title=course.title,
            level=course.level,
            completion_percent=self.completion_percent,
            lessons_completed=self.lessons_completed,
            lessons_in_progress=self.lessons_in_progress,
            lessons_total=self.lessons_total,
            bookmarks_count=self.bookmarks,
            quizzes_passed=self.quizzes_passed,
            problems_solved=self.problems_solved,
            time_spent_minutes=self.time_spent_seconds // 60,
            last_activity_at=self.last_activity_at,
            continue_lesson=_lesson_link(self.continue_lesson),
        )


class LearningService:
    """Business logic for the study-material platform: courses, modules,
    lessons, quizzes, and per-user progress. Shares XP / streak state with
    CodeLab through CodelabPersistence so both surfaces move one counter."""

    def __init__(self, session: AsyncSession) -> None:
        self._learning = LearningPersistence(session)
        self._codelab = CodelabPersistence(session)

    # ------------------------------------------------------------------ #
    # Courses
    # ------------------------------------------------------------------ #
    async def list_courses(self, user: User | None) -> CourseListResponse:
        courses = await self._learning.list_published_courses()
        lessons_count = await self._learning.published_lessons_count([c.id for c in courses])
        progress = (
            {c.id: pct for c, pct in await self._learning.list_course_progress(user.id)}
            if user
            else {}
        )
        return CourseListResponse(
            items=[
                CourseListItem(
                    id=c.id,
                    slug=c.slug,
                    title=c.title,
                    level=c.level,
                    description=c.description,
                    lessons_count=lessons_count.get(c.id, 0),
                    completion_percent=progress.get(c.id, 0),
                )
                for c in courses
            ]
        )

    async def get_course(self, user: User | None, slug: str) -> CourseDetailResponse:
        course = await self._learning.get_course(slug)
        if course is None:
            raise NotFoundError("Course not found")

        published_lessons = [
            lesson
            for module in course.modules
            for lesson in (module.lessons or [])
            if lesson.status == "published"
        ]
        published_ids = [l.id for l in published_lessons]
        progress_map: dict = {}
        bookmarked: set[uuid.UUID] = set()
        course_progress = None
        if user is not None:
            progress_map = await self._learning.lesson_progress_map(user.id, published_ids)
            bookmarked = await self._learning.bookmarked_lesson_ids(user.id, published_ids)
            stats = await self._course_stats(user.id, [course.id])
            course_progress = stats[course.id].to_view(course)

        modules: list[CourseModuleView] = []
        for module in sorted(course.modules, key=lambda m: m.display_order):
            lessons = [l for l in (module.lessons or []) if l.status == "published"]
            lessons.sort(key=lambda l: l.display_order)
            if not lessons and module.status != "published":
                continue
            lesson_refs = [
                CourseLessonRef(
                    id=l.id,
                    title=l.title,
                    order=l.display_order,
                    status=(progress_map[l.id].status if l.id in progress_map else "not_started"),
                    estimated_minutes=l.estimated_minutes,
                    bookmarked=l.id in bookmarked,
                )
                for l in lessons
            ]
            done = sum(1 for r in lesson_refs if r.status == "completed")
            modules.append(
                CourseModuleView(
                    id=module.id,
                    title=module.title,
                    order=module.display_order,
                    completion_percent=(int(round(done / len(lesson_refs) * 100)) if lesson_refs else 0),
                    lessons=lesson_refs,
                )
            )

        return CourseDetailResponse(
            id=course.id,
            slug=course.slug,
            title=course.title,
            level=course.level,
            modules=modules,
            progress=course_progress,
        )

    async def _course_stats(
        self, user_id: uuid.UUID, course_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, _CourseStats]:
        """Live per-course (= per-language) progress for one user, computed
        from lesson rows rather than the cached user_course_progress percent
        so it stays right when lessons are added or unpublished."""
        lessons = await self._learning.ordered_published_lessons(course_ids)
        lesson_ids = [l.id for l in lessons]
        progress = await self._learning.lesson_progress_map(user_id, lesson_ids)
        bookmarked = await self._learning.bookmarked_lesson_ids(user_id, lesson_ids)
        quizzes = await self._learning.quizzes_passed_by_course(user_id, course_ids)
        problems = await self._learning.problems_solved_by_course(user_id, course_ids)

        stats = {
            cid: _CourseStats(quizzes_passed=quizzes.get(cid, 0), problems_solved=problems.get(cid, 0))
            for cid in course_ids
        }
        latest_in_progress: dict[uuid.UUID, tuple[datetime, Lesson]] = {}
        first_unfinished: dict[uuid.UUID, Lesson] = {}
        for lesson in lessons:
            s = stats[lesson.course_id]
            s.lessons_total += 1
            if lesson.id in bookmarked:
                s.bookmarks += 1
            row = progress.get(lesson.id)
            if row is not None:
                s.time_spent_seconds += row.time_spent_seconds or 0
                if s.last_activity_at is None or row.updated_at > s.last_activity_at:
                    s.last_activity_at = row.updated_at
            status = row.status if row is not None else "not_started"
            if status == "completed":
                s.lessons_completed += 1
                continue
            if status == "in_progress":
                s.lessons_in_progress += 1
                seen = latest_in_progress.get(lesson.course_id)
                if seen is None or row.updated_at > seen[0]:
                    latest_in_progress[lesson.course_id] = (row.updated_at, lesson)
            first_unfinished.setdefault(lesson.course_id, lesson)

        for cid, s in stats.items():
            if cid in latest_in_progress:
                s.continue_lesson = latest_in_progress[cid][1]
            else:
                s.continue_lesson = first_unfinished.get(cid)
        return stats

    async def _neighbours(
        self, course_id: uuid.UUID, lesson_id: uuid.UUID
    ) -> tuple[Lesson | None, Lesson | None]:
        ordered = await self._learning.ordered_published_lessons([course_id])
        ids = [l.id for l in ordered]
        if lesson_id not in ids:
            return None, None
        i = ids.index(lesson_id)
        return (ordered[i - 1] if i > 0 else None), (ordered[i + 1] if i + 1 < len(ordered) else None)

    # ------------------------------------------------------------------ #
    # Lessons
    # ------------------------------------------------------------------ #
    async def get_lesson(self, user: User | None, lesson_id: uuid.UUID) -> LessonDetailResponse:
        lesson = await self._learning.get_lesson(lesson_id)
        if lesson is None:
            raise NotFoundError("Lesson not found")

        progress_row = bookmark_row = note_row = None
        if user is not None:
            progress_row, bookmark_row, note_row = await self._learning.lesson_user_state(
                user.id, lesson.id
            )

        content = None
        if lesson.content:
            content = LessonContent(
                format=lesson.content.get("format", "html"),
                body=lesson.content.get("body", ""),
            )

        practice = [
            LessonPracticeRef(slug=link.problem.slug, title=link.problem.title)
            for link in lesson.practice_links
            if link.problem is not None and link.problem.status == "published"
        ]
        previous_lesson, next_lesson = await self._neighbours(lesson.course_id, lesson.id)

        return LessonDetailResponse(
            id=lesson.id,
            course_slug=lesson.course.slug if lesson.course else "",
            module_id=lesson.module_id,
            title=lesson.title,
            content=content,
            resources=list(lesson.resources or []),
            practice=practice,
            progress=LessonProgressView(
                status=(progress_row.status if progress_row else "not_started"),
                completed_percent=(progress_row.completed_percent if progress_row else 0),
                bookmarked=(bool(bookmark_row.bookmarked) if bookmark_row else False),
                note=(note_row.note if note_row else None),
            ),
            previous_lesson=_lesson_link(previous_lesson),
            next_lesson=_lesson_link(next_lesson),
        )

    async def update_lesson_progress(
        self, user: User, lesson_id: uuid.UUID, payload: LessonProgressUpdateRequest
    ) -> LessonProgressUpdateResponse:
        lesson = await self._learning.get_lesson(lesson_id)
        if lesson is None:
            raise NotFoundError("Lesson not found")

        existing = await self._learning.get_lesson_progress(user.id, lesson.id)
        was_completed = existing is not None and existing.status == "completed"

        if payload.status == "not_started":
            # Explicit "mark as not done" - the only way out of completed.
            status, percent = "not_started", 0
        elif payload.status == "completed" or was_completed:
            # Completion is sticky: reopening a finished lesson (which the
            # client reports as in_progress) must not undo it.
            status, percent = "completed", 100
        else:
            # Reading progress only moves forward, and never reaches 100
            # without an explicit completion.
            previous = existing.completed_percent if existing is not None else 0
            status, percent = "in_progress", min(99, max(previous, payload.completed_percent))
        newly_completed = status == "completed" and not was_completed

        row = await self._learning.upsert_lesson_progress(
            user.id,
            lesson.id,
            status,
            percent,
            payload.time_spent_seconds,
            touch_updated_at=not (was_completed and status == "completed"),
        )
        course_percent = await self._learning.recompute_course_progress(user.id, lesson.course_id)
        if newly_completed:
            await self._codelab.touch_streak(user.id, datetime.now(timezone.utc).date())
        _, next_lesson = await self._neighbours(lesson.course_id, lesson.id)

        return LessonProgressUpdateResponse(
            lesson_id=lesson.id,
            status=row.status,
            completed_percent=row.completed_percent,
            updated_at=row.updated_at,
            newly_completed=newly_completed,
            course_slug=lesson.course.slug if lesson.course else "",
            course_completion_percent=course_percent,
            next_lesson=_lesson_link(next_lesson),
        )

    async def set_bookmark(
        self, user: User, lesson_id: uuid.UUID, bookmarked: bool
    ) -> LessonBookmarkResponse:
        lesson = await self._learning.get_lesson(lesson_id)
        if lesson is None:
            raise NotFoundError("Lesson not found")
        row = await self._learning.upsert_bookmark(user.id, lesson.id, bookmarked)
        return LessonBookmarkResponse(lesson_id=lesson.id, bookmarked=bool(row.bookmarked))

    async def list_bookmarks(self, user: User, course_slug: str | None = None) -> BookmarkListResponse:
        course_id = None
        if course_slug:
            course = await self._learning.get_course(course_slug)
            if course is None:
                raise NotFoundError("Course not found")
            course_id = course.id
        rows = await self._learning.list_bookmarks(user.id, course_id)
        progress = await self._learning.lesson_progress_map(user.id, [r[0] for r in rows])
        return BookmarkListResponse(
            items=[
                BookmarkItem(
                    lesson_id=lesson_id,
                    lesson_title=lesson_title,
                    estimated_minutes=minutes,
                    course_slug=c_slug,
                    course_title=c_title,
                    module_title=module_title,
                    status=(progress[lesson_id].status if lesson_id in progress else "not_started"),
                    bookmarked_at=bookmarked_at,
                )
                for lesson_id, lesson_title, minutes, c_slug, c_title, module_title, bookmarked_at in rows
            ]
        )

    async def set_note(self, user: User, lesson_id: uuid.UUID, note: str) -> LessonNoteResponse:
        lesson = await self._learning.get_lesson(lesson_id)
        if lesson is None:
            raise NotFoundError("Lesson not found")
        row = await self._learning.upsert_note(user.id, lesson.id, note)
        return LessonNoteResponse(lesson_id=lesson.id, note=row.note, updated_at=row.updated_at)

    # ------------------------------------------------------------------ #
    # Quizzes
    # ------------------------------------------------------------------ #
    async def get_quiz(self, user: User | None, quiz_id: uuid.UUID) -> QuizDetailResponse:
        quiz = await self._learning.get_quiz(quiz_id)
        if quiz is None:
            raise NotFoundError("Quiz not found")
        already_passed = (
            await self._learning.has_passed_quiz(user.id, quiz.id) if user else False
        )
        return QuizDetailResponse(
            id=quiz.id,
            lesson_id=quiz.lesson_id,
            title=quiz.title,
            pass_percent=quiz.pass_percent,
            xp_reward=quiz.xp_reward,
            already_passed=already_passed,
            questions=[
                QuizQuestionView(
                    id=q.id,
                    prompt=q.prompt,
                    kind=q.kind,
                    options=list(q.options or []),
                    order=q.display_order,
                )
                for q in sorted(quiz.questions, key=lambda q: q.display_order)
            ],
        )

    async def submit_quiz(
        self, user: User, quiz_id: uuid.UUID, payload: QuizSubmitRequest
    ) -> QuizSubmitResponse:
        quiz = await self._learning.get_quiz(quiz_id)
        if quiz is None:
            raise NotFoundError("Quiz not found")

        answers_by_qid = {a.question_id: _normalise_answer(a.answer) for a in payload.answers}
        feedback: list[QuizFeedbackItem] = []
        correct_count = 0
        for question in quiz.questions:
            expected = _normalise_answer((question.correct_answer or {}).get("value"))
            given = answers_by_qid.get(str(question.id))
            is_correct = given is not None and given == expected
            if is_correct:
                correct_count += 1
            feedback.append(
                QuizFeedbackItem(
                    question_id=str(question.id),
                    correct=is_correct,
                    explanation=question.explanation,
                )
            )

        total = len(quiz.questions)
        percent = int(round(correct_count / total * 100)) if total else 0
        passed = percent >= quiz.pass_percent

        had_passed = await self._learning.has_passed_quiz(user.id, quiz.id)
        attempt = await self._learning.add_quiz_attempt(
            user.id, quiz.id, correct_count, total, passed, [a.model_dump() for a in payload.answers]
        )

        xp_awarded = 0
        if passed and not had_passed:
            await self._codelab.add_xp(user.id, quiz.xp_reward)
            xp_awarded = quiz.xp_reward
        if passed:
            await self._codelab.touch_streak(user.id, datetime.now(timezone.utc).date())

        return QuizSubmitResponse(
            attempt_id=attempt.id,
            quiz_id=quiz.id,
            score=correct_count,
            total=total,
            passed=passed,
            xp_awarded=xp_awarded,
            submitted_at=attempt.submitted_at,
            feedback=feedback,
        )

    # ------------------------------------------------------------------ #
    # Learning dashboard
    # ------------------------------------------------------------------ #
    async def get_dashboard(
        self, user: User, course_slug: str | None = None
    ) -> LearningDashboardResponse:
        """Overall dashboard, or - with course_slug - the same dashboard
        scoped to one course (i.e. one language). XP and streak are always
        account-wide since CodeLab and every course share them."""
        scope: Course | None = None
        if course_slug:
            scope = await self._learning.get_course(course_slug)
            if scope is None:
                raise NotFoundError("Course not found")

        courses = [scope] if scope is not None else await self._learning.list_published_courses()
        course_stats = await self._course_stats(user.id, [c.id for c in courses])
        started = [c for c in courses if course_stats[c.id].started]
        started.sort(key=lambda c: course_stats[c.id].last_activity_at or _EPOCH, reverse=True)

        if scope is not None:
            s = course_stats[scope.id]
            courses_enrolled = 1 if s.started else 0
            lessons_completed = s.lessons_completed
            quizzes_passed = s.quizzes_passed
            problems_solved = s.problems_solved
        else:
            courses_enrolled = len(started)
            lessons_completed = sum(s.lessons_completed for s in course_stats.values())
            quizzes_passed = await self._learning.count_passed_quizzes(user.id)
            _, problems_solved = await self._codelab.progress_counts(user.id)
        stats = await self._codelab.get_stats(user.id)

        # Resume in the most recently active course; a scoped dashboard with
        # no activity yet still points at that course's first lesson.
        continue_learning = None
        for course in started or ([scope] if scope is not None else []):
            lesson = course_stats[course.id].continue_lesson
            if lesson is not None:
                continue_learning = LearningContinue(
                    type="lesson", title=lesson.title, lesson_id=lesson.id, course_slug=course.slug
                )
                break

        scope_id = scope.id if scope is not None else None
        lesson_completions = await self._learning.recent_lesson_completions(user.id, 10, scope_id)
        quiz_attempts = await self._learning.recent_quiz_attempts(user.id, 10, scope_id)
        activity: list[LearningActivityItem] = [
            LearningActivityItem(
                type="lesson",
                title=title,
                status="completed",
                created_at=completed_at,
                lesson_id=lesson_id,
                course_slug=c_slug,
            )
            for lesson_id, title, c_slug, completed_at in lesson_completions
        ]
        activity += [
            LearningActivityItem(
                type="quiz",
                title=quiz_title,
                status=("passed" if attempt.passed else "failed"),
                created_at=attempt.submitted_at,
                lesson_id=lesson_id,
                course_slug=c_slug,
            )
            for attempt, quiz_title, lesson_id, c_slug in quiz_attempts
        ]
        activity.sort(key=lambda i: i.created_at, reverse=True)

        return LearningDashboardResponse(
            course_slug=scope.slug if scope is not None else None,
            summary=LearningSummary(
                courses_enrolled=courses_enrolled,
                lessons_completed=lessons_completed,
                problems_solved=problems_solved,
                quizzes_passed=quizzes_passed,
                xp=(stats.xp if stats else 0),
                current_streak=(stats.current_streak if stats else 0),
            ),
            course_progress=[
                course_stats[c.id].to_view(c) for c in (started or ([scope] if scope is not None else []))
            ],
            continue_learning=continue_learning,
            recent_activity=activity[:10],
        )

    # ------------------------------------------------------------------ #
    # Admin
    # ------------------------------------------------------------------ #
    async def upsert_course(self, payload: AdminCourseUpsertRequest) -> AdminCourseResponse:
        wanted_slugs = {
            slug
            for module in payload.modules
            for lesson in module.lessons
            for slug in lesson.practice_problem_slugs
        }
        slug_to_id = await self._learning.resolve_problem_ids_by_slug(sorted(wanted_slugs))
        missing_slugs = sorted(wanted_slugs - set(slug_to_id))
        if missing_slugs:
            raise BadRequestError(
                "Unknown practice problem slug(s): " + ", ".join(missing_slugs)
            )

        course_fields = {
            "slug": payload.slug,
            "title": payload.title,
            "level": payload.level,
            "description": payload.description,
            "status": payload.status,
            "display_order": payload.display_order,
        }
        modules = [
            {
                "id": module.id,
                "title": module.title,
                "display_order": module.display_order,
                "status": module.status,
                "lessons": [
                    {
                        "id": lesson.id,
                        "title": lesson.title,
                        "content": (lesson.content.model_dump() if lesson.content else None),
                        "resources": lesson.resources,
                        "estimated_minutes": lesson.estimated_minutes,
                        "status": lesson.status,
                        "display_order": lesson.display_order,
                        "practice_problem_ids": [
                            slug_to_id[s]
                            for s in lesson.practice_problem_slugs
                            if s in slug_to_id
                        ],
                    }
                    for lesson in module.lessons
                ],
            }
            for module in payload.modules
        ]

        course = await self._learning.upsert_course_tree(payload.slug, course_fields, modules)
        return await self._to_admin_course(course)

    async def get_course_admin(self, slug: str) -> AdminCourseResponse:
        course = await self._learning.get_course_admin(slug)
        if course is None:
            raise NotFoundError("Course not found")
        return await self._to_admin_course(course)

    async def _to_admin_course(self, course) -> AdminCourseResponse:
        lesson_ids = [
            lesson.id for module in course.modules for lesson in (module.lessons or [])
        ]
        practice = await self._learning.practice_slugs_for_lessons(lesson_ids)

        modules = [
            AdminCourseModuleView(
                id=module.id,
                title=module.title,
                display_order=module.display_order,
                status=module.status,
                lessons=[
                    AdminCourseLessonView(
                        id=lesson.id,
                        title=lesson.title,
                        display_order=lesson.display_order,
                        status=lesson.status,
                        estimated_minutes=lesson.estimated_minutes,
                        practice_problem_slugs=practice.get(lesson.id, []),
                    )
                    for lesson in sorted(module.lessons or [], key=lambda l: l.display_order)
                ],
            )
            for module in sorted(course.modules, key=lambda m: m.display_order)
        ]

        return AdminCourseResponse(
            id=course.id,
            slug=course.slug,
            title=course.title,
            level=course.level,
            description=course.description,
            status=course.status,
            display_order=course.display_order,
            modules=modules,
            created_at=course.created_at,
            updated_at=course.updated_at,
        )
