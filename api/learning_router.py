import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from core.dependencies import get_current_user, get_learning_service, get_optional_current_user, require_admin
from database.models import User
from schemas.learning_schemas import (
    AdminCourseResponse,
    AdminCourseUpsertRequest,
    BookmarkListResponse,
    CourseDetailResponse,
    CourseListResponse,
    LearningDashboardResponse,
    LessonBookmarkRequest,
    LessonBookmarkResponse,
    LessonDetailResponse,
    LessonNoteRequest,
    LessonNoteResponse,
    LessonProgressUpdateRequest,
    LessonProgressUpdateResponse,
    QuizDetailResponse,
    QuizSubmitRequest,
    QuizSubmitResponse,
)
from exams.routing import ExamContractRoute
from exams.schemas import BulkProgressRequest, BulkProgressResponse
from services.learning_service import LearningService

router = APIRouter(prefix="/api/learning", tags=["learning"])
# POST /progress/bulk follows the course-exams error contract (contract section 2).
progress_router = APIRouter(prefix="/api/learning", tags=["learning"], route_class=ExamContractRoute)
admin_router = APIRouter(
    prefix="/api/admin/learning", tags=["learning-admin"], dependencies=[Depends(require_admin)]
)


@router.get("/courses", response_model=CourseListResponse)
async def list_courses(
    current_user: User | None = Depends(get_optional_current_user),
    learning: LearningService = Depends(get_learning_service),
) -> CourseListResponse:
    return await learning.list_courses(current_user)


@router.get("/courses/{slug}", response_model=CourseDetailResponse)
async def get_course(
    slug: str,
    current_user: User | None = Depends(get_optional_current_user),
    learning: LearningService = Depends(get_learning_service),
) -> CourseDetailResponse:
    return await learning.get_course(current_user, slug)


@router.get("/lessons/{lesson_id}", response_model=LessonDetailResponse)
async def get_lesson(
    lesson_id: Annotated[str, Path(min_length=1, max_length=200)],
    course_slug: str | None = Query(
        default=None, max_length=200, description="Course the learner is in, for a lesson key shared by several courses"
    ),
    current_user: User | None = Depends(get_optional_current_user),
    learning: LearningService = Depends(get_learning_service),
) -> LessonDetailResponse:
    return await learning.get_lesson(current_user, lesson_id, course_slug)


@router.put("/lessons/{lesson_id}/progress", response_model=LessonProgressUpdateResponse)
async def update_lesson_progress(
    lesson_id: Annotated[str, Path(min_length=1, max_length=200)],
    payload: LessonProgressUpdateRequest,
    course_slug: str | None = Query(
        default=None, max_length=200, description="Course the learner is in, for a lesson key shared by several courses"
    ),
    current_user: User = Depends(get_current_user),
    learning: LearningService = Depends(get_learning_service),
) -> LessonProgressUpdateResponse:
    return await learning.update_lesson_progress(current_user, lesson_id, payload, course_slug)


@progress_router.post("/progress/bulk", response_model=BulkProgressResponse)
async def bulk_progress(
    payload: BulkProgressRequest,
    current_user: User = Depends(get_current_user),
    learning: LearningService = Depends(get_learning_service),
) -> BulkProgressResponse:
    """Uploads progress recorded on the site before server sync existed.
    Lesson ids or keys; unknown ones are reported, never an error; a
    completed lesson is never downgraded."""
    accepted, unknown = await learning.bulk_progress(
        current_user, [(item.lesson_id, item.status, item.completed_percent) for item in payload.lessons]
    )
    return BulkProgressResponse(accepted=accepted, unknown=unknown)


@router.put("/lessons/{lesson_id}/bookmark", response_model=LessonBookmarkResponse)
async def set_lesson_bookmark(
    lesson_id: Annotated[str, Path(min_length=1, max_length=200)],
    payload: LessonBookmarkRequest,
    course_slug: str | None = Query(
        default=None, max_length=200, description="Course the learner is in, for a lesson key shared by several courses"
    ),
    current_user: User = Depends(get_current_user),
    learning: LearningService = Depends(get_learning_service),
) -> LessonBookmarkResponse:
    return await learning.set_bookmark(current_user, lesson_id, payload.bookmarked, course_slug)


@router.get("/bookmarks", response_model=BookmarkListResponse)
async def list_bookmarks(
    course_slug: str | None = Query(default=None, max_length=200),
    current_user: User = Depends(get_current_user),
    learning: LearningService = Depends(get_learning_service),
) -> BookmarkListResponse:
    return await learning.list_bookmarks(current_user, course_slug)


@router.put("/lessons/{lesson_id}/note", response_model=LessonNoteResponse)
async def set_lesson_note(
    lesson_id: Annotated[str, Path(min_length=1, max_length=200)],
    payload: LessonNoteRequest,
    course_slug: str | None = Query(
        default=None, max_length=200, description="Course the learner is in, for a lesson key shared by several courses"
    ),
    current_user: User = Depends(get_current_user),
    learning: LearningService = Depends(get_learning_service),
) -> LessonNoteResponse:
    return await learning.set_note(current_user, lesson_id, payload.note, course_slug)


@router.get("/quizzes/{quiz_id}", response_model=QuizDetailResponse)
async def get_quiz(
    quiz_id: uuid.UUID,
    current_user: User | None = Depends(get_optional_current_user),
    learning: LearningService = Depends(get_learning_service),
) -> QuizDetailResponse:
    return await learning.get_quiz(current_user, quiz_id)


@router.post(
    "/quizzes/{quiz_id}/submit",
    response_model=QuizSubmitResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_quiz(
    quiz_id: uuid.UUID,
    payload: QuizSubmitRequest,
    current_user: User = Depends(get_current_user),
    learning: LearningService = Depends(get_learning_service),
) -> QuizSubmitResponse:
    return await learning.submit_quiz(current_user, quiz_id, payload)


@router.get("/dashboard", response_model=LearningDashboardResponse)
async def get_dashboard(
    course_slug: str | None = Query(default=None, max_length=200),
    current_user: User = Depends(get_current_user),
    learning: LearningService = Depends(get_learning_service),
) -> LearningDashboardResponse:
    return await learning.get_dashboard(current_user, course_slug)


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
@admin_router.put("/courses", response_model=AdminCourseResponse)
async def upsert_course(
    payload: AdminCourseUpsertRequest,
    learning: LearningService = Depends(get_learning_service),
) -> AdminCourseResponse:
    """Create or update a whole course tree (course + modules + lessons), keyed
    by course slug. Existing modules/lessons are matched by id and updated in
    place; omitted practice-problem slugs are unlinked."""
    return await learning.upsert_course(payload)


@admin_router.get("/courses/{slug}", response_model=AdminCourseResponse)
async def get_course_admin(
    slug: str,
    learning: LearningService = Depends(get_learning_service),
) -> AdminCourseResponse:
    return await learning.get_course_admin(slug)
