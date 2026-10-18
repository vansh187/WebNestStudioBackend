"""Admin pool management (contract section 7, plan 6.7-6.8)."""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from exams.domain.certificate_id import EXAM_CODE_PATTERN, EXAM_CODE_RULE
from exams.domain.grading import regrade
from exams.domain.validation import context_passages, is_grounded
from exams.errors import (
    CourseNotFound,
    DuplicateQuestion,
    ExamConflict,
    ExamValidationError,
    JobNotFound,
    QuestionNotFound,
)
from exams.protocols import (
    AttemptStore,
    Clock,
    ContextStore,
    CourseReader,
    GenerationLogStore,
    JobStore,
    QuestionStore,
    ReportStore,
    UnitOfWork,
)
from exams.records import CourseRecord, JobRecord, QuestionRecord
from exams.services.certificate_service import CertificateService
from exams.services.common import audit, guarded
from exams.services.generation_prompts import parse_draft_item
from exams.services.generation_service import store_candidates
from exams.services.pool_service import PoolService
from exams.services.verification import Candidate, VerificationPipeline

logger = logging.getLogger("webnest.exams.admin")

MAX_IMPORT = 500
VERIFY_CHUNK = 5
REGRADE_COMMIT_EVERY = 100
CONTENT_FIELDS = (
    "prompt", "options", "correct_index", "explanation", "difficulty", "kind", "concept",
    "lesson_id", "source_fact", "why_others_wrong", "code", "expected_output",
)  # fmt: skip


@dataclass
class ImportResult:
    imported: int = 0
    rejected: list[dict] = field(default_factory=list)


def context_hash(context: dict) -> str:
    return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class QuestionAdminService:
    def __init__(
        self,
        courses: CourseReader,
        questions: QuestionStore,
        attempts: AttemptStore,
        reports: ReportStore,
        contexts: ContextStore,
        jobs: JobStore,
        logs: GenerationLogStore,
        pool: PoolService,
        pipeline: VerificationPipeline,
        certificates: CertificateService,
        uow: UnitOfWork,
        clock: Clock,
        settings,
    ) -> None:
        self._courses = courses
        self._questions = questions
        self._attempts = attempts
        self._reports = reports
        self._contexts = contexts
        self._jobs = jobs
        self._logs = logs
        self._pool = pool
        self._pipeline = pipeline
        self._certificates = certificates
        self._uow = uow
        self._clock = clock
        self._settings = settings

    async def _course(self, slug: str) -> CourseRecord:
        course = await self._courses.get_course(slug)
        if course is None:
            raise CourseNotFound()
        return course

    @guarded("find course")
    async def course_for(self, slug: str) -> CourseRecord:
        return await self._course(slug)

    async def _module_ref_map(self, course_id: uuid.UUID) -> tuple[dict[str, uuid.UUID], dict[uuid.UUID, str]]:
        """module key or uuid -> id, and id -> the label the contract shows
        (the module key, or the uuid for legacy modules)."""
        refs: dict[str, uuid.UUID] = {}
        labels: dict[uuid.UUID, str] = {}
        for module in await self._courses.list_exam_modules(course_id):
            refs[str(module.id)] = module.id
            if module.key:
                refs[module.key] = module.id
            labels[module.id] = module.key or str(module.id)
        return refs, labels

    # ------------------------------------------------------------------ #
    # Exam switch and context
    # ------------------------------------------------------------------ #
    @guarded("set exam settings")
    async def set_exam(self, slug: str, enabled: bool, code: str | None, admin_id: uuid.UUID) -> tuple[CourseRecord, bool]:
        """(course, needs_fill). Turning the exam on starts the fill job when
        the pool is not full yet; turning it off keeps the pool."""
        course = await self._course(slug)
        new_code = code.strip().upper() if code else course.exam_code
        if new_code is not None and not EXAM_CODE_PATTERN.fullmatch(new_code):
            raise ExamValidationError(EXAM_CODE_RULE)
        if enabled and not new_code:
            raise ExamValidationError("code is required to enable the exam")
        try:
            async with self._uow.savepoint():
                await self._courses.set_exam_settings(course.id, enabled, new_code)
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise
        audit("exam_enable" if enabled else "exam_disable", admin_id, course=course.slug, code=new_code)
        updated = await self._courses.get_course_by_id(course.id) or course
        needs_fill = enabled and not (await self._pool.state(course.id)).full
        return updated, needs_fill

    @guarded("upload exam context")
    async def upload_context(self, slug: str, modules: list[dict], admin_id: uuid.UUID | None) -> dict:
        course = await self._course(slug)
        refs, _ = await self._module_ref_map(course.id)
        unknown = [str(m.get("module_id")) for m in modules if str(m.get("module_id") or "") not in refs]
        if unknown:
            raise ExamValidationError(
                "Unknown module_id: " + ", ".join(unknown[:20]),
                errors=[{"msg": f"Unknown module_id: {ref}"} for ref in unknown[:50]],
            )
        existing = await self._contexts.get_many(course.id)
        now = self._clock.now()
        updated = unchanged = quarantined = 0
        try:
            for module in modules:
                module_id = refs[str(module["module_id"])]
                digest = context_hash(module)
                previous = existing.get(module_id)
                if previous is not None and previous.context_hash == digest:
                    unchanged += 1
                    continue
                await self._contexts.upsert(module_id, course.id, module, digest, now)
                updated += 1
                if previous is not None:
                    quarantined += await self._reground(module_id, module)
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise
        if quarantined:
            logger.warning("Context change for course %s quarantined %d questions", course.slug, quarantined)
        return {"updated": updated, "unchanged": unchanged, "quarantined": quarantined}

    async def _reground(self, module_id: uuid.UUID, context: dict) -> int:
        """Plan 6.2: a rewritten lesson re-runs the grounding check on its
        module's live questions; those that no longer match are quarantined."""
        passages = context_passages(context)
        stale = [
            q.id for q in await self._questions.live_by_module(module_id)
            if q.source_fact and not is_grounded(q.source_fact, passages)
        ]  # fmt: skip
        if stale:
            await self._questions.set_status(stale, "quarantined", ("live",))
        return len(stale)

    # ------------------------------------------------------------------ #
    # Pool views
    # ------------------------------------------------------------------ #
    @guarded("pool summary")
    async def summary(self, slug: str) -> dict:
        course = await self._course(slug)
        state = await self._pool.state(course.id)
        counts = await self._questions.status_counts(course.id)
        ready = course.exam_enabled and await self._pool.is_visible(course)
        return {
            "enabled": course.exam_enabled,
            "ready": ready,
            "live": state.total_live,
            "target": state.total_target,
            "code": course.exam_code,
            "modules": [
                {
                    "module_id": m.key or str(m.id),
                    "title": m.title,
                    "live": counts.get(m.id, {}).get("live", 0),
                    "target": state.targets.get(m.id, 0),
                    "quarantined": counts.get(m.id, {}).get("quarantined", 0),
                    "rejected": counts.get(m.id, {}).get("rejected", 0),
                }
                for m in state.modules
            ],
        }

    @guarded("list questions")
    async def list_questions(self, slug: str, status: str | None, module_ref: str | None) -> list[dict]:
        course = await self._course(slug)
        refs, labels = await self._module_ref_map(course.id)
        module_id = None
        if module_ref:
            module_id = refs.get(module_ref)
            if module_id is None:
                return []
        rows = await self._questions.list_questions(course.id, status, module_id, 500)
        lessons = {l.id: l for l in await self._courses.list_lessons(course.id)}
        reports = await self._questions.report_counts([q.id for q in rows])
        stats = await self._questions.stats_for([q.id for q in rows])
        return [self._admin_item(q, labels, lessons, reports, stats) for q in rows]

    def _admin_item(self, q: QuestionRecord, labels, lessons, reports, stats) -> dict:
        lesson = lessons.get(q.lesson_id)
        return {
            "id": q.id,
            "module_id": labels.get(q.module_id, str(q.module_id)),
            "lesson_id": (lesson.key if lesson and lesson.key else str(q.lesson_id)),
            "concept": q.concept,
            "kind": q.kind,
            "prompt": q.prompt,
            "options": q.options,
            "correct_index": q.correct_index,
            "explanation": q.explanation,
            "difficulty": q.difficulty,
            "status": q.status,
            "source": q.source,
            "reject_reason": q.reject_reason,
            "checks": q.checks or {},
            "reports": reports.get(q.id, 0),
            "stats": stats.get(q.id),
            "times_served": q.times_served,
            "updated_at": q.updated_at,
        }

    async def _item(self, course: CourseRecord, question_id: uuid.UUID) -> dict:
        q = await self._questions.get(question_id)
        if q is None:
            raise QuestionNotFound()
        _, labels = await self._module_ref_map(course.id)
        lessons = {l.id: l for l in await self._courses.list_lessons(course.id)}
        return self._admin_item(q, labels, lessons, await self._questions.report_counts([q.id]), await self._questions.stats_for([q.id]))

    # ------------------------------------------------------------------ #
    # Create / import / edit
    # ------------------------------------------------------------------ #
    async def _contexts_for(self, course_id: uuid.UUID) -> dict[uuid.UUID, dict]:
        return {m: c.context for m, c in (await self._contexts.get_many(course_id)).items()}

    @guarded("create question")
    async def create_manual(self, payload: dict, admin_id: uuid.UUID) -> dict:
        course = await self._course(str(payload.get("course_slug") or ""))
        refs, _ = await self._module_ref_map(course.id)
        module_ref = payload.get("module_id")
        expected_module = refs.get(str(module_ref)) if module_ref else None
        if module_ref and expected_module is None:
            raise ExamValidationError("module_id does not belong to this course")
        draft = parse_draft_item(payload)
        candidate = Candidate(line=1, draft=draft, expected_module_id=expected_module)
        await self._pipeline.verify(
            course, await self._courses.list_lessons(course.id), await self._contexts_for(course.id), [candidate], mode="manual"
        )
        if candidate.status == "invalid":
            await self._uow.rollback()
            raise ExamValidationError(candidate.reason or "invalid question")
        try:
            await store_candidates(self._questions, self._uow, course, [candidate], "manual", None, None)
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise
        audit("question_create", admin_id, course=course.slug, question=candidate.question_id, status=candidate.status)
        return await self._item(course, candidate.question_id)

    @guarded("import questions")
    async def import_questions(self, items: list[object], admin_id: uuid.UUID) -> ImportResult:
        if len(items) > MAX_IMPORT:
            raise ExamValidationError(f"At most {MAX_IMPORT} questions per request")
        result = ImportResult()
        by_course: dict[str, list[tuple[int, dict]]] = {}
        for line, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                result.rejected.append({"line": line, "reason": "not_an_object"})
                continue
            by_course.setdefault(str(item.get("course_slug") or ""), []).append((line, item))

        for slug, entries in by_course.items():
            course = await self._courses.get_course(slug) if slug else None
            if course is None:
                result.rejected.extend({"line": line, "reason": "unknown_course"} for line, _ in entries)
                continue
            refs, _ = await self._module_ref_map(course.id)
            lessons = await self._courses.list_lessons(course.id)
            contexts = await self._contexts_for(course.id)
            candidates: list[Candidate] = []
            for line, item in entries:
                module_ref = item.get("module_id")
                expected = refs.get(str(module_ref)) if module_ref else None
                if module_ref and expected is None:
                    result.rejected.append({"line": line, "reason": "course_mismatch"})
                    continue
                draft = parse_draft_item(item)
                candidates.append(Candidate(line=line, draft=draft, expected_module_id=expected, expected_course_slug=slug))
            # All lines of a course are checked together, so duplicates inside
            # the file are caught too.
            await self._pipeline.verify(course, lessons, contexts, candidates, mode="import")
            try:
                result.imported += await store_candidates(self._questions, self._uow, course, candidates, "seed", None, None)
                await self._uow.commit()
            except Exception:
                await self._uow.rollback()
                raise
            result.rejected.extend(
                {"line": c.line, "reason": c.reason or "rejected"} for c in candidates if c.status != "live"
            )
        result.rejected.sort(key=lambda r: r["line"])
        audit("question_import", admin_id, imported=result.imported, rejected=len(result.rejected))
        return result

    @guarded("edit question")
    async def edit(self, question_id: uuid.UUID, changes: dict, admin_id: uuid.UUID) -> dict:
        q = await self._questions.get(question_id)
        if q is None:
            raise QuestionNotFound()
        course = await self._courses.get_course_by_id(q.course_id)
        if course is None:
            raise QuestionNotFound()
        content_changed = any(k in changes and changes[k] is not None for k in CONTENT_FIELDS)
        wanted_status = changes.get("status")
        if wanted_status not in (None, "live"):
            raise ExamValidationError("status can only be set to live (restore); use void to remove a question")
        if q.status == "voided":
            raise ExamConflict("A voided question can't be edited or restored.")

        try:
            if not content_changed:
                if wanted_status == "live" and q.status in ("quarantined", "retired", "rejected"):
                    if q.status == "rejected":
                        raise ExamConflict("A rejected question must be edited so it passes the checks.")
                    await self._questions.set_status([q.id], "live", (q.status,))
                    await self._reports.resolve_for_question(q.id, admin_id, self._clock.now())
                    await self._uow.commit()
                    audit("question_restore", admin_id, question=q.id)
                return await self._item(course, q.id)

            merged = {
                "prompt": q.prompt, "options": q.options, "correct_index": q.correct_index,
                "explanation": q.explanation, "difficulty": q.difficulty, "kind": q.kind, "concept": q.concept,
                "lesson_id": str(q.lesson_id), "source_fact": q.source_fact,
                "why_others_wrong": q.why_others_wrong or [], "code": q.code, "expected_output": q.expected_output,
            }  # fmt: skip
            merged.update({k: v for k, v in changes.items() if k in CONTENT_FIELDS and v is not None})
            candidate = Candidate(line=1, draft=parse_draft_item(merged))
            await self._pipeline.verify(
                course, await self._courses.list_lessons(course.id), await self._contexts_for(course.id),
                [candidate], mode="manual", exclude_id=q.id,
            )  # fmt: skip
            if candidate.status == "invalid":
                raise ExamValidationError(candidate.reason or "invalid question")
            row = candidate.to_new_question(course.id, q.source, q.generator_model, q.generation_job_id)
            fields = {
                "module_id": row.module_id, "lesson_id": row.lesson_id, "concept": row.concept, "kind": row.kind,
                "difficulty": row.difficulty, "prompt": row.prompt, "options": row.options,
                "correct_index": row.correct_index, "explanation": row.explanation,
                "why_others_wrong": row.why_others_wrong, "source_fact": row.source_fact, "code": row.code,
                "code_language": row.code_language, "expected_output": row.expected_output,
                "prompt_norm": row.prompt_norm, "prompt_hash": row.prompt_hash, "embedding": row.embedding,
                "checks": row.checks, "reject_reason": row.reject_reason,
                # Passing returns it to live; failing keeps a live question out of draws.
                "status": "live" if candidate.status == "live" else ("rejected" if q.status == "rejected" else "quarantined"),
            }  # fmt: skip
            try:
                async with self._uow.savepoint():
                    await self._questions.update(q.id, fields)
            except DuplicateQuestion as exc:
                raise ExamConflict("Another question in this course already has this prompt and answer.") from exc
            if candidate.status == "live":
                await self._reports.resolve_for_question(q.id, admin_id, self._clock.now())
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise
        audit("question_edit", admin_id, question=q.id, result=candidate.status)
        return await self._item(course, q.id)

    # ------------------------------------------------------------------ #
    # Void and regrade
    # ------------------------------------------------------------------ #
    @guarded("void question")
    async def void(self, question_id: uuid.UUID, admin_id: uuid.UUID) -> dict:
        q = await self._questions.get_for_update(question_id)
        if q is None:
            await self._uow.rollback()
            raise QuestionNotFound()
        course = await self._courses.get_course_by_id(q.course_id)
        regraded = newly_passed = 0
        try:
            if q.status != "voided":
                await self._questions.set_status([q.id], "voided", (q.status,))
                await self._reports.resolve_for_question(q.id, admin_id, self._clock.now())
                await self._uow.commit()
                audit("question_void", admin_id, question=q.id)
            voided = await self._questions.ids_with_status(q.course_id, "voided")
            pending = 0
            for attempt in await self._attempts.graded_containing(q.id):
                outcome = regrade(attempt.results or [], voided, self._settings.exam_pass_percent)
                # Certificates and passes are never taken away.
                passed = bool(attempt.passed) or outcome.passed
                await self._attempts.update_grade(attempt.id, outcome.score, outcome.total, passed, outcome.module_breakdown)
                regraded += 1
                if outcome.passed and not attempt.passed and course is not None:
                    await self._certificates.issue_in_transaction(
                        attempt, course, outcome.score, outcome.total, self._clock.now()
                    )
                    newly_passed += 1
                    logger.info("Attempt %s passes after regrade; certificate issued", attempt.id)
                pending += 1
                if pending >= REGRADE_COMMIT_EVERY:
                    await self._uow.commit()
                    pending = 0
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise
        return {"id": q.id, "status": "voided", "regraded_attempts": regraded, "new_certificates": newly_passed}

    # ------------------------------------------------------------------ #
    # Jobs and quota
    # ------------------------------------------------------------------ #
    @guarded("get job")
    async def get_job(self, job_id: uuid.UUID) -> JobRecord:
        job = await self._jobs.get(job_id)
        if job is None:
            raise JobNotFound()
        return job

    @guarded("quota")
    async def quota(self) -> list[dict]:
        now = self._clock.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        resets_at = midnight + timedelta(days=1)
        usage = {u.provider: u for u in await self._logs.usage_since(midnight, now - timedelta(hours=1))}
        limits = {
            "gemini": (self._settings.gemini_model, self._settings.gemini_daily_request_limit, self._settings.gemini_daily_token_limit),
            "groq": (self._settings.groq_model, self._settings.groq_daily_request_limit, self._settings.groq_daily_token_limit),
        }
        paused_until = None
        for course in await self._courses.list_enabled_courses():
            job = await self._jobs.latest_for(course.id)
            if job is not None and job.status == "paused" and job.paused_until and job.paused_until > now:
                paused_until = max(paused_until or job.paused_until, job.paused_until)
        models = []
        for provider, (model, request_limit, token_limit) in limits.items():
            u = usage.get(provider)
            used_requests, used_tokens = (u.requests, u.tokens) if u else (0, 0)
            remaining = {
                "requests": max(0, request_limit - used_requests) if request_limit else None,
                "tokens": max(0, token_limit - used_tokens) if token_limit else None,
            }
            forecast = None
            if u and u.last_hour_tokens and remaining["tokens"] is not None:
                forecast = now + timedelta(hours=remaining["tokens"] / u.last_hour_tokens)
            elif u and u.last_hour_requests and remaining["requests"] is not None:
                forecast = now + timedelta(hours=remaining["requests"] / u.last_hour_requests)
            models.append(
                {
                    "model": model,
                    "provider": provider,
                    "used_today": {"requests": used_requests, "tokens": used_tokens},
                    "limit": {"requests": request_limit or None, "tokens": token_limit or None},
                    "remaining": remaining,
                    "resets_at": resets_at,
                    "forecast_exhausted_at": forecast if forecast and forecast < resets_at else None,
                    "paused_until": paused_until,
                    "rate_limits": u.latest_rate_limits if u else None,
                }
            )
        return models
