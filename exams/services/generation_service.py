"""The pool refill job (plan sections 6.2, 6.3, 6.8a): fills each module up to
its share of the fixed pool, 5 questions per model call, every draft through
the full verification pipeline, then stops. Runs in the background - no
learner ever waits for it - and pauses (never hammers the API) when quota or
the daily call limit runs out."""

import logging
import math
import uuid
from datetime import timedelta

from exams.domain.dedupe import concept_key
from exams.errors import DuplicateQuestion, JobAlreadyRunning
from exams.protocols import (
    Clock,
    ContextStore,
    CourseReader,
    JobStore,
    LlmClient,
    QuestionStore,
    UnitOfWork,
)
from exams.records import CourseRecord, JobRecord, LessonRecord, QuestionRecord
from exams.services.generation_prompts import (
    COURSE_CODE_LANGUAGES,
    DRAFT_SYSTEM_PROMPT,
    build_draft_prompt,
    parse_draft_item,
    parse_draft_reply,
)
from exams.services.context_provider import ModuleContextProvider
from exams.services.pool_service import PoolService
from exams.services.verification import Candidate, QuotaPause, VerificationPipeline
from exams.domain.validation import DraftQuestion

logger = logging.getLogger("webnest.exams.generation")

DRAFT_TEMPERATURE = 0.3
DRAFT_MAX_TOKENS = 8192
STALE_JOB_AFTER = timedelta(minutes=15)
# Calls per module per run: enough for a ~50% survival rate, bounded so a
# module whose drafts keep failing can't eat the whole day's budget.
CALLS_PER_MISSING_BATCH = 3
MAX_CALLS_PER_MODULE = 30
SEED_RECHECK_BATCH = 5
SEED_RECHECKS_PER_RUN = 4


def _difficulty_counts(count: int) -> dict[str, int]:
    easy = round(count * 0.3)
    medium = round(count * 0.5)
    return {"easy": easy, "medium": medium, "hard": max(0, count - easy - medium)}


def candidate_from_record(q: QuestionRecord, lesson: LessonRecord) -> Candidate:
    draft = DraftQuestion(
        prompt=q.prompt,
        options=list(q.options),
        correct_index=q.correct_index,
        explanation=q.explanation,
        difficulty=q.difficulty,
        kind=q.kind,
        concept=q.concept,
        lesson_ref=str(q.lesson_id),
        source_fact=q.source_fact,
        why_others_wrong=list(q.why_others_wrong or []),
        code=q.code,
        expected_output=q.expected_output,
    )
    return Candidate(line=0, draft=draft, lesson=lesson, checks=dict(q.checks or {}))


async def store_candidates(
    questions: QuestionStore,
    uow: UnitOfWork,
    course: CourseRecord,
    candidates: list[Candidate],
    source: str,
    model: str | None,
    job: JobRecord | None,
) -> int:
    """Inserts verified candidates (live or rejected; invalid ones can't be
    stored) and returns how many went live. Each insert has its own savepoint:
    if a racing job stored the same question first, the database refuses the
    copy and it is kept as a rejected duplicate."""
    created = 0
    for c in candidates:
        if c.status not in ("live", "rejected"):
            if job is not None and c.check:
                job.rejected[c.check] = job.rejected.get(c.check, 0) + 1
            continue
        row = c.to_new_question(course.id, source, model, job.id if job else None)
        try:
            async with uow.savepoint():
                c.question_id = await questions.insert(row)
        except DuplicateQuestion:
            c.status, c.check, c.reason = "rejected", "duplicate", "duplicate_exact"
            row.status, row.reject_reason = "rejected", "duplicate_exact"
            async with uow.savepoint():
                c.question_id = await questions.insert(row)
        if c.status == "live":
            created += 1
        elif job is not None and c.check:
            job.rejected[c.check] = job.rejected.get(c.check, 0) + 1
    return created


class GenerationService:
    def __init__(
        self,
        courses: CourseReader,
        questions: QuestionStore,
        contexts: ContextStore,
        jobs: JobStore,
        pool: PoolService,
        pipeline: VerificationPipeline,
        llm: LlmClient,
        uow: UnitOfWork,
        clock: Clock,
        settings,
        context_provider: ModuleContextProvider | None = None,
    ) -> None:
        self._courses = courses
        self._questions = questions
        self._contexts = contexts
        self._context_provider = context_provider or ModuleContextProvider(courses, contexts, questions, clock, settings)
        self._jobs = jobs
        self._pool = pool
        self._pipeline = pipeline
        self._llm = llm
        self._uow = uow
        self._clock = clock
        self._settings = settings

    async def start_job(self, course_id: uuid.UUID, trigger: str) -> tuple[JobRecord | None, bool]:
        """(job, created). Returns the running job instead when one exists."""
        now = self._clock.now()
        try:
            await self._jobs.fail_stale(now - STALE_JOB_AFTER, now)
            try:
                async with self._uow.savepoint():
                    job = await self._jobs.create_running(course_id, trigger, now)
            except JobAlreadyRunning:
                await self._uow.commit()  # keeps the stale-job cleanup
                return await self._jobs.running_for(course_id), False
            await self._uow.commit()
            return job, True
        except Exception:
            await self._uow.rollback()
            raise

    async def _save(self, job: JobRecord) -> None:
        job.heartbeat_at = self._clock.now()
        await self._jobs.save(job)
        await self._uow.commit()

    def _pick_drafter(self) -> str | None:
        """Gemini drafts, Groq if Gemini isn't configured - and only when a
        different model is available to cross-check."""
        available = self._llm.available_providers()
        if len(available) < 2:
            return None
        return "gemini" if "gemini" in available else available[0]

    async def run_job(self, job: JobRecord) -> JobRecord:
        """Runs a created job to completion (done / paused / failed). Never raises."""
        course = await self._courses.get_course_by_id(job.course_id)
        try:
            if course is None:
                raise RuntimeError("course no longer exists")
            await self._fill(course, job)
            await self._recheck_deferred(course, job)
            job.status = "done"
        except QuotaPause as pause:
            await self._uow.rollback()
            job.status = "paused"
            job.paused_until = pause.until
            job.errors.append(f"paused: {pause.reason}")
            logger.warning("Refill of course %s paused until %s: %s", job.course_id, pause.until, pause.reason)
        except Exception as exc:
            await self._uow.rollback()
            job.status = "failed"
            job.errors.append(f"failed: {type(exc).__name__}")
            logger.error("Refill job %s for course %s failed", job.id, job.course_id, exc_info=True)
        job.finished_at = self._clock.now()
        try:
            await self._save(job)
        except Exception:
            await self._uow.rollback()
            logger.error("Could not save the final state of refill job %s", job.id, exc_info=True)
        if course is not None:
            try:
                await self._pool.is_visible(course)
            except Exception:
                logger.warning("Readiness check after refill failed", exc_info=True)
        return job

    async def _fill(self, course: CourseRecord, job: JobRecord) -> None:
        state = await self._pool.state(course.id)
        if await self._context_provider.refresh(course):
            state = await self._pool.state(course.id)  # quarantines opened gaps
        await self._uow.commit()
        contexts = await self.module_contexts(course)
        lessons = await self._courses.list_lessons(course.id)
        modules = {m.id: m for m in state.modules}
        job.progress = {
            "modules_total": len(state.targets),
            "modules_done": sum(1 for m, t in state.targets.items() if state.live.get(m, 0) >= t),
            "modules": {},
        }
        if self._settings.exam_max_serves > 0:
            over = await self._questions.over_served(course.id, self._settings.exam_max_serves)
            if over:
                await self._questions.set_status(over, "retired", ("live",))
                await self._uow.commit()
                state = await self._pool.state(course.id)

        gaps = state.gaps()
        if not gaps:
            return
        drafter = self._pick_drafter()
        if drafter is None:
            raise QuotaPause("no_independent_checker", self._clock.now() + timedelta(hours=1))

        for module_id in sorted(gaps, key=lambda m: -gaps[m]):
            module = modules[module_id]
            label = module.key or str(module.id)
            context = contexts.get(module_id)
            if not context:
                job.errors.append(f"module {label}: no exam context uploaded")
                continue
            module_lessons = [l for l in lessons if l.module_id == module_id and l.status == "published"]
            if not module_lessons:
                continue
            await self._fill_module(course, job, module, label, context, module_lessons, lessons, contexts, drafter, state.targets[module_id])
            await self._update_done(course.id, state.targets, job)

    async def _fill_module(self, course, job, module, label, context, module_lessons, all_lessons, contexts, drafter, target) -> None:
        batch = self._settings.exam_gen_batch_size
        stats = job.progress["modules"].setdefault(label, {"target": target, "created": 0, "rejected": 0, "calls": 0})
        live = (await self._questions.live_counts(course.id)).get(module.id, 0)
        max_calls = min(MAX_CALLS_PER_MODULE, math.ceil(max(0, target - live) / batch) * CALLS_PER_MISSING_BATCH)
        context_lessons = [l for l in (context.get("lessons") or []) if isinstance(l, dict)]
        by_ref = {}
        for lesson in module_lessons:
            by_ref[str(lesson.id)] = lesson
            if lesson.key:
                by_ref[lesson.key] = lesson
        usable = [cl for cl in context_lessons if str(cl.get("lesson_id") or "") in by_ref]
        if not usable:
            job.errors.append(f"module {label}: context lessons don't match the course lessons")
            return
        last_pair: tuple[str, ...] = ()
        calls = 0
        while live < target and calls < max_calls:
            # Fewer than 80% of a provider's daily limit used: full speed;
            # above it, one batch per run (plan 6.8a).
            if calls and await self._pipeline.provider_usage_fraction(drafter) >= 0.8:
                job.errors.append(f"{drafter} above 80% of its daily limit; slowing down")
                break
            pair = await self._pick_lessons(course.id, usable, by_ref, last_pair)
            last_pair = tuple(str(cl.get("lesson_id")) for cl in pair)
            covered = await self._covered_concepts(course.id, [by_ref[str(cl["lesson_id"])].id for cl in pair])
            prompt = build_draft_prompt(
                course.slug,
                course.title,
                str(context.get("module_title") or module.title),
                pair,
                covered,
                [p for cl in pair for p in (cl.get("avoid_prompts") or []) if isinstance(p, str)],
                batch,
                _difficulty_counts(batch),
                COURSE_CODE_LANGUAGES.get(course.slug),
            )
            calls += 1
            job.calls += 1
            stats["calls"] += 1
            reply = await self._pipeline.call_llm(
                "draft", drafter, DRAFT_SYSTEM_PROMPT, prompt, DRAFT_TEMPERATURE, DRAFT_MAX_TOKENS,
                {"job_id": job.id, "course_id": course.id, "module_id": module.id, "requested": batch},
            )  # fmt: skip
            if reply is None:
                job.errors.append(f"module {label}: a drafting call failed 3 times and was skipped")
                await self._save(job)
                continue
            # A model that returns more than it was asked for can't overshoot
            # the target or spend extra checker tokens: only the batch is used.
            drafts = [parse_draft_item(item) for item in parse_draft_reply(reply.text)[:batch]]
            candidates = [
                Candidate(line=i, draft=d, expected_module_id=module.id) for i, d in enumerate(drafts) if d is not None
            ]
            invalid = len(drafts) - len(candidates)
            await self._pipeline.verify(course, all_lessons, contexts, candidates, mode="ai", drafter=drafter, job_id=job.id)
            created = await self._store(course, candidates, "ai", reply.model, job)
            rejected = len(candidates) - created + invalid
            job.created += created
            stats["created"] += created
            stats["rejected"] += rejected
            if invalid:
                job.rejected["parse"] = job.rejected.get("parse", 0) + invalid
            live = (await self._questions.live_counts(course.id)).get(module.id, 0)
            await self._save(job)
            logger.info(
                "Refill %s module %s: +%d live, %d rejected (%d/%d)", job.id, label, created, rejected, live, target
            )

    async def module_contexts(self, course: CourseRecord) -> dict[uuid.UUID, dict]:
        return await self._context_provider.contexts(course)

    async def _update_done(self, course_id: uuid.UUID, targets: dict, job: JobRecord) -> None:
        live = await self._questions.live_counts(course_id)
        job.progress["modules_done"] = sum(1 for m, t in targets.items() if live.get(m, 0) >= t)
        await self._save(job)

    async def _store(self, course: CourseRecord, candidates: list[Candidate], source: str, model: str | None, job: JobRecord | None) -> int:
        return await store_candidates(self._questions, self._uow, course, candidates, source, model, job)

    async def _pick_lessons(self, course_id, usable: list[dict], by_ref: dict, last_pair: tuple[str, ...]) -> list[dict]:
        """The one or two least-covered lessons, never the same set twice in a row."""
        counts: dict[uuid.UUID, int] = {}
        for row in await self._questions.dedupe_rows(course_id):
            counts[row.lesson_id] = counts.get(row.lesson_id, 0) + 1
        ranked = sorted(usable, key=lambda cl: (counts.get(by_ref[str(cl["lesson_id"])].id, 0), str(cl["lesson_id"])))
        size = 2 if len(ranked) >= 2 else 1
        pair = ranked[:size]
        if len(ranked) > size and tuple(str(cl["lesson_id"]) for cl in pair) == last_pair:
            pair = [ranked[0], ranked[size]] if size == 2 else [ranked[1]]
        return pair

    async def _covered_concepts(self, course_id, lesson_ids: list[uuid.UUID]) -> list[str]:
        wanted = set(lesson_ids)
        seen: list[str] = []
        keys: set[str] = set()
        for row in await self._questions.dedupe_rows(course_id):
            key = concept_key(row.concept)
            if row.lesson_id in wanted and key not in keys:
                keys.add(key)
                seen.append(row.concept)
        return seen

    async def _recheck_deferred(self, course: CourseRecord, job: JobRecord) -> None:
        """Seed questions that went live without a cross-check get it now;
        failing it quarantines them (plan 6.8b)."""
        drafter = None  # imported questions have no drafting model
        lessons = {l.id: l for l in await self._courses.list_lessons(course.id)}
        for _ in range(SEED_RECHECKS_PER_RUN):
            pending = await self._questions.unchecked_seed_questions(course.id, SEED_RECHECK_BATCH)
            pending = [q for q in pending if q.lesson_id in lessons]
            if not pending:
                return
            candidates = [candidate_from_record(q, lessons[q.lesson_id]) for q in pending]
            await self._pipeline.check_existing(candidates, drafter, {"job_id": job.id, "course_id": course.id})
            job.calls += 1
            for q, c in zip(pending, candidates):
                checks = dict(c.checks)
                if c.status == "pending":
                    checks["checker"] = "passed"
                    await self._questions.update(q.id, {"checks": checks})
                else:
                    checks["checker"] = "failed"
                    await self._questions.update(q.id, {"checks": checks, "reject_reason": c.reason})
                    await self._questions.set_status([q.id], "quarantined", ("live",))
                    logger.warning("Seed question %s quarantined after failing the deferred cross-check", q.id)
            await self._save(job)
