import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.exceptions import (
    ConflictError,
    ForbiddenError,
    GenerationUnavailableError,
    InvalidPromptError,
    NotFoundError,
    RateLimitedError,
)
from database.chatbot_limit_persistence import ChatbotLimitPersistence
from database.chatbot_persistence import ChatbotPersistence
from database.chatbot_plan_persistence import ChatbotPlanPersistence
from database.lead_persistence import LeadPersistence
from database.models import ChatbotPlan, ChatbotThread, User
from services.chatbot_pipeline import (
    COLLECTED_FIELD_KEYS,
    MalformedChatOutputError,
    contains_pricing_language,
    parse_chat_turn,
    parse_plan_output,
    scrub_pricing_language,
)
from services.llm_provider import GenerationFailedError, LLMProvider

logger = logging.getLogger("webnest.chatbot")

RATE_LIMIT_WINDOW = timedelta(hours=1)
MAX_TURN_RETRIES = 1
MAX_PLAN_RETRIES = 1

CHAT_SYSTEM_PROMPT = """You are WebNest Studio's project-enquiry assistant. Your job is to have a
short, friendly conversation with a logged-in visitor to gather exactly these
fields before recommending a build plan:
- project_type (e.g. portfolio site, e-commerce store, SaaS web app, booking platform...)
- project_goal (what the project needs to achieve)
- pages_features (list of pages/sections/features they need)
- budget_range (a rough band is fine, do not push hard for exact numbers)
- timeline_expectation (when they want it live)

You are given the conversation so far and the fields already collected. Ask
ONE clear follow-up question at a time for whichever field(s) are still
missing/unclear. Do not ask about contact details - those are already on file.
Never discuss specific pricing, quotes, or dollar/rupee figures yourself -
if asked, say the team will follow up with pricing separately.

Once project_type, project_goal, pages_features, budget_range and
timeline_expectation are all reasonably filled in, set ready_for_plan=true
and write a reply that summarizes what you understood and tells the user
you can now generate their build plan.

Respond with ONLY this JSON object, no markdown fences, no commentary:
{
  "reply": "...",
  "collected_fields": {
    "project_type": "..." or null,
    "project_goal": "..." or null,
    "pages_features": ["...", "..."] or null,
    "budget_range": "..." or null,
    "timeline_expectation": "..." or null
  },
  "ready_for_plan": true or false
}

Always return your best current understanding of ALL five fields (merging
what's newly said with what was already known - never drop a previously
known value just because this turn didn't mention it), not just what changed
this turn."""

PLAN_SYSTEM_PROMPT_TEMPLATE = """You are producing a week-by-week BUILD/DELIVERY PLAN for a WebNest Studio web
project, based on the gathered requirements below. This is a business/
timeline planning document only.

STRICT RULE: Do NOT mention pricing, cost, budget figures, dollar/rupee
amounts, hourly rates, or payment terms ANYWHERE in your output - not even
indirectly (e.g. "budget-friendly", "premium package"). Omit any reference
to cost entirely.

Based on the project type and scope, estimate a sensible total number of
build weeks (typically 2-10 depending on complexity) and break it into
sequential phases. For each phase return week_range (e.g. "Week 1-2"), a
short title, and a 1-3 sentence description of what gets built/delivered in
that phase (discovery/design, core build, features, content/integration,
QA/launch - adapt phase names to the actual project type).

Project requirements:
- Project type: {project_type}
- Goal: {project_goal}
- Pages/features needed: {pages_features}
- Timeline expectation: {timeline_expectation}

Return ONLY valid JSON, no markdown fences, no commentary:
{{
  "total_weeks": <int>,
  "weeks": [
    {{"week_range": "Week 1-2", "title": "...", "description": "..."}}
  ]
}}"""


class ChatbotService:
    """Orchestrates the project-enquiry conversation: turn-by-turn structured
    field extraction via one stateless LLM call per turn, then a final
    week-by-week (no pricing) build-plan generation, Lead creation/update,
    and rate limiting - mirrors GenerationService's shape throughout."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._threads = ChatbotPersistence(session)
        self._limits = ChatbotLimitPersistence(session)
        self._plans = ChatbotPlanPersistence(session)
        self._leads = LeadPersistence(session)
        self._settings = settings
        self._llm = LLMProvider(settings)

    async def start_thread_with_message(self, user: User, first_message: str) -> tuple[ChatbotThread, str, bool]:
        self._validate_message(first_message)
        thread = await self._threads.create_thread(
            user_id=user.id,
            contact_name=user.full_name,
            contact_email=user.email,
            contact_phone=user.phone_number,
        )
        reply, ready_for_plan = await self._process_turn(user.id, thread, first_message)
        return thread, reply, ready_for_plan

    async def send_message(self, user_id: uuid.UUID, thread_id: uuid.UUID, message: str) -> tuple[ChatbotThread, str, bool]:
        self._validate_message(message)
        thread = await self._threads.get_by_id(thread_id)
        if thread is None:
            raise NotFoundError("Chatbot thread not found")
        if thread.user_id != user_id:
            raise ForbiddenError("This conversation does not belong to you")

        reply, ready_for_plan = await self._process_turn(user_id, thread, message)
        return thread, reply, ready_for_plan

    async def get_thread_detail(self, user_id: uuid.UUID, thread_id: uuid.UUID) -> ChatbotThread:
        thread = await self._threads.get_with_messages(thread_id)
        if thread is None:
            raise NotFoundError("Chatbot thread not found")
        if thread.user_id != user_id:
            raise ForbiddenError("This conversation does not belong to you")
        return thread

    async def get_limit_status(self, user_id: uuid.UUID) -> tuple[int, int, datetime]:
        limit = await self._limits.get_by_user(user_id)
        now = datetime.now(timezone.utc)
        limit_per_hour = self._settings.chatbot_rate_limit_per_hour

        if limit is None:
            return limit_per_hour, limit_per_hour, now + RATE_LIMIT_WINDOW

        window_start = _as_aware(limit.window_start)
        if now - window_start >= RATE_LIMIT_WINDOW:
            return limit_per_hour, limit_per_hour, now + RATE_LIMIT_WINDOW

        remaining = max(0, limit_per_hour - limit.count)
        return remaining, limit_per_hour, window_start + RATE_LIMIT_WINDOW

    async def generate_plan(self, user: User, thread_id: uuid.UUID) -> tuple[ChatbotPlan, uuid.UUID | None, bool]:
        thread = await self._threads.get_by_id(thread_id)
        if thread is None:
            raise NotFoundError("Chatbot thread not found")
        if thread.user_id != user.id:
            raise ForbiddenError("This conversation does not belong to you")

        missing = [
            label
            for label, value in (
                ("project type", thread.project_type),
                ("project goal", thread.project_goal),
                ("pages/features", thread.pages_features),
                ("budget range", thread.budget_range),
                ("timeline expectation", thread.timeline_expectation),
            )
            if not value
        ]
        if missing:
            raise ConflictError(f"Not enough information gathered yet - still missing: {', '.join(missing)}")

        weeks, total_weeks, provider = await self._run_plan_llm(thread)

        plan = await self._plans.create(
            thread_id=thread.id,
            user_id=user.id,
            project_type=thread.project_type,
            total_weeks=total_weeks,
            plan_weeks_json=json.dumps(weeks),
            provider_used=provider,
        )

        lead_created = await self._upsert_lead(user, thread)
        await self._threads.update_state(
            thread,
            collected_fields_json=thread.collected_fields_json,
            project_type=thread.project_type,
            project_goal=thread.project_goal,
            pages_features=thread.pages_features,
            budget_range=thread.budget_range,
            timeline_expectation=thread.timeline_expectation,
            status="plan_generated",
        )
        return plan, thread.lead_id, lead_created

    async def check_and_consume_rate_limit(self, user_id: uuid.UUID) -> None:
        """Public check+consume pair for callers with no LLM call of their own
        to gate on (e.g. the "email me my plan" endpoint) - safe to consume
        immediately since there's no provider call whose failure would need
        to be exempted from the charge."""
        await self._reject_if_already_over_limit(user_id)
        await self._consume_rate_limit(user_id)

    async def reject_if_over_limit(self, user_id: uuid.UUID) -> None:
        """Public wrapper so the chat orchestrator can pre-check this bucket
        before its own LLM call (intent classification) - paired with
        consume_rate_limit() below, called only after that LLM call actually
        succeeds, so a provider failure never burns the user's quota (same
        rule GenerationService follows for its own rate limit)."""
        await self._reject_if_already_over_limit(user_id)

    async def consume_rate_limit(self, user_id: uuid.UUID) -> None:
        await self._consume_rate_limit(user_id)

    async def get_plan(self, user_id: uuid.UUID, plan_id: uuid.UUID) -> ChatbotPlan:
        plan = await self._plans.get_by_id(plan_id)
        if plan is None:
            raise NotFoundError("Plan not found")
        if plan.user_id != user_id:
            raise ForbiddenError("This plan does not belong to you")
        return plan

    # ---- internals ----

    def _validate_message(self, message: str) -> None:
        if not message or not message.strip():
            raise InvalidPromptError("The message must not be empty")
        if len(message) > self._settings.chatbot_max_message_length:
            raise InvalidPromptError(
                f"The message must be at most {self._settings.chatbot_max_message_length} characters"
            )

    async def _process_turn(self, user_id: uuid.UUID, thread: ChatbotThread, message: str) -> tuple[str, bool]:
        await self._reject_if_already_over_limit(user_id)

        current_fields = _safe_load_fields(thread.collected_fields_json)
        transcript = await self._build_transcript(thread.id)
        user_message = self._build_turn_message(transcript, current_fields, message)

        reply, ready_for_plan, merged_fields, provider = await self._run_turn_llm(user_message, current_fields)
        await self._consume_rate_limit(user_id)

        await self._threads.append_message(thread.id, role="user", content=message.strip())
        await self._threads.append_message(thread.id, role="assistant", content=reply)

        status = "ready_for_plan" if ready_for_plan else "in_progress"
        await self._threads.update_state(
            thread,
            collected_fields_json=json.dumps(merged_fields),
            project_type=merged_fields.get("project_type"),
            project_goal=merged_fields.get("project_goal"),
            pages_features=_stringify_pages_features(merged_fields.get("pages_features")),
            budget_range=merged_fields.get("budget_range"),
            timeline_expectation=merged_fields.get("timeline_expectation"),
            status=status,
        )
        return reply, ready_for_plan

    async def _build_transcript(self, thread_id: uuid.UUID) -> str:
        thread = await self._threads.get_with_messages(thread_id)
        if thread is None:
            return ""
        # A cap of 0 (or misconfigured negative) must mean "no history", not
        # Python's list[-0:] == list[0:] == everything - guard explicitly.
        cap = max(self._settings.chatbot_max_transcript_messages, 0)
        messages = thread.messages[-cap:] if cap > 0 else []
        return "\n".join(f"{m.role}: {m.content}" for m in messages)

    def _build_turn_message(self, transcript: str, current_fields: dict, message: str) -> str:
        fields_json = json.dumps({k: current_fields.get(k) for k in COLLECTED_FIELD_KEYS})
        parts = []
        if transcript:
            parts.append(f"Conversation so far:\n{transcript}")
        parts.append(f"Fields collected so far (JSON): {fields_json}")
        parts.append(f"Latest user message:\n{message.strip()}")
        return "\n\n".join(parts)

    async def _run_turn_llm(self, user_message: str, current_fields: dict) -> tuple[str, bool, dict, str]:
        last_error: Exception | None = None
        for attempt in range(MAX_TURN_RETRIES + 1):
            try:
                raw, provider = await self._llm.generate_text(CHAT_SYSTEM_PROMPT, user_message)
                parsed = parse_chat_turn(raw)
                merged = _merge_fields(current_fields, parsed["collected_fields"])
                return parsed["reply"], parsed["ready_for_plan"], merged, provider
            except (GenerationFailedError, MalformedChatOutputError) as exc:
                last_error = exc
                logger.warning("Chatbot turn generation failed (attempt %s/%s): %s", attempt + 1, MAX_TURN_RETRIES + 1, exc)
                user_message += "\n\nYour previous response was invalid. Return ONLY the JSON object matching the required schema."
        raise GenerationUnavailableError("Chatbot is temporarily unavailable. Please try again shortly.") from last_error

    async def _run_plan_llm(self, thread: ChatbotThread) -> tuple[list[dict], int, str]:
        prompt = PLAN_SYSTEM_PROMPT_TEMPLATE.format(
            project_type=thread.project_type,
            project_goal=thread.project_goal,
            pages_features=thread.pages_features,
            timeline_expectation=thread.timeline_expectation,
        )
        last_error: Exception | None = None
        for attempt in range(MAX_PLAN_RETRIES + 1):
            try:
                raw, provider = await self._llm.generate_text(prompt, "Generate the build plan as JSON.")
                parsed = parse_plan_output(raw, self._settings.chatbot_plan_max_weeks)
                weeks = parsed["weeks"]
                if contains_pricing_language(weeks):
                    logger.warning("Plan output flagged for pricing language on attempt %s, retrying", attempt + 1)
                    if attempt < MAX_PLAN_RETRIES:
                        raw, provider = await self._llm.generate_text(
                            prompt + "\n\nIMPORTANT: your previous attempt mentioned pricing/cost - do not do that.",
                            "Generate the build plan as JSON.",
                        )
                        parsed = parse_plan_output(raw, self._settings.chatbot_plan_max_weeks)
                        weeks = parsed["weeks"]
                    weeks, flagged = scrub_pricing_language(weeks)
                    if flagged:
                        logger.warning("Stripped pricing-language sentences from generated plan as a fallback")
                return weeks, parsed["total_weeks"], provider
            except (GenerationFailedError, MalformedChatOutputError) as exc:
                last_error = exc
                logger.warning("Plan generation failed (attempt %s/%s): %s", attempt + 1, MAX_PLAN_RETRIES + 1, exc)
        raise GenerationUnavailableError("Plan generation is temporarily unavailable. Please try again shortly.") from last_error

    async def _upsert_lead(self, user: User, thread: ChatbotThread) -> bool:
        """Returns True only when a new Lead row was created (first plan
        generation on this thread) - False when an existing one was updated
        (regeneration), so callers can avoid re-notifying the team about a
        "new" lead that isn't actually new."""
        summary = f"Chatbot enquiry: {thread.project_type} - {thread.project_goal}"[:2000]
        if thread.lead_id is None:
            lead = await self._leads.create(
                user_id=user.id,
                full_name=user.full_name,
                email=user.email,
                phone_number=user.phone_number,
                source="chatbot",
                project_type=thread.project_type,
                budget_range=thread.budget_range,
                timeline=thread.timeline_expectation,
                message=summary,
            )
            await self._threads.link_lead(thread, lead.id)
            return True

        lead = await self._leads.get_by_id(thread.lead_id)
        if lead is not None:
            await self._leads.update_fields(
                lead,
                project_type=thread.project_type,
                budget_range=thread.budget_range,
                timeline=thread.timeline_expectation,
                message=summary,
            )
        return False

    async def _reject_if_already_over_limit(self, user_id: uuid.UUID) -> None:
        limit = await self._limits.get_by_user(user_id)
        if limit is None:
            return
        now = datetime.now(timezone.utc)
        window_start = _as_aware(limit.window_start)
        if now - window_start >= RATE_LIMIT_WINDOW:
            return
        if limit.count >= self._settings.chatbot_rate_limit_per_hour:
            self._raise_rate_limited(window_start, now)

    async def _consume_rate_limit(self, user_id: uuid.UUID) -> None:
        now = datetime.now(timezone.utc)
        window_cutoff = now - RATE_LIMIT_WINDOW
        granted = await self._limits.try_consume(
            user_id, limit_per_hour=self._settings.chatbot_rate_limit_per_hour, now=now, window_cutoff=window_cutoff
        )
        if not granted:
            limit = await self._limits.get_by_user(user_id)
            window_start = _as_aware(limit.window_start) if limit else now
            self._raise_rate_limited(window_start, now)

    def _raise_rate_limited(self, window_start: datetime, now: datetime) -> None:
        reset_at = window_start + RATE_LIMIT_WINDOW
        wait_seconds = max(int((reset_at - now).total_seconds()), 1)
        raise RateLimitedError(f"Chatbot message limit reached. Try again in {wait_seconds}s")


def _merge_fields(current: dict, incoming: dict) -> dict:
    """Defensive merge: never let a turn null out a previously known field."""
    merged = dict(current)
    for key in COLLECTED_FIELD_KEYS:
        value = incoming.get(key)
        if value not in (None, "", []):
            merged[key] = value
        elif key not in merged:
            merged[key] = None
    return merged


def _stringify_pages_features(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else None
    return str(value)


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _safe_load_fields(collected_fields_json: str | None) -> dict:
    """We only ever write this column via json.dumps(dict), but a defensive
    fallback here (rather than trusting the shape) means a corrupted row -
    e.g. from a future manual DB edit or migration bug - degrades to "start
    fresh" instead of permanently 500ing every message on that thread."""
    try:
        data = json.loads(collected_fields_json or "{}")
    except json.JSONDecodeError:
        logger.warning("collected_fields_json was not valid JSON, resetting to empty")
        return {}
    return data if isinstance(data, dict) else {}
