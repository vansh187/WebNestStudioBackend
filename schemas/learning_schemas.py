import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from exams.domain.certificate_id import EXAM_CODE_PATTERN, EXAM_CODE_RULE

# --------------------------------------------------------------------------- #
# Courses
# --------------------------------------------------------------------------- #
class CourseListItem(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    level: str
    description: str
    lessons_count: int
    completion_percent: int


class CourseListResponse(BaseModel):
    items: list[CourseListItem]


class CourseLessonRef(BaseModel):
    id: uuid.UUID
    key: str | None = None  # the site's lesson key, so clients can match their own ids
    title: str
    order: int
    status: str  # per-user: not_started | in_progress | completed
    estimated_minutes: int
    bookmarked: bool = False


class CourseModuleView(BaseModel):
    id: uuid.UUID
    title: str
    order: int
    completion_percent: int
    lessons: list[CourseLessonRef]


class LessonLink(BaseModel):
    id: uuid.UUID
    title: str
    estimated_minutes: int


class LearningCourseProgress(BaseModel):
    """Per-user progress for one course. Each course covers a single
    language, so this is also the learner's progress in that language."""

    course_id: uuid.UUID
    course_slug: str
    title: str
    level: str
    completion_percent: int
    lessons_completed: int
    lessons_in_progress: int
    lessons_total: int
    bookmarks_count: int
    quizzes_passed: int
    problems_solved: int  # CodeLab problems linked to this course's lessons
    time_spent_minutes: int
    last_activity_at: datetime | None = None
    continue_lesson: LessonLink | None = None  # latest in-progress, else first unfinished


class CourseDetailResponse(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    level: str
    modules: list[CourseModuleView]
    progress: LearningCourseProgress | None = None  # authenticated users only


# --------------------------------------------------------------------------- #
# Lessons
# --------------------------------------------------------------------------- #
class LessonContent(BaseModel):
    format: str = "html"
    body: str = ""


class LessonPracticeRef(BaseModel):
    type: str = "problem"
    slug: str
    title: str


class LessonProgressView(BaseModel):
    status: str
    completed_percent: int
    bookmarked: bool
    note: str | None = None


class LessonDetailResponse(BaseModel):
    id: uuid.UUID
    key: str | None = None
    course_slug: str
    module_id: uuid.UUID
    title: str
    content: LessonContent | None = None
    resources: list[dict] = Field(default_factory=list)
    practice: list[LessonPracticeRef] = Field(default_factory=list)
    progress: LessonProgressView
    previous_lesson: LessonLink | None = None
    next_lesson: LessonLink | None = None


class LessonProgressUpdateRequest(BaseModel):
    """Completion is sticky: later in_progress updates (e.g. revisiting the
    lesson) keep it completed and only add time. Send not_started to
    explicitly un-mark a lesson."""

    status: Literal["not_started", "in_progress", "completed"]
    completed_percent: int = Field(ge=0, le=100)
    time_spent_seconds: int = Field(default=0, ge=0, le=86_400)


class LessonProgressUpdateResponse(BaseModel):
    lesson_id: uuid.UUID
    status: str
    completed_percent: int
    updated_at: datetime
    newly_completed: bool = False  # true only on the transition into completed
    course_slug: str = ""
    course_completion_percent: int = 0
    next_lesson: LessonLink | None = None


class MyLessonProgressItem(BaseModel):
    """One lesson the user has started or completed (LESSON_PROGRESS_SYNC_BACKEND.md section 2)."""

    key: str | None  # null for a lesson created before keys existed; clients skip it
    lesson_id: uuid.UUID
    course_slug: str
    # not_started appears only in `since` responses: an explicit un-mark.
    status: Literal["not_started", "in_progress", "completed"]
    completed_percent: int = Field(ge=0, le=100)
    updated_at: datetime


class MyProgressResponse(BaseModel):
    lessons: list[MyLessonProgressItem]
    server_time: datetime  # send back as ?since= to fetch only later changes
    # True when the row cap cut the list (newest kept). Don't use server_time
    # from such a response as a sync point; fetch in full again next time.
    truncated: bool = False


class LessonBookmarkRequest(BaseModel):
    bookmarked: bool


class LessonBookmarkResponse(BaseModel):
    lesson_id: uuid.UUID
    bookmarked: bool


class BookmarkItem(BaseModel):
    lesson_id: uuid.UUID
    lesson_title: str
    estimated_minutes: int
    course_slug: str
    course_title: str
    module_title: str
    status: str  # per-user lesson status
    bookmarked_at: datetime


class BookmarkListResponse(BaseModel):
    items: list[BookmarkItem]


class LessonNoteRequest(BaseModel):
    note: str = Field(max_length=10_000)


class LessonNoteResponse(BaseModel):
    lesson_id: uuid.UUID
    note: str
    updated_at: datetime


# --------------------------------------------------------------------------- #
# Quizzes
# --------------------------------------------------------------------------- #
class QuizQuestionView(BaseModel):
    id: uuid.UUID
    prompt: str
    kind: str  # single | boolean
    options: list[dict] = Field(default_factory=list)  # [{id, text}] for single-choice; empty for boolean
    order: int


class QuizDetailResponse(BaseModel):
    id: uuid.UUID
    lesson_id: uuid.UUID | None = None
    title: str
    pass_percent: int
    xp_reward: int
    already_passed: bool
    questions: list[QuizQuestionView]


class QuizAnswerInput(BaseModel):
    question_id: str = Field(min_length=1, max_length=200)
    answer: str | bool | int


class QuizSubmitRequest(BaseModel):
    answers: list[QuizAnswerInput] = Field(min_length=1, max_length=200)


class QuizFeedbackItem(BaseModel):
    question_id: str
    correct: bool
    explanation: str = ""


class QuizSubmitResponse(BaseModel):
    attempt_id: uuid.UUID
    quiz_id: uuid.UUID
    score: int
    total: int
    passed: bool
    xp_awarded: int
    submitted_at: datetime
    feedback: list[QuizFeedbackItem]


# --------------------------------------------------------------------------- #
# Learning dashboard
# --------------------------------------------------------------------------- #
class LearningSummary(BaseModel):
    courses_enrolled: int
    lessons_completed: int
    problems_solved: int
    quizzes_passed: int
    xp: int
    current_streak: int


class LearningContinue(BaseModel):
    type: str
    title: str
    lesson_id: uuid.UUID | None = None
    course_slug: str | None = None
    slug: str | None = None


class LearningActivityItem(BaseModel):
    type: str
    title: str
    status: str | None = None
    created_at: datetime
    lesson_id: uuid.UUID | None = None
    course_slug: str | None = None


class LearningDashboardResponse(BaseModel):
    course_slug: str | None = None  # set when the dashboard is scoped to one course
    summary: LearningSummary
    course_progress: list[LearningCourseProgress]
    continue_learning: LearningContinue | None = None
    recent_activity: list[LearningActivityItem]


# --------------------------------------------------------------------------- #
# Admin - courses / modules / lessons
# --------------------------------------------------------------------------- #
_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$"


class AdminLessonInput(BaseModel):
    id: uuid.UUID | None = None
    # The site's stable lesson key; upserts match on it (COURSE_EXAMS_CERTIFICATES.md section 1).
    key: str | None = Field(default=None, max_length=200, pattern=_KEY_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    content: LessonContent | None = None
    resources: list[dict] = Field(default_factory=list, max_length=50)
    estimated_minutes: int = Field(default=8, ge=1, le=600)
    status: Literal["draft", "published", "archived"] = "draft"
    display_order: int = Field(default=0, ge=0)
    practice_problem_slugs: list[str] = Field(default_factory=list, max_length=20)


class AdminModuleInput(BaseModel):
    id: uuid.UUID | None = None
    key: str | None = Field(default=None, max_length=200, pattern=_KEY_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    display_order: int = Field(default=0, ge=0)
    status: Literal["draft", "published"] = "published"
    lessons: list[AdminLessonInput] = Field(default_factory=list, max_length=200)


class AdminCourseUpsertRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=200)
    level: str = Field(default="beginner", max_length=40)
    description: str = Field(default="", max_length=5_000)
    status: Literal["draft", "published", "archived"] = "draft"
    display_order: int = Field(default=0, ge=0)
    modules: list[AdminModuleInput] = Field(default_factory=list, max_length=100)
    # Short code for certificate IDs (JAVA, PY, ...). Omitted = keep the current one.
    exam_code: str | None = Field(default=None, max_length=12)

    @field_validator("exam_code")
    @classmethod
    def _clean_exam_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if not EXAM_CODE_PATTERN.fullmatch(value):
            raise ValueError(EXAM_CODE_RULE)
        return value

    @model_validator(mode="after")
    def _unique_keys(self):
        module_keys = [m.key for m in self.modules if m.key]
        lesson_keys = [l.key for m in self.modules for l in m.lessons if l.key]
        if len(module_keys) != len(set(module_keys)):
            raise ValueError("module keys must be unique within a course")
        if len(lesson_keys) != len(set(lesson_keys)):
            raise ValueError("lesson keys must be unique within a course")
        return self

    @field_validator("slug")
    @classmethod
    def _clean_slug(cls, value: str) -> str:
        value = value.strip().lower()
        if not value or " " in value:
            raise ValueError("slug must be a non-empty lowercase token without spaces")
        return value


class AdminCourseModuleView(BaseModel):
    id: uuid.UUID
    key: str | None = None
    title: str
    display_order: int
    status: str
    lessons: list["AdminCourseLessonView"]


class AdminCourseLessonView(BaseModel):
    id: uuid.UUID
    key: str | None = None
    title: str
    display_order: int
    status: str
    estimated_minutes: int
    practice_problem_slugs: list[str] = Field(default_factory=list)


class AdminCourseResponse(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    level: str
    description: str
    status: str
    display_order: int
    exam_code: str | None = None
    exam_enabled: bool = False
    modules: list[AdminCourseModuleView]
    created_at: datetime
    updated_at: datetime


AdminCourseModuleView.model_rebuild()
