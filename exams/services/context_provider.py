"""The one source of module context (lesson material) for every exam path:
the refill job, manual questions, edits and seed imports all ground against
the same material.

- An uploaded context (PUT .../exam-context) always wins.
- With EXAM_ENABLED=true, a module without one gets context built from the
  lesson content in the database. The refill job stores it (marked
  source: auto, with a hash); when the lessons change, the new context is
  stored and the module's live questions are re-grounded against it, so
  questions the rewritten lessons no longer support are quarantined
  automatically - no admin step.
"""

import hashlib
import json
import logging
import uuid

from exams.domain.auto_context import build_module_context
from exams.domain.validation import context_passages, is_grounded
from exams.protocols import Clock, ContextStore, CourseReader, QuestionStore
from exams.records import CourseRecord

logger = logging.getLogger("webnest.exams.context")

AUTO_SOURCE = "auto"


def context_hash(context: dict) -> str:
    return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def is_auto(context: dict | None) -> bool:
    return isinstance(context, dict) and context.get("source") == AUTO_SOURCE


class ModuleContextProvider:
    def __init__(self, courses: CourseReader, contexts: ContextStore, questions: QuestionStore, clock: Clock, settings) -> None:
        self._courses = courses
        self._contexts = contexts
        self._questions = questions
        self._clock = clock
        self._settings = settings

    async def _built(self, course: CourseRecord) -> dict[uuid.UUID, dict]:
        """Context built from the lessons, per module that has usable text."""
        modules = {m.id: m for m in await self._courses.list_exam_modules(course.id)}
        by_module: dict[uuid.UUID, list] = {}
        for material in await self._courses.lesson_materials(course.id):
            if material.module_id in modules:
                by_module.setdefault(material.module_id, []).append(material)
        built: dict[uuid.UUID, dict] = {}
        for module_id, materials in by_module.items():
            module = modules[module_id]
            context = build_module_context(course.slug, course.title, module.key or str(module.id), module.title, materials)
            if context is not None:
                built[module_id] = context
        return built

    async def contexts(self, course: CourseRecord) -> dict[uuid.UUID, dict]:
        """Read only: stored contexts (uploaded, or auto from an earlier
        refill), plus - with EXAM_ENABLED - freshly built ones for modules
        that have none stored yet."""
        stored = {m: c.context for m, c in (await self._contexts.get_many(course.id)).items()}
        if not self._settings.exam_enabled:
            return stored
        for module_id, context in (await self._built(course)).items():
            if module_id not in stored or is_auto(stored[module_id]):
                stored[module_id] = context
        return stored

    async def refresh(self, course: CourseRecord) -> int:
        """Refill job only (the caller commits): stores the built contexts of
        modules without an uploaded one and re-grounds a module whose lessons
        changed. Returns how many questions were quarantined."""
        if not self._settings.exam_enabled:
            return 0
        stored = await self._contexts.get_many(course.id)
        quarantined = 0
        now = self._clock.now()
        for module_id, context in (await self._built(course)).items():
            existing = stored.get(module_id)
            if existing is not None and not is_auto(existing.context):
                continue  # an uploaded context wins
            digest = context_hash(context)
            if existing is not None and existing.context_hash == digest:
                continue
            await self._contexts.upsert(module_id, course.id, context, digest, now)
            if existing is not None:
                quarantined += await self.reground(module_id, context)
        if quarantined:
            logger.warning("Lessons of %s changed: %d questions quarantined for re-generation", course.slug, quarantined)
        return quarantined

    async def reground(self, module_id: uuid.UUID, context: dict) -> int:
        """Plan 6.2: questions whose source fact the new material no longer
        supports are quarantined (the refill job replaces them)."""
        passages = context_passages(context)
        stale = [
            q.id for q in await self._questions.live_by_module(module_id)
            if q.source_fact and not is_grounded(q.source_fact, passages)
        ]  # fmt: skip
        if stale:
            await self._questions.set_status(stale, "quarantined", ("live",))
        return len(stale)
