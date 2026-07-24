import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.exceptions import (
    ContentRejectedError,
    ForbiddenError,
    GenerationUnavailableError,
    InvalidPromptError,
    NotFoundError,
    RateLimitedError,
)
from database.generation_limit_persistence import GenerationLimitPersistence
from database.generation_persistence import GenerationPersistence
from database.models import Generation
from services.html_pipeline import MalformedOutputError, finalize_generated_html
from services.llm_provider import GenerationFailedError, LLMProvider

INITIAL_SYSTEM_PROMPT = """You are a web page generator. Output ONLY a single complete HTML file
starting exactly with "<!DOCTYPE html>". Do not include any explanation,
markdown code fences, or commentary before or after the HTML.

Requirements for every generated page:
- Single self-contained file: inline <style> and <script> only, no external
  build steps or dependencies (Google Fonts and icon CDNs are allowed)
- Mobile-responsive layout
- Semantic HTML5 elements
- Use CSS gradients/placeholder styling instead of real images unless the
  user explicitly asks for image placeholders
- Clean, modern visual design appropriate to the page's stated purpose"""

REFINEMENT_SYSTEM_PROMPT = """You are editing an existing HTML page based on a user's requested change.
You will be given the current full HTML and a refinement instruction.
Apply ONLY the requested change, preserving everything else about the
page's structure and content unless the instruction implies otherwise.
Output the COMPLETE updated HTML file - not a diff, not a partial snippet -
following the same format rules as before (starts with <!DOCTYPE html>,
no commentary, no code fences)."""

# Minimal off-topic/abuse guardrail. Not a substitute for provider-side safety
# filtering, just a fast pre-LLM-call reject for obviously disallowed requests.
BLOCKED_KEYWORDS = (
    "child sexual", "csam", "how to make a bomb", "build a bomb",
    "credit card number generator", "malware", "ransomware", "keylogger",
    "phishing kit", "ddos attack",
)

_INTER_TAG_WHITESPACE = re.compile(r">\s+<")
_MULTI_WHITESPACE = re.compile(r"[ \t\n\r\f]+")

RATE_LIMIT_WINDOW = timedelta(hours=1)


class GenerationService:
    """Orchestrates the AI Page Builder pipeline: validation, rate limiting,
    LLM fallback call, HTML post-processing, and persistence."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._generations = GenerationPersistence(session)
        self._limits = GenerationLimitPersistence(session)
        self._settings = settings
        self._llm = LLMProvider(settings)

    async def generate(self, user_id: uuid.UUID, prompt: str) -> tuple[Generation, str]:
        self._validate_length(prompt, self._settings.max_prompt_length, label="prompt")
        self._check_content(prompt)
        await self._reject_if_already_over_limit(user_id)

        html, provider = await self._run_llm(INITIAL_SYSTEM_PROMPT, prompt.strip())
        await self._consume_rate_limit(user_id)

        title = prompt.strip()[:50]
        generation = await self._generations.create_thread(
            user_id=user_id, initial_prompt=prompt.strip(), html=html, title=title, provider_used=provider
        )
        return generation, provider

    async def refine(self, user_id: uuid.UUID, generation_id: uuid.UUID, refinement: str) -> tuple[Generation, str]:
        self._validate_length(refinement, self._settings.max_refinement_length, label="refinement")
        self._check_content(refinement)

        generation = await self._generations.get_by_id(generation_id)
        if generation is None:
            raise NotFoundError("Generation not found")
        if generation.user_id != user_id:
            raise ForbiddenError("This generation does not belong to you")

        await self._reject_if_already_over_limit(user_id)

        context_html = _minify_for_context(generation.latest_html)
        user_message = f"Current HTML:\n{context_html}\n\nRefinement instruction:\n{refinement.strip()}"
        html, provider = await self._run_llm(REFINEMENT_SYSTEM_PROMPT, user_message)
        await self._consume_rate_limit(user_id)

        generation = await self._generations.append_refinement(
            generation, refinement_text=refinement.strip(), html=html, provider_used=provider
        )
        return generation, provider

    async def get_history_list(self, user_id: uuid.UUID) -> list[tuple[Generation, int]]:
        return await self._generations.list_by_user(user_id)

    async def get_thread_detail(self, user_id: uuid.UUID, generation_id: uuid.UUID) -> Generation:
        generation = await self._generations.get_with_messages(generation_id)
        if generation is None:
            raise NotFoundError("Generation not found")
        if generation.user_id != user_id:
            raise ForbiddenError("This generation does not belong to you")
        return generation

    async def get_limit_status(self, user_id: uuid.UUID) -> tuple[int, int, datetime]:
        limit = await self._limits.get_by_user(user_id)
        now = datetime.now(timezone.utc)
        limit_per_hour = self._settings.rate_limit_per_hour

        if limit is None:
            return limit_per_hour, limit_per_hour, now + RATE_LIMIT_WINDOW

        window_start = _as_aware(limit.window_start)
        if now - window_start >= RATE_LIMIT_WINDOW:
            return limit_per_hour, limit_per_hour, now + RATE_LIMIT_WINDOW

        remaining = max(0, limit_per_hour - limit.count)
        return remaining, limit_per_hour, window_start + RATE_LIMIT_WINDOW

    def _validate_length(self, text: str, max_length: int, label: str) -> None:
        if not text or not text.strip():
            raise InvalidPromptError(f"The {label} must not be empty")
        if len(text) > max_length:
            raise InvalidPromptError(f"The {label} must be at most {max_length} characters")

    def _check_content(self, text: str) -> None:
        lowered = text.lower()
        for keyword in BLOCKED_KEYWORDS:
            if keyword in lowered:
                raise ContentRejectedError("This request was rejected by the content filter")

    async def _reject_if_already_over_limit(self, user_id: uuid.UUID) -> None:
        """Read-only fail-fast check, so an obviously-exhausted user gets a 429
        immediately instead of first paying for an LLM call that would just be
        thrown away. Not authoritative by itself - _consume_rate_limit() after
        the LLM call is what actually enforces the cap atomically."""
        limit = await self._limits.get_by_user(user_id)
        if limit is None:
            return
        now = datetime.now(timezone.utc)
        window_start = _as_aware(limit.window_start)
        if now - window_start >= RATE_LIMIT_WINDOW:
            return
        if limit.count >= self._settings.rate_limit_per_hour:
            self._raise_rate_limited(window_start, now)

    async def _consume_rate_limit(self, user_id: uuid.UUID) -> None:
        """Atomically books one slot in the current hourly window - called only
        after a successful LLM call, so a provider failure never burns a user's
        quota. The INSERT ... ON CONFLICT DO UPDATE ... WHERE in try_consume()
        makes the check-and-increment a single statement, so two concurrent
        requests from the same user can't both read the same pre-increment
        count and both squeeze past the cap."""
        now = datetime.now(timezone.utc)
        window_cutoff = now - RATE_LIMIT_WINDOW
        granted = await self._limits.try_consume(
            user_id, limit_per_hour=self._settings.rate_limit_per_hour, now=now, window_cutoff=window_cutoff
        )
        if not granted:
            limit = await self._limits.get_by_user(user_id)
            window_start = _as_aware(limit.window_start) if limit else now
            self._raise_rate_limited(window_start, now)

    def _raise_rate_limited(self, window_start: datetime, now: datetime) -> None:
        reset_at = window_start + RATE_LIMIT_WINDOW
        wait_seconds = max(int((reset_at - now).total_seconds()), 1)
        raise RateLimitedError(f"Generation limit reached. Try again in {wait_seconds}s")

    async def _run_llm(self, system_prompt: str, user_message: str) -> tuple[str, str]:
        try:
            raw_html, provider = await self._llm.generate_html(system_prompt, user_message)
        except GenerationFailedError as exc:
            raise GenerationUnavailableError(
                "Generation is temporarily unavailable. Please try again shortly."
            ) from exc

        try:
            return finalize_generated_html(raw_html), provider
        except MalformedOutputError as exc:
            raise GenerationUnavailableError(
                "The generated page could not be validated. Please try again."
            ) from exc


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _minify_for_context(html: str) -> str:
    collapsed = _INTER_TAG_WHITESPACE.sub("><", html)
    return _MULTI_WHITESPACE.sub(" ", collapsed).strip()
