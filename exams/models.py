"""ORM tables for the course exams (plan section 3). Only the infra/sql_*
stores import this module."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database.session import Base

QUESTION_STATUSES = ("live", "rejected", "quarantined", "voided", "retired")
ATTEMPT_STATUSES = ("active", "submitted", "expired", "lost")


class ExamQuestion(Base):
    """The per-course pool. The composite foreign keys make a question whose
    module or lesson belongs to another course impossible to store."""

    __tablename__ = "exam_questions"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(options) = 'array' and jsonb_array_length(options) = 4", name="ck_exam_questions_options"),
        CheckConstraint("correct_index between 0 and 3", name="ck_exam_questions_correct_index"),
        CheckConstraint(
            "status in ('live','rejected','quarantined','voided','retired')", name="ck_exam_questions_status"
        ),
        CheckConstraint("difficulty in ('easy','medium','hard')", name="ck_exam_questions_difficulty"),
        CheckConstraint(
            "kind in ('concept','scenario','code_output','spot_the_bug')", name="ck_exam_questions_kind"
        ),
        CheckConstraint("source in ('ai','manual','seed')", name="ck_exam_questions_source"),
        ForeignKeyConstraint(
            ["course_id", "module_id"],
            ["course_modules.course_id", "course_modules.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
            name="fk_exam_questions_course_module",
        ),
        ForeignKeyConstraint(
            ["module_id", "lesson_id"],
            ["lessons.module_id", "lessons.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
            name="fk_exam_questions_module_lesson",
        ),
        Index("ix_exam_questions_course_status_module", "course_id", "status", "module_id"),
        Index("ix_exam_questions_course_lesson_concept", "course_id", "lesson_id", "concept"),
        # Exact duplicates are refused by the database itself, even from two
        # racing jobs. Rejected drafts are kept for auditing and never block.
        Index(
            "uq_exam_questions_course_hash",
            "course_id",
            "prompt_hash",
            unique=True,
            postgresql_where=text("status <> 'rejected'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    module_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lesson_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    concept: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="concept")
    difficulty: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[list] = mapped_column(JSONB, nullable=False)
    correct_index: Mapped[int] = mapped_column(Integer, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    why_others_wrong: Mapped[list | None] = mapped_column(JSONB)
    source_fact: Mapped[str | None] = mapped_column(Text)
    code: Mapped[str | None] = mapped_column(Text)
    code_language: Mapped[str | None] = mapped_column(Text)
    expected_output: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="ai")
    reject_reason: Mapped[str | None] = mapped_column(Text)
    prompt_norm: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # real[] with in-process cosine (a course pool is a few hundred rows), so
    # no pgvector extension is required.
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(Float))
    checks: Mapped[dict | None] = mapped_column(JSONB)
    generator_model: Mapped[str | None] = mapped_column(Text)
    generation_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_generation_jobs.id", ondelete="SET NULL")
    )
    times_served: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamModuleContext(Base):
    """Lesson material per module, uploaded by the site's catalog sync script
    and used to draft and ground questions."""

    __tablename__ = "exam_module_context"

    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("course_modules.id", ondelete="CASCADE"), primary_key=True
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    context: Mapped[dict] = mapped_column(JSONB, nullable=False)
    context_hash: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamGenerationJob(Base):
    """One refill run for one course. The partial unique index allows only one
    unfinished job per course at a time."""

    __tablename__ = "exam_generation_jobs"
    __table_args__ = (
        CheckConstraint(
            "status in ('running','done','failed','paused')", name="ck_exam_generation_jobs_status"
        ),
        Index(
            "uq_exam_generation_jobs_course_running",
            "course_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        Index("ix_exam_generation_jobs_course_created", "course_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    progress: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rejected: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    errors: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamGenerationLog(Base):
    """One row per model call (draft, check, embed) or external code run.
    Holds token usage and provider rate-limit headers - never prompts or answers."""

    __tablename__ = "exam_generation_logs"
    __table_args__ = (Index("ix_exam_generation_logs_created", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_generation_jobs.id", ondelete="SET NULL"), index=True
    )
    course_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("courses.id", ondelete="SET NULL"))
    module_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    purpose: Mapped[str] = mapped_column(Text, nullable=False)  # draft | check | embed | run
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    accepted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rate_limits: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamAttempt(Base):
    """The permanent, compact record of one attempt. Answers live here (not in
    the cache) so a lost cache can never lose a learner's answers."""

    __tablename__ = "exam_attempts"
    __table_args__ = (
        CheckConstraint("status in ('active','submitted','expired','lost')", name="ck_exam_attempts_status"),
        Index(
            "uq_exam_attempts_user_course_active",
            "user_id",
            "course_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_exam_attempts_status_expires", "status", "expires_at"),
        Index("ix_exam_attempts_user_course_submitted", "user_id", "course_id", "submitted_at"),
        Index("ix_exam_attempts_course_status", "course_id", "status"),
        Index("ix_exam_attempts_question_ids", "question_ids", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    recipient_name: Mapped[str] = mapped_column(Text, nullable=False)
    question_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    answers: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    tab_switches: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    score: Mapped[int | None] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    module_breakdown: Mapped[list | None] = mapped_column(JSONB)
    results: Mapped[list | None] = mapped_column(JSONB)
    lost_reason: Mapped[str | None] = mapped_column(Text)


class ExamAttemptCache(Base):
    """The served copy of an active attempt plus its answer key. Deleted in the
    same transaction that grades the attempt."""

    __tablename__ = "exam_attempt_cache"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_attempts.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload: Mapped[list] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class Certificate(Base):
    __tablename__ = "certificates"
    __table_args__ = (
        CheckConstraint("status in ('valid','revoked')", name="ck_certificates_status"),
        Index(
            "uq_certificates_user_course_valid",
            "user_id",
            "course_id",
            unique=True,
            postgresql_where=text("status = 'valid'"),
        ),
        # Partial so a reissue can reuse the attempt of the revoked original.
        Index(
            "uq_certificates_attempt_valid",
            "attempt_id",
            unique=True,
            postgresql_where=text("status = 'valid'"),
        ),
        Index("ix_certificates_user_issued", "user_id", "issued_at"),
    )

    certificate_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_attempts.id", ondelete="CASCADE"), nullable=False
    )
    recipient_name: Mapped[str] = mapped_column(Text, nullable=False)
    course_title: Mapped[str] = mapped_column(Text, nullable=False)
    course_slug: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="valid")
    revoked_reason: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[str | None] = mapped_column(Text)


class ExamQuestionReport(Base):
    __tablename__ = "exam_question_reports"
    __table_args__ = (
        UniqueConstraint("attempt_id", "question_id", name="uq_exam_question_reports_attempt_question"),
        CheckConstraint(
            "reason in ('wrong_answer','unclear','typo','other')", name="ck_exam_question_reports_reason"
        ),
        Index("ix_exam_question_reports_question", "question_id", "resolved_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_questions.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_attempts.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class ExamQuestionStats(Base):
    __tablename__ = "exam_question_stats"

    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_questions.id", ondelete="CASCADE"), primary_key=True
    )
    answered: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    correct: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    discrimination: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
