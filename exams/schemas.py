"""Request/response models, exactly the contract (COURSE_EXAMS_CERTIFICATES.md).

Learner and public models are separate classes with no correct_index,
explanation or answer-key fields; tests/test_exams_api.py serializes every one
of them and fails if such a field appears. Only Admin* models carry answers."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from exams.domain.certificate_id import EXAM_CODE_PATTERN, EXAM_CODE_RULE


class _Strict(BaseModel):
    # Unknown fields are ignored, never rejected: the frontend is already built.
    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------- #
# Learner
# --------------------------------------------------------------------------- #
class ExamViewResponse(BaseModel):
    enabled: bool
    eligible: bool
    reasons: list[str]
    lessons_remaining: int
    cooldown_until: datetime | None
    active_attempt_id: uuid.UUID | None
    passed: bool
    certificate_id: str | None
    question_count: int
    duration_minutes: int
    pass_percent: int


class StartAttemptRequest(_Strict):
    recipient_name: str = Field(max_length=200)


class QuestionView(BaseModel):
    id: uuid.UUID
    prompt: str
    options: list[str]
    module_title: str


class AnswerView(BaseModel):
    question_id: uuid.UUID
    option_index: int


class ActiveAttemptResponse(BaseModel):
    attempt_id: uuid.UUID
    status: str
    started_at: datetime
    expires_at: datetime
    server_now: datetime
    questions: list[QuestionView]
    answers: list[AnswerView]


class ModuleScore(BaseModel):
    module_title: str
    correct: int
    total: int


class ResultResponse(BaseModel):
    score: int
    total: int
    percent: int
    passed: bool
    module_breakdown: list[ModuleScore]
    certificate_id: str | None
    retake_after: datetime | None


class GradedAttemptResponse(BaseModel):
    attempt_id: uuid.UUID
    status: str
    result: ResultResponse


class AnswerInput(_Strict):
    question_id: uuid.UUID
    option_index: int = Field(ge=0, le=3)


class SaveAnswersRequest(_Strict):
    answers: list[AnswerInput] = Field(default_factory=list, max_length=100)
    tab_switches: int = Field(default=0, ge=0, le=100_000)


class SaveAnswersResponse(BaseModel):
    saved_at: datetime


class PingResponse(BaseModel):
    status: str
    expires_at: datetime
    server_now: datetime


class ReportRequest(_Strict):
    question_id: uuid.UUID
    reason: Literal["wrong_answer", "unclear", "typo", "other"]
    comment: str = Field(default="", max_length=500)


class ReportResponse(BaseModel):
    status: str = "received"


class MyCertificate(BaseModel):
    certificate_id: str
    course_slug: str
    course_title: str
    issued_at: datetime
    status: str


class MyCertificatesResponse(BaseModel):
    items: list[MyCertificate]


# --------------------------------------------------------------------------- #
# Public
# --------------------------------------------------------------------------- #
class PublicCertificateResponse(BaseModel):
    certificate_id: str
    recipient_name: str
    course_title: str
    course_slug: str
    issued_at: datetime
    status: str


# --------------------------------------------------------------------------- #
# Progress (phase 1)
# --------------------------------------------------------------------------- #
class BulkProgressItem(_Strict):
    lesson_id: str = Field(min_length=1, max_length=200)
    status: Literal["not_started", "in_progress", "completed"] = "completed"
    completed_percent: int = Field(default=100, ge=0, le=100)


class BulkProgressRequest(_Strict):
    lessons: list[BulkProgressItem] = Field(max_length=500)


class BulkProgressResponse(BaseModel):
    accepted: int
    unknown: list[str]


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
class AdminExamSettingsRequest(_Strict):
    enabled: bool
    code: str | None = Field(default=None, max_length=12)

    @field_validator("code")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if not EXAM_CODE_PATTERN.fullmatch(value):
            raise ValueError(EXAM_CODE_RULE)
        return value


class AdminExamSettingsResponse(BaseModel):
    slug: str
    enabled: bool
    code: str | None
    ready: bool
    job_id: uuid.UUID | None


class ContextLesson(BaseModel):
    lesson_id: str = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=300)
    summary: str = Field(default="", max_length=10_000)
    key_points: list[str] = Field(default_factory=list, max_length=60)
    avoid_prompts: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("key_points", "avoid_prompts")
    @classmethod
    def _short_items(cls, value: list[str]) -> list[str]:
        if any(len(item) > 2000 for item in value):
            raise ValueError("each item must be at most 2000 characters")
        return value


class ContextModule(BaseModel):
    course_slug: str = Field(default="", max_length=200)
    course_title: str = Field(default="", max_length=300)
    module_id: str = Field(min_length=1, max_length=200)
    module_title: str = Field(default="", max_length=300)
    lessons: list[ContextLesson] = Field(default_factory=list, max_length=300)


class ExamContextRequest(BaseModel):
    modules: list[ContextModule] = Field(max_length=200)


class ExamContextResponse(BaseModel):
    updated: int
    unchanged: int
    quarantined: int


class AdminModuleSummary(BaseModel):
    module_id: str
    title: str
    live: int
    target: int
    quarantined: int
    rejected: int


class AdminPoolSummary(BaseModel):
    enabled: bool
    ready: bool
    live: int
    target: int
    code: str | None
    modules: list[AdminModuleSummary]


class AdminQuestionItem(BaseModel):
    id: uuid.UUID
    module_id: str
    lesson_id: str
    concept: str
    kind: str
    prompt: str
    options: list[str]
    correct_index: int
    explanation: str
    difficulty: str
    status: str
    source: str
    reject_reason: str | None
    checks: dict
    reports: int
    stats: dict | None
    times_served: int
    updated_at: datetime | None


class AdminQuestionList(BaseModel):
    items: list[AdminQuestionItem]


class AdminQuestionCreate(BaseModel):
    course_slug: str = Field(min_length=1, max_length=200)
    module_id: str | None = Field(default=None, max_length=200)
    lesson_id: str = Field(min_length=1, max_length=200)
    concept: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=1500)
    options: list[str] = Field(min_length=4, max_length=4)
    correct_index: int = Field(ge=0, le=3)
    explanation: str = Field(min_length=1, max_length=2000)
    difficulty: Literal["easy", "medium", "hard"]
    kind: Literal["concept", "scenario", "code_output", "spot_the_bug"] = "concept"
    source_fact: str | None = Field(default=None, max_length=1000)
    why_others_wrong: list[str] | None = Field(default=None, max_length=3)
    code: str | None = Field(default=None, max_length=4000)
    expected_output: str | None = Field(default=None, max_length=2000)


class AdminQuestionUpdate(BaseModel):
    status: Literal["live"] | None = None
    lesson_id: str | None = Field(default=None, max_length=200)
    concept: str | None = Field(default=None, max_length=120)
    prompt: str | None = Field(default=None, max_length=1500)
    options: list[str] | None = Field(default=None, min_length=4, max_length=4)
    correct_index: int | None = Field(default=None, ge=0, le=3)
    explanation: str | None = Field(default=None, max_length=2000)
    difficulty: Literal["easy", "medium", "hard"] | None = None
    kind: Literal["concept", "scenario", "code_output", "spot_the_bug"] | None = None
    source_fact: str | None = Field(default=None, max_length=1000)
    why_others_wrong: list[str] | None = Field(default=None, max_length=3)
    code: str | None = Field(default=None, max_length=4000)
    expected_output: str | None = Field(default=None, max_length=2000)


class ImportRejection(BaseModel):
    line: int
    reason: str


class ImportResponse(BaseModel):
    imported: int
    rejected: list[ImportRejection]


class VoidResponse(BaseModel):
    id: uuid.UUID
    status: str
    regraded_attempts: int
    new_certificates: int


class RefillResponse(BaseModel):
    job_id: uuid.UUID | None
    status: str


class JobResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    trigger: str
    modules_done: int
    modules_total: int
    calls: int
    created: int
    rejected_by_check: dict
    errors: list[str]
    paused_until: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class QuotaResponse(BaseModel):
    models: list[dict]


class AdminCertificate(BaseModel):
    certificate_id: str
    recipient_name: str
    user_email: str | None
    course_title: str
    score: int
    total: int
    issued_at: datetime
    status: str
    revoked_reason: str | None
    replaced_by: str | None = None


class AdminCertificateList(BaseModel):
    items: list[AdminCertificate]


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ReissueRequest(BaseModel):
    recipient_name: str = Field(min_length=1, max_length=200)
