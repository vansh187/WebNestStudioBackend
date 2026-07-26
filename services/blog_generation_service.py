import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.exceptions import ConflictError
from database.blog_generation_log_persistence import BlogGenerationLogPersistence
from database.blog_persistence import BlogPersistence
from database.models import BlogGenerationLog, BlogPost
from services.email_service import EmailService
from services.llm_provider import GenerationFailedError, LLMProvider

logger = logging.getLogger("webnest.blog_generation")

MAX_WORDS = 300
MAX_TOPIC_RETRIES = 3
MAX_JSON_RETRIES = 1
MAX_SLUG_RETRIES = 5
# How many historical topics get sent into the prompt's exclusion list. The
# *validation* below still checks the freshly generated topic against the
# full historical set (unbounded) - this cap only bounds prompt token usage.
MAX_EXCLUDED_TOPICS_IN_PROMPT = 40

FALLBACK_KEYWORD_POOL = [
    "small business website design",
    "AI solutions for small business",
    "Instagram growth strategy",
    "custom web development services",
    "business process automation",
    "social media marketing for startups",
]

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_MULTI_HYPHEN = re.compile(r"-{2,}")
_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_WORD_PATTERN = re.compile(r"\S+")
_MARKDOWN_NOISE = re.compile(r"[#*_`>]+")


class BlogGenerationError(Exception):
    """Raised when a generation attempt could not produce a publishable post
    even after every self-healing fallback. Callers must catch this (and
    GenerationFailedError) so a bad run is logged rather than left unhandled."""


class BlogGenerationService:
    """Orchestrates automated blog post generation: topic-uniqueness
    enforcement, LLM call with Gemini/Groq fallback, SEO-field validation
    with graceful self-healing, slug-collision handling, and persistence.
    Designed so a single bad run (malformed JSON, over-length content,
    missing SEO fields, repeated topic) degrades gracefully instead of
    throwing - the only exceptions that ever leave generate_and_publish are
    BlogGenerationError (logged failure, no post) when every fallback was
    exhausted."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._blog_posts = BlogPersistence(session)
        self._logs = BlogGenerationLogPersistence(session)
        self._settings = settings
        self._llm = LLMProvider(settings)
        self._email = EmailService(settings)

    async def list_recent_logs(self, limit: int = 20) -> list[BlogGenerationLog]:
        return await self._logs.list_recent(limit=limit)

    # ---- Idempotency guards (used by the scheduler, bypassed by manual admin trigger) ----

    async def should_run_recurring(self) -> bool:
        latest = await self._blog_posts.get_latest()
        if latest is None or latest.published_at is None:
            return True
        elapsed = datetime.now(timezone.utc) - _as_aware(latest.published_at)
        return elapsed >= timedelta(days=self._settings.blog_generation_interval_days)

    async def already_published_on_ist_date(self, ist_now: datetime) -> bool:
        day_start_ist = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end_ist = day_start_ist + timedelta(days=1)
        return await self._blog_posts.has_published_on_date(
            day_start_ist.astimezone(timezone.utc), day_end_ist.astimezone(timezone.utc)
        )

    async def has_launch_post_already_run(self) -> bool:
        return await self._logs.has_successful_run("scheduled-launch")

    # ---- Main entry point ----

    async def generate_and_publish(self, trigger_source: str, topic_hint: str | None = None) -> BlogPost:
        try:
            post, provider, topic_tag = await self._run_pipeline(topic_hint=topic_hint)
        except GenerationFailedError as exc:
            logger.critical("Blog generation failed: both LLM providers are unavailable (%s)", exc)
            await self._log_failure(trigger_source, topic_tag=None, error=str(exc))
            await self._alert_failure(str(exc))
            raise BlogGenerationError(str(exc)) from exc
        except BlogGenerationError as exc:
            logger.error("Blog generation failed: %s", exc)
            await self._log_failure(trigger_source, topic_tag=None, error=str(exc))
            raise

        try:
            await self._logs.create(
                success=True,
                llm_used=provider,
                topic_tag=topic_tag,
                blog_post_id=post.id,
                trigger_source=trigger_source,
            )
        except Exception:
            # The post itself is already published at this point - a failure
            # to write the audit-log row must not make an otherwise-successful
            # run look like a failed one to the caller.
            logger.error("Blog post %s was published but its generation log row could not be written", post.id, exc_info=True)
        logger.info(
            "Published blog post %r (slug=%s, provider=%s, topic=%s, words=%s)",
            post.title,
            post.slug,
            provider,
            topic_tag,
            post.word_count,
        )

        try:
            await self._email.send_blog_published_notification(post.title or "Untitled post", post.slug)
        except Exception:
            # The post is already live either way - a notification-email
            # failure must never be reported as a failed publish.
            logger.error("Blog post %s was published but the publicity notification email failed to send", post.id, exc_info=True)

        return post

    # ---- Pipeline internals ----

    async def _run_pipeline(self, topic_hint: str | None = None) -> tuple[BlogPost, str, str]:
        existing_topics = await self._blog_posts.list_topic_tags()
        normalized_existing = {t.strip().lower() for t in existing_topics if t}
        excluded_for_prompt = existing_topics[:MAX_EXCLUDED_TOPICS_IN_PROMPT]

        data, provider = await self._generate_json(excluded_for_prompt, topic_hint=topic_hint)
        data, provider = await self._ensure_word_limit(data, provider, excluded_for_prompt, topic_hint=topic_hint)
        topic_tag = _clean_str(data.get("topic_tag")) or "general"

        # A directed topic (topic_hint) is trusted as-is even if its topic_tag
        # happens to collide with a prior post - the caller asked for this
        # exact subject deliberately, so the uniqueness retry loop (which
        # exists to stop the model repeating itself when picking freely)
        # doesn't apply here.
        attempts = 0
        while topic_hint is None and topic_tag.strip().lower() in normalized_existing and attempts < MAX_TOPIC_RETRIES:
            attempts += 1
            logger.warning(
                "Generated topic_tag %r collides with an existing topic, retrying (%s/%s)",
                topic_tag,
                attempts,
                MAX_TOPIC_RETRIES,
            )
            excluded_for_prompt = list(dict.fromkeys([*excluded_for_prompt, topic_tag]))
            data, provider = await self._generate_json(excluded_for_prompt)
            data, provider = await self._ensure_word_limit(data, provider, excluded_for_prompt)
            topic_tag = _clean_str(data.get("topic_tag")) or "general"

        if topic_hint is None and topic_tag.strip().lower() in normalized_existing:
            uniquified = f"{topic_tag} ({datetime.now(timezone.utc).date().isoformat()})"
            logger.warning("Auto-uniquifying topic_tag %r -> %r after exhausting retries", topic_tag, uniquified)
            data["topic_tag"] = uniquified

        post_fields = self._validate_and_heal(data)
        post = await self._persist_with_unique_slug(post_fields)
        return post, provider, post.topic_tag or topic_tag

    async def _generate_json(
        self, excluded_topics: list[str], strict_word_limit: bool = False, topic_hint: str | None = None
    ) -> tuple[dict, str]:
        system_prompt = _build_system_prompt(excluded_topics, topic_hint=topic_hint)
        user_message = "Generate today's blog post as JSON."
        if strict_word_limit:
            user_message += (
                " IMPORTANT: the previous attempt exceeded the word limit - the "
                "'content' field MUST be 300 words or fewer. Count carefully before responding."
            )

        last_error: Exception | None = None
        for attempt in range(MAX_JSON_RETRIES + 1):
            raw_text, provider = await self._llm.generate_text(system_prompt, user_message)
            try:
                return _parse_json(raw_text), provider
            except ValueError as exc:
                last_error = exc
                logger.warning(
                    "Blog LLM response was not valid JSON (attempt %s/%s): %s", attempt + 1, MAX_JSON_RETRIES + 1, exc
                )
        raise BlogGenerationError(f"LLM did not return valid JSON after {MAX_JSON_RETRIES + 1} attempt(s): {last_error}")

    async def _ensure_word_limit(
        self, data: dict, provider: str, excluded_topics: list[str], topic_hint: str | None = None
    ) -> tuple[dict, str]:
        content = data.get("content") if isinstance(data.get("content"), str) else ""
        if _word_count(content) <= MAX_WORDS:
            return data, provider
        logger.warning("Generated content exceeded %s words; regenerating once", MAX_WORDS)
        try:
            return await self._generate_json(excluded_topics, strict_word_limit=True, topic_hint=topic_hint)
        except BlogGenerationError:
            logger.warning("Word-limit regeneration failed; will truncate at a sentence boundary instead")
            return data, provider

    def _validate_and_heal(self, data: dict) -> dict:
        title = _clean_str(data.get("title"))
        content = _clean_str(data.get("content"))
        if not title:
            raise BlogGenerationError("LLM response was missing a non-empty 'title'")
        if not content:
            raise BlogGenerationError("LLM response was missing a non-empty 'content'")

        words = _word_count(content)
        if words > MAX_WORDS:
            logger.warning("Truncating content at a sentence boundary: %s words -> <= %s", words, MAX_WORDS)
            content = _truncate_to_word_limit(content, MAX_WORDS)
        word_count = _word_count(content)

        excerpt = _clean_str(data.get("excerpt")) or _derive_excerpt(content)

        meta_title = _clean_str(data.get("meta_title"))
        if not meta_title or len(meta_title) > 70:
            meta_title = _truncate(title, 60)

        meta_description = _clean_str(data.get("meta_description"))
        if not meta_description or not (80 <= len(meta_description) <= 200):
            meta_description = _truncate(excerpt or content, 155)

        topic_tag = _clean_str(data.get("topic_tag")) or _truncate(title, 60)

        keywords_raw = data.get("keywords")
        keywords: list[str] = []
        if isinstance(keywords_raw, list):
            keywords = [_clean_str(k) for k in keywords_raw if _clean_str(k)]
        if not (3 <= len(keywords) <= 8):
            keywords = _fallback_keywords(topic_tag)

        now = datetime.now(timezone.utc)
        return {
            "title": title,
            "slug": _slugify(title),
            "excerpt": _truncate(excerpt, 300),
            "content": content,
            "meta_title": meta_title,
            "meta_description": meta_description,
            "keywords": keywords,
            "tags": keywords[:3],
            "topic_tag": topic_tag,
            "word_count": word_count,
            "is_published": True,
            "published_at": now,
            "expires_at": now + timedelta(days=self._settings.blog_post_lifetime_days),
        }

    async def _persist_with_unique_slug(self, fields: dict) -> BlogPost:
        base_slug = fields["slug"]
        slug = base_slug
        for attempt in range(MAX_SLUG_RETRIES):
            existing = await self._blog_posts.get_by_slug(slug)
            if existing is None:
                break
            slug = f"{base_slug}-{attempt + 2}"
        else:
            slug = f"{base_slug}-{uuid.uuid4().hex[:8]}"
        fields["slug"] = slug
        try:
            return await self._blog_posts.create(**fields)
        except ConflictError:
            # Extremely unlikely race with the check-loop above (e.g. a
            # concurrent worker), but must never bubble up as an unhandled 500
            # from a background job - force a guaranteed-unique slug and retry once.
            fields["slug"] = f"{base_slug}-{uuid.uuid4().hex[:8]}"
            try:
                return await self._blog_posts.create(**fields)
            except ConflictError as exc:
                # A raw ConflictError escaping here would skip generate_and_publish's
                # except clauses entirely (it only matches BlogGenerationError/
                # GenerationFailedError), silently breaking the "every attempt is
                # logged" guarantee. Wrap it so this run is always logged and
                # reported the same way as any other exhausted-fallback failure.
                raise BlogGenerationError(
                    f"Could not persist blog post: slug collision could not be resolved ({exc})"
                ) from exc

    async def _log_failure(self, trigger_source: str, topic_tag: str | None, error: str) -> None:
        try:
            await self._logs.create(
                success=False,
                llm_used=None,
                topic_tag=topic_tag,
                blog_post_id=None,
                error_message=error[:2000],
                trigger_source=trigger_source,
            )
        except Exception:
            logger.error("Could not write a blog_generation_logs failure row", exc_info=True)

    async def _alert_failure(self, error: str) -> None:
        if not self._settings.team_notification_email:
            logger.critical(
                "Blog generation failed and TEAM_NOTIFICATION_EMAIL is not configured - no alert sent. Error: %s", error
            )
            return
        try:
            await self._email.send_alert_email(
                subject="Webnest Studio - automated blog generation failed",
                body=(
                    "Both configured LLM providers failed while generating the scheduled "
                    f"blog post. No post was published for this run.\n\nError: {error}"
                ),
            )
        except Exception:
            logger.error("Failed to send the blog-generation failure alert email", exc_info=True)


def _build_system_prompt(excluded_topics: list[str], topic_hint: str | None = None) -> str:
    excluded_str = "; ".join(excluded_topics) if excluded_topics else "(none yet)"
    if topic_hint:
        topic_instruction = f"""Write specifically about the following topic (you may narrow or angle it,
but stay on this subject - do not substitute a different topic):
{topic_hint}"""
    else:
        topic_instruction = """Pick ONE fresh, specific topic: either (a) a current trend in web
development, AI, or social media relevant to small businesses right now, or
(b) a common real-world business problem and how a consultancy like Webnest
Studio would solve it."""
    return f"""You are writing a blog post for Webnest Studio, a tech consultancy offering
web development, AI/ML solutions, and social media growth for small and
medium businesses.

{topic_instruction}

Rules:
- The article body ("content") must be at most 300 words, markdown format,
  with a hook opening, 2-3 short "##" subheadings, and a closing call-to-
  action toward Webnest Studio's services.
- Include 4-6 realistic SEO keywords/phrases naturally within the content
  (not stuffed) - prefer specific, moderately-searched, long-tail phrases a
  small business owner would actually type into Google, over generic single
  words, so the post can realistically rank and drive traffic.
- meta_title: at most 60 characters, includes a primary keyword near the start.
- meta_description: 150-160 characters, includes a keyword and a soft call to action.
- slug: lowercase, hyphen-separated, url-safe, derived from the title.
- topic_tag: a short 2-5 word label for this topic, distinct in wording from the title.
- Do NOT reuse or closely rephrase any of these previously covered topics: {excluded_str}

Return ONLY valid JSON, no markdown code fences, no extra commentary:
{{
  "title": "...",
  "slug": "...",
  "excerpt": "...",
  "content": "...",
  "meta_title": "...",
  "meta_description": "...",
  "keywords": ["...", "..."],
  "topic_tag": "..."
}}"""


def _parse_json(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    # The prompt says "no markdown fences", but real LLM responses sometimes
    # ignore that instruction - stripping defensively is cheap and avoids
    # treating an otherwise-valid response as a hard failure.
    text = _FENCE_PATTERN.sub("", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON root was not an object")
    return data


def _clean_str(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _truncate(text: str, max_length: int) -> str:
    text = text.strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def _derive_excerpt(content: str) -> str:
    plain = _MARKDOWN_NOISE.sub("", content).strip()
    return _truncate(plain, 160)


def _slugify(title: str) -> str:
    slug = _NON_ALNUM.sub("-", title.lower()).strip("-")
    slug = _MULTI_HYPHEN.sub("-", slug)
    slug = slug[:200].strip("-")
    if not slug or not SLUG_PATTERN.match(slug):
        slug = f"post-{uuid.uuid4().hex[:8]}"
    return slug


def _fallback_keywords(topic_tag: str) -> list[str]:
    seed = list(FALLBACK_KEYWORD_POOL)
    if topic_tag and topic_tag.lower() not in {k.lower() for k in seed}:
        seed = [topic_tag] + seed
    return seed[:5]


def _word_count(text: str) -> int:
    return len(_WORD_PATTERN.findall(text or ""))


def _truncate_to_word_limit(content: str, max_words: int) -> str:
    words = _WORD_PATTERN.findall(content)
    if len(words) <= max_words:
        return content

    sentences = _SENTENCE_BOUNDARY.split(content)
    kept: list[str] = []
    count = 0
    for sentence in sentences:
        sentence_words = len(_WORD_PATTERN.findall(sentence))
        if count + sentence_words > max_words and kept:
            break
        kept.append(sentence)
        count += sentence_words
        if count >= max_words:
            break

    truncated = " ".join(kept).strip()
    return truncated if truncated else " ".join(words[:max_words])


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
