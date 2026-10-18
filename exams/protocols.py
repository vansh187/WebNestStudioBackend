"""Interfaces the exam services depend on. Concrete classes are built only in
exams/dependencies.py (and exams/jobs.py for background runs); tests pass
in-memory fakes."""

import random
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from exams.records import (
    AttemptRecord,
    CandidateRow,
    CertificateRecord,
    CourseRecord,
    DedupeRow,
    GenerationLogEntry,
    JobRecord,
    LessonRecord,
    ModuleContextRecord,
    ModuleRecord,
    NewQuestion,
    ProviderUsage,
    QuestionRecord,
    ServeRow,
)


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC now."""


class RandomSource(Protocol):
    def draw_random(self) -> random.Random:
        """A Random to draw one attempt with (unpredictable in production)."""


class IdGenerator(Protocol):
    def new_uuid(self) -> uuid.UUID: ...

    def choice(self, alphabet: str) -> str:
        """One cryptographically random character of `alphabet`."""


class UnitOfWork(Protocol):
    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    def savepoint(self) -> AbstractAsyncContextManager:
        """A nested transaction; an exception inside rolls back only it."""


class RateLimiter(Protocol):
    def hit(self, key: str, limit: int, window_seconds: int) -> int:
        """0 when allowed (and counted), else seconds until a slot frees up."""


class CourseReader(Protocol):
    async def get_published_course(self, slug: str) -> CourseRecord | None: ...

    async def get_course(self, slug: str) -> CourseRecord | None:
        """Any status (admin)."""

    async def get_course_by_id(self, course_id: uuid.UUID) -> CourseRecord | None: ...

    async def list_exam_modules(self, course_id: uuid.UUID) -> list[ModuleRecord]:
        """Modules of the course with their published-lesson counts, in order."""

    async def list_lessons(self, course_id: uuid.UUID) -> list[LessonRecord]: ...

    async def set_exam_settings(self, course_id: uuid.UUID, enabled: bool, exam_code: str | None) -> None: ...

    async def mark_ready(self, course_id: uuid.UUID, now: datetime) -> None: ...

    async def list_enabled_courses(self) -> list[CourseRecord]: ...


class ProgressReader(Protocol):
    async def remaining_lessons(self, user_id: uuid.UUID, course_id: uuid.UUID) -> int:
        """Published lessons of the course the user has not completed."""


class QuestionStore(Protocol):
    async def live_candidates(self, course_id: uuid.UUID) -> list[CandidateRow]: ...

    async def load_for_serving(self, course_id: uuid.UUID, question_ids: list[uuid.UUID]) -> list[ServeRow]:
        """Only live rows of this course; anything else is left out."""

    async def increment_served(self, question_ids: list[uuid.UUID]) -> None: ...

    async def status_counts(self, course_id: uuid.UUID) -> dict[uuid.UUID, dict[str, int]]:
        """module_id -> status -> count."""

    async def live_counts(self, course_id: uuid.UUID) -> dict[uuid.UUID, int]: ...

    async def list_questions(
        self, course_id: uuid.UUID, status: str | None, module_id: uuid.UUID | None, limit: int
    ) -> list[QuestionRecord]: ...

    async def get(self, question_id: uuid.UUID) -> QuestionRecord | None: ...

    async def get_for_update(self, question_id: uuid.UUID) -> QuestionRecord | None: ...

    async def insert(self, question: NewQuestion) -> uuid.UUID:
        """Raises DuplicateQuestion if the course already holds this prompt hash."""

    async def update(self, question_id: uuid.UUID, fields: dict) -> None: ...

    async def set_status(self, question_ids: list[uuid.UUID], status: str, from_statuses: tuple[str, ...]) -> int: ...

    async def dedupe_rows(self, course_id: uuid.UUID, exclude_id: uuid.UUID | None = None) -> list[DedupeRow]: ...

    async def ids_with_status(self, course_id: uuid.UUID, status: str) -> set[uuid.UUID]: ...

    async def report_counts(self, question_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]: ...

    async def stats_for(self, question_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]: ...

    async def live_by_module(self, module_id: uuid.UUID) -> list[QuestionRecord]: ...

    async def unchecked_seed_questions(self, course_id: uuid.UUID, limit: int) -> list[QuestionRecord]: ...

    async def over_served(self, course_id: uuid.UUID, max_serves: int) -> list[uuid.UUID]: ...

    async def upsert_stats(self, rows: list[dict], now: datetime) -> None: ...


class AttemptStore(Protocol):
    async def get(self, attempt_id: uuid.UUID) -> AttemptRecord | None: ...

    async def get_for_update(self, attempt_id: uuid.UUID, skip_locked: bool = False) -> AttemptRecord | None:
        """None when missing, or locked by someone else with skip_locked."""

    async def active_for(self, user_id: uuid.UUID, course_id: uuid.UUID) -> AttemptRecord | None: ...

    async def latest_graded(self, user_id: uuid.UUID, course_id: uuid.UUID) -> AttemptRecord | None: ...

    async def seen_questions(self, user_id: uuid.UUID, course_id: uuid.UUID) -> dict[uuid.UUID, datetime]:
        """Question id -> latest started_at of an attempt (any status) of this course."""

    async def insert(self, attempt: AttemptRecord) -> None:
        """Raises ActiveAttemptExists when the user already has an active attempt."""

    async def save_answers(
        self, attempt_id: uuid.UUID, user_id: uuid.UUID, answers: dict[str, int], tab_switches: int, now: datetime
    ) -> bool:
        """True if an active, unexpired attempt of this user was updated."""

    async def mark_graded(self, attempt: AttemptRecord) -> None:
        """Writes status, submitted_at, score, total, passed, module_breakdown,
        results and clears answers."""

    async def mark_lost(self, attempt_id: uuid.UUID, reason: str, now: datetime) -> bool:
        """Active -> lost (submitted_at records when it was closed); False if
        it was no longer active."""

    async def update_grade(
        self, attempt_id: uuid.UUID, score: int, total: int, passed: bool, module_breakdown: list[dict]
    ) -> None: ...

    async def overdue_ids(self, cutoff: datetime, limit: int) -> list[uuid.UUID]: ...

    async def graded_containing(self, question_id: uuid.UUID) -> list[AttemptRecord]: ...

    async def graded_results(self, course_id: uuid.UUID) -> list[tuple[int, int, list[dict]]]:
        """(score, total, results) of every graded attempt of the course."""

    async def count_lost_since(self, since: datetime) -> int:
        """Attempts marked lost since `since` (by when they were lost)."""


class AttemptCache(Protocol):
    async def get(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> list[dict] | None: ...

    async def exists(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> bool: ...

    async def put(self, attempt_id: uuid.UUID, user_id: uuid.UUID, payload: list[dict], expires_at: datetime) -> None: ...

    async def delete(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> None: ...

    async def sweep(self, now: datetime) -> int:
        """Removes entries past their own expiry; returns how many."""


class CertificateStore(Protocol):
    async def get(self, certificate_id: str) -> CertificateRecord | None: ...

    async def get_for_update(self, certificate_id: str) -> CertificateRecord | None: ...

    async def valid_for(self, user_id: uuid.UUID, course_id: uuid.UUID) -> CertificateRecord | None: ...

    async def valid_for_attempt(self, attempt_id: uuid.UUID) -> CertificateRecord | None: ...

    async def insert(self, certificate: CertificateRecord) -> None:
        """Raises CertificateIdTaken or ValidCertificateExists."""

    async def list_for_user(self, user_id: uuid.UUID) -> list[CertificateRecord]: ...

    async def search(self, query: str | None, limit: int) -> list[CertificateRecord]: ...

    async def revoke(self, certificate_id: str, reason: str, now: datetime, replaced_by: str | None) -> None: ...


class ReportStore(Protocol):
    async def add(
        self, question_id: uuid.UUID, attempt_id: uuid.UUID, user_id: uuid.UUID, reason: str, comment: str
    ) -> bool:
        """False when this attempt already reported this question."""

    async def distinct_open_reporters(self, question_id: uuid.UUID) -> int: ...

    async def resolve_for_question(self, question_id: uuid.UUID, admin_id: uuid.UUID | None, now: datetime) -> None: ...


class ContextStore(Protocol):
    async def get_many(self, course_id: uuid.UUID) -> dict[uuid.UUID, ModuleContextRecord]: ...

    async def upsert(self, module_id: uuid.UUID, course_id: uuid.UUID, context: dict, context_hash: str, now: datetime) -> None: ...


class JobStore(Protocol):
    async def create_running(self, course_id: uuid.UUID, trigger: str, now: datetime) -> JobRecord:
        """Raises JobAlreadyRunning when the course already has a running job."""

    async def get(self, job_id: uuid.UUID) -> JobRecord | None: ...

    async def running_for(self, course_id: uuid.UUID) -> JobRecord | None: ...

    async def latest_for(self, course_id: uuid.UUID) -> JobRecord | None: ...

    async def save(self, job: JobRecord) -> None: ...

    async def fail_stale(self, heartbeat_before: datetime, now: datetime) -> int: ...


class GenerationLogStore(Protocol):
    async def add(self, entry: GenerationLogEntry) -> None: ...

    async def calls_since(self, since: datetime, purposes: tuple[str, ...]) -> int: ...

    async def usage_since(self, since: datetime, hour_ago: datetime) -> list[ProviderUsage]: ...


class LlmReply(Protocol):
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    rate_limits: dict | None


class LlmClient(Protocol):
    def available_providers(self) -> list[str]: ...

    async def complete_json(
        self, provider: str, system_prompt: str, user_message: str, temperature: float, max_tokens: int
    ) -> LlmReply:
        """Raises QuotaExhausted (429 / RESOURCE_EXHAUSTED) or ProviderFailure."""


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        """Vectors in input order plus tokens used. Raises QuotaExhausted or ProviderFailure."""


class CodeRunner(Protocol):
    def supports(self, language: str) -> bool: ...

    def is_external(self, language: str) -> bool:
        """True when a run spends external credits (counted against the daily limit)."""

    async def run(self, language: str, code: str) -> "RunOutcome": ...


class RunOutcome(Protocol):
    ran: bool
    output: str
    error: str


class PdfRenderer(Protocol):
    def render(self, certificate: CertificateRecord, verify_url: str) -> bytes: ...


class AlertSender(Protocol):
    async def send(self, subject: str, body: str) -> None: ...
