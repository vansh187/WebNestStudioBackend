"""Automatic verification (plan sections 6.4 and 6.5). A question becomes live
only if it passes every check; otherwise it is stored as rejected with the
reason (never shown to learners), or - when it can't even be stored (wrong
shape, unknown lesson, other course) - reported as invalid.

Checks run cheapest first so model quota is only spent on drafts that
already passed everything local: shape -> lesson/course -> grounding ->
exact/wording/public-quiz duplicates -> concept count -> code execution ->
embeddings (meaning) -> independent cross-check -> second cross-check for
hard and code questions -> answer balance.

Modes:
- ai:     everything is required. No checker model (other than the drafter)
          or no quota -> QuotaPause, so the job pauses instead of publishing
          an unchecked question.
- manual: same checks; if no model quota is left the cross-check is recorded
          as "skipped_no_quota" and the refill job checks it later.
- import: the cross-check is deferred to the refill job ("deferred") so a
          500-line import never waits minutes on model calls.
"""

import asyncio
import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from exams.domain.dedupe import PoolEntry, concept_is_full, concept_key, semantic_duplicate, textual_duplicate
from exams.domain.validation import (
    CODE_KINDS,
    DIFFICULTIES,
    KINDS,
    DraftQuestion,
    check_shape,
    context_passages,
    is_grounded,
    normalize_text,
    option_as_output,
    prompt_hash,
    tokens,
)
from exams.errors import ExamSignal, ProviderFailure, QuotaExhausted
from exams.protocols import Clock, CodeRunner, Embedder, GenerationLogStore, LlmClient, QuestionStore
from exams.records import CourseRecord, GenerationLogEntry, LessonRecord, NewQuestion
from exams.services.generation_prompts import (
    CHECK_SYSTEM_PROMPT,
    CHECK_SYSTEM_PROMPT_ALT,
    COURSE_CODE_LANGUAGES,
    build_check_prompt,
    parse_check_reply,
)

logger = logging.getLogger("webnest.exams.verification")

LLM_PURPOSES = ("draft", "check", "embed")
CALL_RETRY_DELAYS = (2.0, 5.0)  # a failed call is retried twice, then skipped
CHECK_MAX_TOKENS = 2048
PROVIDER_PAUSE_FRACTION = 0.95
PROVIDER_SLOW_FRACTION = 0.80
DEFAULT_QUOTA_PAUSE = timedelta(hours=1)


class QuotaPause(ExamSignal):
    """Stop refilling until `until` (quota, daily limit, or no independent checker)."""

    def __init__(self, reason: str, until: datetime) -> None:
        super().__init__(reason)
        self.reason = reason
        self.until = until


def next_utc_midnight(now: datetime) -> datetime:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class Candidate:
    line: int
    draft: DraftQuestion
    expected_module_id: uuid.UUID | None = None
    expected_course_slug: str | None = None
    lesson: LessonRecord | None = None
    status: str = "pending"  # pending -> live | rejected | invalid
    reason: str | None = None
    check: str | None = None  # which check failed (for job statistics)
    checks: dict = field(default_factory=dict)
    prompt_norm: str = ""
    prompt_hash: str = ""
    token_set: frozenset = frozenset()
    embedding: list[float] | None = None
    code_language: str | None = None
    question_id: uuid.UUID | None = None

    def fail(self, check: str, reason: str, storable: bool = True) -> None:
        self.status = "rejected" if storable and self.lesson is not None and self._storable() else "invalid"
        self.check = check
        self.reason = reason

    def _storable(self) -> bool:
        d = self.draft
        return (
            bool((d.prompt or "").strip())
            and isinstance(d.options, list)
            and len(d.options) == 4
            and all(isinstance(o, str) for o in d.options)
            and isinstance(d.correct_index, int)
            and 0 <= d.correct_index <= 3
            and d.difficulty in DIFFICULTIES
            and d.kind in KINDS
            and bool((d.concept or "").strip())
        )

    def to_new_question(self, course_id: uuid.UUID, source: str, generator_model: str | None, job_id: uuid.UUID | None) -> NewQuestion:
        d = self.draft
        return NewQuestion(
            course_id=course_id,
            module_id=self.lesson.module_id,
            lesson_id=self.lesson.id,
            concept=d.concept.strip()[:120],
            kind=d.kind,
            difficulty=d.difficulty,
            prompt=d.prompt.strip(),
            options=[o.strip() for o in d.options],
            correct_index=d.correct_index,
            explanation=(d.explanation or "").strip(),
            status="live" if self.status == "live" else "rejected",
            source=source,
            prompt_norm=self.prompt_norm,
            prompt_hash=self.prompt_hash,
            why_others_wrong=[r.strip() for r in d.why_others_wrong] if d.why_others_wrong else None,
            source_fact=d.source_fact,
            code=d.code,
            code_language=self.code_language if d.kind in CODE_KINDS else None,
            expected_output=d.expected_output,
            reject_reason=self.reason if self.status != "live" else None,
            embedding=self.embedding,
            checks=self.checks,
            generator_model=generator_model,
            generation_job_id=job_id,
        )


class VerificationPipeline:
    def __init__(
        self,
        questions: QuestionStore,
        logs: GenerationLogStore,
        llm: LlmClient,
        embedder: Embedder,
        runner: CodeRunner,
        clock: Clock,
        rng: random.Random,
        settings,
    ) -> None:
        self._questions = questions
        self._logs = logs
        self._llm = llm
        self._embedder = embedder
        self._runner = runner
        self._clock = clock
        self._rng = rng
        self._settings = settings

    # ------------------------------------------------------------------ #
    # Model call budget and logging
    # ------------------------------------------------------------------ #
    async def ensure_call_budget(self, provider: str | None) -> None:
        """Raises QuotaPause when today's call limit, or 95% of a configured
        provider limit, is reached."""
        now = self._clock.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        calls = await self._logs.calls_since(midnight, LLM_PURPOSES)
        if calls >= self._settings.exam_gen_daily_call_limit:
            raise QuotaPause("daily_call_limit", next_utc_midnight(now))
        if provider is None:
            return
        fraction = await self.provider_usage_fraction(provider)
        if fraction >= PROVIDER_PAUSE_FRACTION:
            raise QuotaPause(f"{provider}_quota_95", next_utc_midnight(now))

    async def provider_usage_fraction(self, provider: str) -> float:
        limits = {
            "gemini": (self._settings.gemini_daily_request_limit, self._settings.gemini_daily_token_limit),
            "groq": (self._settings.groq_daily_request_limit, self._settings.groq_daily_token_limit),
        }.get(provider, (0, 0))
        if not any(limits):
            return 0.0
        now = self._clock.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        usage = {u.provider: u for u in await self._logs.usage_since(midnight, now - timedelta(hours=1))}.get(provider)
        if usage is None:
            return 0.0
        fractions = []
        if limits[0]:
            fractions.append(usage.requests / limits[0])
        if limits[1]:
            fractions.append(usage.tokens / limits[1])
        return max(fractions) if fractions else 0.0

    async def call_llm(
        self,
        purpose: str,
        provider: str,
        system_prompt: str,
        user_message: str,
        temperature: float,
        max_tokens: int,
        context: dict,
    ):
        """One model call with the budget check, two retries on failure and a
        log row per attempt. Returns the reply, or None after three failures.
        QuotaExhausted becomes QuotaPause straight away (no retry storm)."""
        for attempt, delay in enumerate((0.0, *CALL_RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            await self.ensure_call_budget(provider)
            try:
                reply = await self._llm.complete_json(provider, system_prompt, user_message, temperature, max_tokens)
            except QuotaExhausted as exc:
                await self._log(purpose, provider, None, context, error="quota exhausted")
                until = self._clock.now() + (
                    timedelta(seconds=exc.retry_after) if exc.retry_after else DEFAULT_QUOTA_PAUSE
                )
                raise QuotaPause(f"{provider}_quota", until) from exc
            except ProviderFailure as exc:
                await self._log(purpose, provider, None, context, error=str(exc))
                logger.warning("%s call to %s failed (attempt %d): %s", purpose, provider, attempt + 1, exc)
                continue
            await self._log(purpose, provider, reply, context)
            return reply
        return None

    async def _log(self, purpose: str, provider: str | None, reply, context: dict, error: str | None = None, **counts) -> None:
        try:
            await self._logs.add(
                GenerationLogEntry(
                    purpose=purpose,
                    provider=provider,
                    model=getattr(reply, "model", None),
                    job_id=context.get("job_id"),
                    course_id=context.get("course_id"),
                    module_id=context.get("module_id"),
                    requested_count=context.get("requested", 0),
                    prompt_tokens=getattr(reply, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(reply, "completion_tokens", 0) or 0,
                    rate_limits=getattr(reply, "rate_limits", None),
                    error=error,
                    **counts,
                )
            )
        except Exception:
            # Logging must never break verification.
            logger.warning("Could not write a generation log row", exc_info=True)

    # ------------------------------------------------------------------ #
    # Verification
    # ------------------------------------------------------------------ #
    async def verify(
        self,
        course: CourseRecord,
        lessons: list[LessonRecord],
        contexts: dict[uuid.UUID, dict],
        candidates: list[Candidate],
        mode: str,
        drafter: str | None = None,
        job_id: uuid.UUID | None = None,
        exclude_id: uuid.UUID | None = None,
    ) -> None:
        """Sets status/reason/checks on every candidate in place."""
        log_context = {"job_id": job_id, "course_id": course.id}
        lesson_by_ref: dict[str, LessonRecord] = {}
        for lesson in lessons:
            if lesson.status == "archived":
                continue
            lesson_by_ref[str(lesson.id)] = lesson
            if lesson.key:
                lesson_by_ref[lesson.key] = lesson
        avoid_sets = [
            frozenset(tokens(prompt))
            for context in contexts.values()
            for lesson in (context.get("lessons") or [])
            if isinstance(lesson, dict)
            for prompt in (lesson.get("avoid_prompts") or [])
            if isinstance(prompt, str)
        ]
        pool = [
            PoolEntry(
                id=row.id,
                lesson_id=row.lesson_id,
                concept_key=concept_key(row.concept),
                prompt_hash=row.prompt_hash,
                tokens=frozenset(row.prompt_norm.split()),
                embedding=tuple(row.embedding) if row.embedding else None,
            )
            for row in await self._questions.dedupe_rows(course.id, exclude_id)
        ]
        language = COURSE_CODE_LANGUAGES.get(course.slug)

        # 1-6: local checks, in input order; accepted drafts join the pool so
        # a batch can't contain two copies of one question.
        accepted: list[PoolEntry] = []
        for c in candidates:
            self._local_checks(c, course, lesson_by_ref, contexts, pool + accepted, avoid_sets, mode)
            if c.status == "pending":
                accepted.append(self._entry(c))

        # 7: code really runs.
        for c in candidates:
            if c.status == "pending" and c.draft.kind in CODE_KINDS:
                await self._code_check(c, language, log_context)

        # 8: meaning (embeddings), compared with the pool and earlier drafts.
        await self._semantic_check([c for c in candidates if c.status == "pending"], pool, mode, log_context)

        # 9-10: independent cross-checks.
        survivors = [c for c in candidates if c.status == "pending"]
        if survivors:
            if mode == "import":
                for c in survivors:
                    c.checks["checker"] = "deferred"
            else:
                await self._cross_check(survivors, mode, drafter, log_context)

        for c in candidates:
            if c.status == "pending":
                c.status = "live"
                self._balance(c)

    def _entry(self, c: Candidate) -> PoolEntry:
        return PoolEntry(
            id=None,
            lesson_id=c.lesson.id,
            concept_key=concept_key(c.draft.concept),
            prompt_hash=c.prompt_hash,
            tokens=c.token_set,
            embedding=tuple(c.embedding) if c.embedding else None,
        )

    def _local_checks(self, c, course, lesson_by_ref, contexts, pool, avoid_sets, mode) -> None:
        d = c.draft
        c.lesson = lesson_by_ref.get(d.lesson_ref or "")
        if c.lesson is None:
            c.fail("lesson", "unknown_lesson", storable=False)
            return
        if c.lesson.course_id != course.id or (
            c.expected_course_slug is not None and c.expected_course_slug != course.slug
        ):
            c.lesson = None
            c.fail("lesson", "course_mismatch", storable=False)
            return
        if c.expected_module_id is not None and c.lesson.module_id != c.expected_module_id:
            c.lesson = None
            c.fail("lesson", "course_mismatch", storable=False)
            return

        problems = check_shape(d, require_rationale=(mode == "ai"))
        c.prompt_norm = normalize_text(d.prompt)
        if c._storable():
            c.prompt_hash = prompt_hash(c.prompt_norm, d.options[d.correct_index])
        c.token_set = frozenset(c.prompt_norm.split())
        if problems:
            c.fail("shape", "shape: " + "; ".join(problems[:3]))
            return

        context = contexts.get(c.lesson.module_id)
        if d.source_fact:
            if context is None:
                c.checks["grounded"] = "skipped_no_context"
                if mode == "ai":
                    c.fail("grounding", "no_module_context")
                    return
            elif is_grounded(d.source_fact, context_passages(context)):
                c.checks["grounded"] = True
            else:
                c.checks["grounded"] = False
                c.fail("grounding", "source_fact not found in the lesson material")
                return
        elif mode == "ai":
            c.checks["grounded"] = False
            c.fail("grounding", "source_fact missing")
            return
        else:
            c.checks["grounded"] = "skipped"

        reason = textual_duplicate(self._entry(c), pool, avoid_sets, self._settings.exam_duplicate_jaccard)
        if reason:
            c.checks["duplicate"] = {"reason": reason}
            c.fail("duplicate", reason)
            return
        if concept_is_full(self._entry(c), pool, self._settings.exam_max_per_concept):
            c.fail("concept", "concept already covered for this lesson")
            return

    async def _code_check(self, c: Candidate, language: str | None, log_context: dict) -> None:
        if language is None or not self._runner.supports(language):
            c.checks["executed"] = {"ran": False, "reason": "not_runnable"}
            c.fail("code", "code questions can't be verified for this course")
            return
        c.code_language = language
        if c.draft.kind == "spot_the_bug" and not c.draft.expected_output:
            # The correct option describes the bug, not an output; nothing to compare.
            c.checks["executed"] = {"ran": False, "reason": "not_required"}
            return
        if self._runner.is_external(language):
            now = self._clock.now()
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if await self._logs.calls_since(midnight, ("run",)) >= self._settings.exam_code_run_daily_limit:
                c.checks["executed"] = {"ran": False, "reason": "daily_run_limit"}
                c.fail("code", "code_run_limit")
                return
        outcome = await self._runner.run(language, c.draft.code or "")
        if self._runner.is_external(language):
            await self._log("run", "jdoodle", None, log_context, error=outcome.error or None)
        output = _normalize_output(outcome.output)
        expected = _normalize_output(c.draft.expected_output or "")
        matches = outcome.ran and output == expected
        if matches and c.draft.kind == "code_output":
            matches = _normalize_output(option_as_output(c.draft.options[c.draft.correct_index])) == output
        c.checks["executed"] = {"ran": outcome.ran, "matches": matches, "output": output[:500]}
        if not outcome.ran:
            c.fail("code", f"code did not run: {outcome.error[:120]}")
        elif not matches:
            c.fail("code", "real output differs from the keyed answer")

    async def _semantic_check(self, survivors: list[Candidate], pool: list[PoolEntry], mode: str, log_context: dict) -> None:
        if not survivors:
            return
        texts = [f"{c.draft.prompt}\n{c.draft.options[c.draft.correct_index]}" for c in survivors]
        try:
            if mode == "ai":
                await self.ensure_call_budget("gemini")
            vectors, used = await self._embedder.embed(texts)
            await self._log("embed", "gemini", None, {**log_context, "requested": len(texts)})
        except QuotaExhausted as exc:
            await self._log("embed", "gemini", None, log_context, error="quota exhausted")
            if mode == "ai":
                raise QuotaPause("gemini_embedding_quota", self._clock.now() + DEFAULT_QUOTA_PAUSE) from exc
            for c in survivors:
                c.checks["semantic"] = "skipped_no_quota"
            return
        except ProviderFailure as exc:
            await self._log("embed", "gemini", None, log_context, error=str(exc))
            logger.warning("Embedding check skipped: %s", exc)
            for c in survivors:
                c.checks["semantic"] = "skipped_unavailable"
            return
        accepted: list[PoolEntry] = []
        for c, vector in zip(survivors, vectors):
            c.embedding = vector
            match = semantic_duplicate(
                self._entry(c),
                pool + accepted,
                self._settings.exam_duplicate_cosine,
                self._settings.exam_duplicate_cosine_same_lesson,
            )
            c.checks["duplicate"] = {"nearest_id": str(match.nearest_id) if match.nearest_id else None, "similarity": match.similarity}
            if match.duplicate:
                c.fail("semantic", "duplicate_meaning")
            else:
                accepted.append(self._entry(c))

    def _checker_for(self, drafter: str | None) -> str | None:
        available = self._llm.available_providers()
        others = [p for p in available if p != drafter]
        if others:
            return others[0] if drafter else ("groq" if "groq" in others else others[0])
        return None

    async def _cross_check(self, survivors: list[Candidate], mode: str, drafter: str | None, log_context: dict) -> None:
        checker = self._checker_for(drafter)
        if checker is None:
            if mode == "ai":
                # The same model never checks its own questions.
                raise QuotaPause("no_independent_checker", self._clock.now() + DEFAULT_QUOTA_PAUSE)
            for c in survivors:
                c.checks["checker"] = "skipped_no_quota"
            return
        try:
            await self._run_check(survivors, checker, CHECK_SYSTEM_PROMPT, shuffle=False, log_context=log_context)
            second = [c for c in survivors if c.status == "pending" and (c.draft.difficulty == "hard" or c.draft.kind in CODE_KINDS)]
            if second:
                await self._run_check(second, checker, CHECK_SYSTEM_PROMPT_ALT, shuffle=True, log_context=log_context)
        except QuotaPause:
            if mode == "ai":
                raise
            for c in survivors:
                if c.status == "pending":
                    c.checks["checker"] = "skipped_no_quota"

    async def check_existing(self, candidates: list[Candidate], drafter: str | None, log_context: dict) -> None:
        """Cross-checks already-stored questions (seed questions whose check
        was deferred). Raises QuotaPause when no quota is left."""
        await self._cross_check(candidates, "ai", drafter, log_context)

    async def _run_check(self, items: list[Candidate], checker: str, system_prompt: str, shuffle: bool, log_context: dict) -> None:
        order = list(items)
        self._rng.shuffle(order)
        shown: list[tuple[str, DraftQuestion, list[str]]] = []
        maps: dict[str, tuple[Candidate, list[int]]] = {}
        for i, c in enumerate(order):
            label = f"q{i + 1}"
            option_order = list(range(4))
            if shuffle:
                self._rng.shuffle(option_order)
            shown.append((label, c.draft, [c.draft.options[o] for o in option_order]))
            maps[label] = (c, option_order)
        reply = await self.call_llm(
            "check", checker, system_prompt, build_check_prompt(shown), 0.0, CHECK_MAX_TOKENS,
            {**log_context, "requested": len(items)},
        )  # fmt: skip
        answers = parse_check_reply(reply.text) if reply is not None else {}
        run_name = "checker_2" if shuffle else "checker"
        for label, (c, option_order) in maps.items():
            answer = answers.get(label)
            if answer is None:
                c.checks.setdefault("checker_runs", []).append({"model": checker, "run": run_name, "answer": None})
                c.fail(run_name, "checker gave no answer")
                continue
            displayed = answer["answer"]
            original = option_order[displayed] if 0 <= displayed < 4 else -1
            agrees = original == c.draft.correct_index
            record = {
                "model": getattr(reply, "model", checker),
                "run": run_name,
                "answer": original,
                "agrees": agrees,
                "single_correct": answer["single_correct"],
                "version_issue": answer["version_issue"],
                "confidence": answer["confidence"],
            }
            c.checks.setdefault("checker_runs", []).append(record)
            c.checks["checker"] = "passed"
            if not agrees:
                c.fail(run_name, "independent checker picked a different answer")
            elif not answer["single_correct"]:
                c.fail(run_name, "checker found more than one or no correct option")
            elif answer["version_issue"]:
                c.fail(run_name, "checker reported a version issue")
            elif answer["confidence"] < self._settings.exam_checker_min_confidence:
                c.fail(run_name, "checker confidence too low")

    def _balance(self, c: Candidate) -> None:
        """Plan 6.4 #7: re-shuffle options so correct answers spread over A-D."""
        d = c.draft
        order = list(range(4))
        self._rng.shuffle(order)
        reasons = None
        if d.why_others_wrong and len(d.why_others_wrong) == 3:
            wrong_original = [i for i in range(4) if i != d.correct_index]
            reason_of = dict(zip(wrong_original, d.why_others_wrong))
            reasons = [reason_of[o] for o in order if o != d.correct_index]
        d.options = [d.options[o] for o in order]
        d.correct_index = order.index(d.correct_index)
        if reasons is not None:
            d.why_others_wrong = reasons


def _normalize_output(text: str) -> str:
    lines = [line.rstrip() for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)
