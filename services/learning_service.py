import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import BadRequestError, NotFoundError
from database.codelab_persistence import CodelabPersistence
from database.learning_persistence import LearningPersistence
from database.models import User
from schemas.learning_schemas import (
    AdminCourseLessonView,
    AdminCourseModuleView,
    AdminCourseResponse,
    AdminCourseUpsertRequest,
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
        progress_map = (
            await self._learning.lesson_progress_map(user.id, [l.id for l in published_lessons])
            if user
            else {}
        )

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
            id=course.id, slug=course.slug, title=course.title, level=course.level, modules=modules
        )

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
        )

    async def update_lesson_progress(
        self, user: User, lesson_id: uuid.UUID, payload: LessonProgressUpdateRequest
    ) -> LessonProgressUpdateResponse:
        lesson = await self._learning.get_lesson(lesson_id)
        if lesson is None:
            raise NotFoundError("Lesson not found")

        row = await self._learning.upsert_lesson_progress(
            user.id,
            lesson.id,
            payload.status,
            payload.completed_percent,
            payload.time_spent_seconds,
        )
        await self._learning.recompute_course_progress(user.id, lesson.course_id)
        if payload.status == "completed":
            await self._codelab.touch_streak(user.id, datetime.now(timezone.utc).date())

        return LessonProgressUpdateResponse(
            lesson_id=lesson.id,
            status=row.status,
            completed_percent=row.completed_percent,
            updated_at=row.updated_at,
        )

    async def set_bookmark(
        self, user: User, lesson_id: uuid.UUID, bookmarked: bool
    ) -> LessonBookmarkResponse:
        lesson = await self._learning.get_lesson(lesson_id)
        if lesson is None:
            raise NotFoundError("Lesson not found")
        row = await self._learning.upsert_bookmark(user.id, lesson.id, bookmarked)
        return LessonBookmarkResponse(lesson_id=lesson.id, bookmarked=bool(row.bookmarked))

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
    async def get_dashboard(self, user: User) -> LearningDashboardResponse:
        courses_enrolled = await self._learning.count_courses_with_progress(user.id)
        lessons_completed = await self._learning.count_completed_lessons(user.id)
        quizzes_passed = await self._learning.count_passed_quizzes(user.id)
        stats = await self._codelab.get_stats(user.id)
        _, problems_solved = await self._codelab.progress_counts(user.id)
        course_progress = await self._learning.list_course_progress(user.id)
        continue_lesson = await self._learning.pick_continue_lesson(user.id)
        lesson_completions = await self._learning.recent_lesson_completions(user.id, 5)
        quiz_attempts = await self._learning.recent_quiz_attempts(user.id, 5)

        continue_learning = None
        if continue_lesson is not None:
            continue_learning = LearningContinue(
                type="lesson",
                title=continue_lesson.title,
                lesson_id=continue_lesson.id,
                course_slug=continue_lesson.course.slug if continue_lesson.course else None,
            )

        activity: list[LearningActivityItem] = [
            LearningActivityItem(
                type="lesson", title=lesson.title, status="completed", created_at=completed_at
            )
            for lesson, completed_at in lesson_completions
        ]
        activity += [
            LearningActivityItem(
                type="quiz",
                title="Quiz attempt",
                status=("passed" if a.passed else "failed"),
                created_at=a.submitted_at,
            )
            for a in quiz_attempts
        ]
        activity.sort(key=lambda i: i.created_at, reverse=True)

        return LearningDashboardResponse(
            summary=LearningSummary(
                courses_enrolled=courses_enrolled,
                lessons_completed=lessons_completed,
                problems_solved=problems_solved,
                quizzes_passed=quizzes_passed,
                xp=(stats.xp if stats else 0),
                current_streak=(stats.current_streak if stats else 0),
            ),
            course_progress=[
                LearningCourseProgress(
                    course_slug=course.slug, title=course.title, completion_percent=pct
                )
                for course, pct in course_progress
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
