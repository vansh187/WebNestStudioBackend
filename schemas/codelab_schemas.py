import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------- #
# Shared
# --------------------------------------------------------------------------- #
DIFFICULTIES = ("easy", "medium", "hard")
PROBLEM_STATUSES = ("draft", "published", "archived")
SUBMISSION_RESULT_STATUSES = ("passed", "failed", "runtime_error", "timeout", "manual_review")


class CodeFile(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    language: str = Field(min_length=1, max_length=40)
    content: str = Field(max_length=200_000)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value or "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("File name must be a plain file name")
        return value


class TestExample(BaseModel):
    input: str = ""
    expected_output: str = ""
    explanation: str | None = None


class PublicTest(BaseModel):
    id: str
    input: str
    expected_output: str
    weight: int = 1


# --------------------------------------------------------------------------- #
# Tracks
# --------------------------------------------------------------------------- #
class TrackItem(BaseModel):
    id: str
    label: str
    runner: str
    description: str


class TrackListResponse(BaseModel):
    items: list[TrackItem]


# --------------------------------------------------------------------------- #
# Problems
# --------------------------------------------------------------------------- #
class ProblemListItem(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    track: str
    language: str
    difficulty: str
    topics: list[str] = Field(default_factory=list)
    points: int
    estimated_minutes: int
    status: str  # per-user solve status: not_started | in_progress | solved
    solved_count: int


class ProblemListResponse(BaseModel):
    items: list[ProblemListItem]
    next_cursor: str | None = None


class UserProblemProgress(BaseModel):
    status: str
    best_score: int
    attempts: int
    last_activity_at: datetime | None = None


class ProblemDetailResponse(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    track: str
    language: str
    difficulty: str
    points: int
    estimated_minutes: int
    topics: list[str] = Field(default_factory=list)
    statement: str
    constraints: list[str] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    starter_files: list[CodeFile] = Field(default_factory=list)
    examples: list[TestExample] = Field(default_factory=list)
    public_tests: list[PublicTest] = Field(default_factory=list)
    user_progress: UserProblemProgress | None = None


# --------------------------------------------------------------------------- #
# Submissions
# --------------------------------------------------------------------------- #
class SubmissionResultInput(BaseModel):
    status: Literal["passed", "failed", "runtime_error", "timeout", "manual_review"]
    score: int = Field(default=0, ge=0, le=100)
    passed_tests: int = Field(default=0, ge=0)
    total_tests: int = Field(default=0, ge=0)
    stdout: str = Field(default="", max_length=100_000)
    stderr: str = Field(default="", max_length=100_000)
    runtime_ms: int | None = Field(default=None, ge=0)


class SubmissionCreateRequest(BaseModel):
    # Accepts either the problem UUID or its slug.
    problem_id: str = Field(min_length=1, max_length=200)
    language: str = Field(min_length=1, max_length=40)
    files: list[CodeFile] = Field(min_length=1, max_length=20)
    result: SubmissionResultInput


class SubmissionCreateResponse(BaseModel):
    id: uuid.UUID
    problem_id: uuid.UUID
    status: str  # solved | attempted | manual_review
    score: int
    passed_tests: int
    total_tests: int
    xp_awarded: int
    best_score: int
    attempts: int
    submitted_at: datetime


class SubmissionProblemRef(BaseModel):
    id: uuid.UUID
    slug: str
    title: str


class SubmissionHistoryItem(BaseModel):
    id: uuid.UUID
    problem: SubmissionProblemRef
    language: str
    status: str
    score: int
    passed_tests: int
    total_tests: int
    submitted_at: datetime


class SubmissionHistoryResponse(BaseModel):
    items: list[SubmissionHistoryItem]
    next_cursor: str | None = None


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
class DashboardSummary(BaseModel):
    problems_total: int
    attempted: int
    solved: int
    completion_percent: int
    xp: int
    current_streak: int
    best_streak: int


class TrackProgress(BaseModel):
    track: str
    label: str
    solved: int
    total: int
    completion_percent: int


class DifficultyProgress(BaseModel):
    difficulty: str
    solved: int
    total: int


class ContinueLearning(BaseModel):
    type: str = "problem"
    slug: str
    title: str
    track: str
    difficulty: str


class RecentActivityItem(BaseModel):
    type: str = "submission"
    title: str
    track: str
    status: str
    score: int
    xp: int
    created_at: datetime


class AchievementItem(BaseModel):
    id: str
    label: str
    earned_at: datetime


class CodelabDashboardResponse(BaseModel):
    summary: DashboardSummary
    track_progress: list[TrackProgress]
    difficulty_progress: list[DifficultyProgress]
    continue_learning: ContinueLearning | None = None
    recent_activity: list[RecentActivityItem]
    achievements: list[AchievementItem]


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
class AdminTestCaseInput(BaseModel):
    input: str = Field(default="", max_length=100_000)
    expected_output: str = Field(default="", max_length=100_000)
    is_hidden: bool = False
    weight: int = Field(default=1, ge=1, le=100)


class AdminProblemUpsertRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=200)
    track: str = Field(min_length=1, max_length=60)
    language: str = Field(min_length=1, max_length=40)
    difficulty: Literal["easy", "medium", "hard"] = "easy"
    points: int = Field(default=20, ge=0, le=1000)
    estimated_minutes: int = Field(default=10, ge=1, le=600)
    status: Literal["draft", "published", "archived"] = "draft"
    topics: list[str] = Field(default_factory=list, max_length=30)
    statement: str = Field(default="", max_length=20_000)
    constraints: list[str] = Field(default_factory=list, max_length=50)
    hints: list[str] = Field(default_factory=list, max_length=50)
    starter_files: list[CodeFile] = Field(default_factory=list, max_length=20)
    examples: list[TestExample] = Field(default_factory=list, max_length=20)
    test_cases: list[AdminTestCaseInput] = Field(default_factory=list, max_length=200)

    @field_validator("slug")
    @classmethod
    def _clean_slug(cls, value: str) -> str:
        value = value.strip().lower()
        if not value or " " in value:
            raise ValueError("slug must be a non-empty lowercase token without spaces")
        return value


class AdminProblemResponse(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    track: str
    language: str
    difficulty: str
    points: int
    estimated_minutes: int
    status: str
    topics: list[str] = Field(default_factory=list)
    statement: str
    constraints: list[str] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    starter_files: list[CodeFile] = Field(default_factory=list)
    examples: list[TestExample] = Field(default_factory=list)
    public_test_count: int
    hidden_test_count: int
    created_at: datetime
    updated_at: datetime
