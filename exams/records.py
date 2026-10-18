"""Plain data records passed between stores and services. Stores return these
instead of ORM objects, so services never trigger lazy loads or see stale
identity-map state."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class CourseRecord:
    id: uuid.UUID
    slug: str
    title: str
    status: str
    exam_code: str | None
    exam_enabled: bool
    exam_ready_at: datetime | None


@dataclass(frozen=True)
class ModuleRecord:
    id: uuid.UUID
    key: str | None
    title: str
    display_order: int
    published_lessons: int


@dataclass(frozen=True)
class LessonRecord:
    id: uuid.UUID
    key: str | None
    module_id: uuid.UUID
    course_id: uuid.UUID
    title: str
    status: str


@dataclass
class AttemptRecord:
    id: uuid.UUID
    user_id: uuid.UUID
    course_id: uuid.UUID
    recipient_name: str
    question_ids: list[uuid.UUID]
    answers: dict[str, int]
    tab_switches: int
    status: str
    started_at: datetime
    expires_at: datetime
    submitted_at: datetime | None
    score: int | None
    total: int
    passed: bool | None
    module_breakdown: list[dict] | None
    results: list[dict] | None
    lost_reason: str | None = None

    @property
    def graded(self) -> bool:
        return self.status in ("submitted", "expired")


@dataclass(frozen=True)
class CertificateRecord:
    certificate_id: str
    user_id: uuid.UUID
    course_id: uuid.UUID
    attempt_id: uuid.UUID
    recipient_name: str
    course_title: str
    course_slug: str
    score: int
    total: int
    issued_at: datetime
    status: str
    revoked_reason: str | None = None
    revoked_at: datetime | None = None
    replaced_by: str | None = None
    user_email: str | None = None


@dataclass(frozen=True)
class CandidateRow:
    """A live question as the draw sees it."""

    id: uuid.UUID
    course_id: uuid.UUID
    module_id: uuid.UUID
    lesson_id: uuid.UUID
    difficulty: str
    concept: str


@dataclass(frozen=True)
class ServeRow:
    """What a served question needs: text, options and the key (cache only)."""

    id: uuid.UUID
    course_id: uuid.UUID
    module_id: uuid.UUID
    prompt: str
    options: list[str]
    correct_index: int


@dataclass
class QuestionRecord:
    id: uuid.UUID
    course_id: uuid.UUID
    module_id: uuid.UUID
    lesson_id: uuid.UUID
    concept: str
    kind: str
    difficulty: str
    prompt: str
    options: list[str]
    correct_index: int
    explanation: str
    why_others_wrong: list[str] | None
    source_fact: str | None
    code: str | None
    code_language: str | None
    expected_output: str | None
    status: str
    source: str
    reject_reason: str | None
    prompt_norm: str
    prompt_hash: str
    embedding: list[float] | None
    checks: dict | None
    generator_model: str | None
    generation_job_id: uuid.UUID | None
    times_served: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class NewQuestion:
    """A question row to insert (live or rejected)."""

    course_id: uuid.UUID
    module_id: uuid.UUID
    lesson_id: uuid.UUID
    concept: str
    kind: str
    difficulty: str
    prompt: str
    options: list[str]
    correct_index: int
    explanation: str
    status: str
    source: str
    prompt_norm: str
    prompt_hash: str
    why_others_wrong: list[str] | None = None
    source_fact: str | None = None
    code: str | None = None
    code_language: str | None = None
    expected_output: str | None = None
    reject_reason: str | None = None
    embedding: list[float] | None = None
    checks: dict | None = None
    generator_model: str | None = None
    generation_job_id: uuid.UUID | None = None


@dataclass(frozen=True)
class DedupeRow:
    """A non-rejected pool question as the duplicate checks see it."""

    id: uuid.UUID
    lesson_id: uuid.UUID
    concept: str
    prompt_norm: str
    prompt_hash: str
    embedding: list[float] | None


@dataclass
class JobRecord:
    id: uuid.UUID
    course_id: uuid.UUID
    trigger: str
    status: str
    progress: dict = field(default_factory=dict)
    calls: int = 0
    created: int = 0
    rejected: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    paused_until: datetime | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class ModuleContextRecord:
    module_id: uuid.UUID
    course_id: uuid.UUID
    context: dict
    context_hash: str


@dataclass(frozen=True)
class GenerationLogEntry:
    purpose: str  # draft | check | embed | run
    provider: str | None = None
    model: str | None = None
    job_id: uuid.UUID | None = None
    course_id: uuid.UUID | None = None
    module_id: uuid.UUID | None = None
    requested_count: int = 0
    accepted: int = 0
    rejected: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    rate_limits: dict | None = None
    error: str | None = None


@dataclass(frozen=True)
class ProviderUsage:
    provider: str
    requests: int
    tokens: int
    last_hour_requests: int
    last_hour_tokens: int
    latest_rate_limits: dict | None
