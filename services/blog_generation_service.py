import difflib
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
MAX_JSON_RETRIES = 2
MAX_SLUG_RETRIES = 5
# How many historical topics get sent into the prompt's exclusion list. The
# *validation* below still checks the freshly generated topic against the
# full historical set (unbounded) - this cap only bounds prompt token usage.
MAX_EXCLUDED_TOPICS_IN_PROMPT = 40
# How many recent post titles get shown to the model as "already published,
# don't rephrase these". Same rationale as the topic cap - bounds token use
# while the post-generation similarity check below still runs against every
# historical title.
MAX_RECENT_TITLES_IN_PROMPT = 60
# How many recent posts' "##" subheadings are shown to the model as ones not to
# reuse, so every article doesn't end up with the same "Why It Matters" /
# "The Bottom Line" skeleton.
MAX_RECENT_POSTS_FOR_SUBHEADINGS = 15
MAX_SUBHEADINGS_IN_PROMPT = 45
# How many recent posts a new article body is compared against, and how many
# of their opening lines are shown to the model as hooks not to repeat.
MAX_RECENT_POSTS_FOR_CONTENT_CHECK = 60
MAX_RECENT_OPENINGS_IN_PROMPT = 15
# A new article counts as a copy of an existing one when this share of its
# 3-word phrases (Jaccard over word trigrams) also appear in that post, or its
# opening sentence is this close to that post's opening. Two different posts
# in the same niche typically share well under 10% of trigrams; a reworded
# rerun of the same article shares far more.
CONTENT_SHINGLE_OVERLAP = 0.2
OPENING_SIMILARITY_RATIO = 0.75
# How many extra LLM calls are spent rewriting only the headline when the
# article is fine but its title still duplicates an existing one.
MAX_TITLE_REWRITES = 3
# A freshly generated title is treated as a duplicate of an existing one when
# the normalized character-level similarity ratio, the word-set overlap
# (Jaccard), or the overlap relative to the shorter title is at or above these
# thresholds, or when both titles share the same headline stem (the part
# before a ":", "?", or " - "). Topics may repeat; headlines must not - so
# "Website Development Cost in India: 2026 Guide" and "Website Development
# Cost in India: What Drives Pricing" count as the same heading.
TITLE_SIMILARITY_RATIO = 0.7
TITLE_TOKEN_OVERLAP = 0.5
TITLE_TOKEN_CONTAINMENT = 0.8
_TITLE_STEM_SPLIT = re.compile(r"\s*(?::|\?|\s[-–—|]\s)\s*")
_SUBHEADING = re.compile(r"^#{2,3}\s+(.+?)\s*#*\s*$", re.MULTILINE)
# Common filler words stripped before the word-set comparison so near-identical
# titles aren't hidden by, or falsely flagged from, shared connective tissue.
_TITLE_STOPWORDS = frozenset(
    "a an and the to for of your you is are with how why what when in on it its".split()
)

FALLBACK_KEYWORD_POOL = [
    "small business website design",
    "AI solutions for small business",
    "Instagram growth strategy",
    "custom web development services",
    "business process automation",
    "social media marketing for startups",
]

PREFERRED_TOPIC_CLUSTERS = [
    {
        "cluster": "Website Development",
        "service_page": "Website Development",
        "topics": [
            "Website Development Cost in India",
            "React frontend with Python and Java backend development",
            "Website conversion improvements for lead generation",
            "Ecommerce website development cost in India",
        ],
    },
    {
        "cluster": "AI & Automation",
        "service_page": "AI Development",
        "topics": [
            "How Much Does an AI Chatbot Cost in India?",
            "Lead automation for small and medium businesses",
            "WhatsApp automation for sales and support",
            "SEO vs GEO: How to Rank on Google, ChatGPT and AI Search",
        ],
    },
    {
        "cluster": "CRM & Enterprise Software",
        "service_page": "CRM Development",
        "topics": [
            "How Much Does Custom CRM Development Cost in India?",
            "Custom CRM vs Zoho vs Salesforce",
            "SaaS vs custom software for Indian businesses",
            "CRM development for sales, support, and operations teams",
        ],
    },
]

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_MULTI_HYPHEN = re.compile(r"-{2,}")
_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_WORD_PATTERN = re.compile(r"\S+")
_MARKDOWN_NOISE = re.compile(r"[#*_`>]+")
# Strips trailing/inline hashtag clusters (e.g. "#WebDesign #SmallBusiness")
# that the LLM sometimes appends despite the prompt forbidding them. Only
# matches a "#" directly followed by a word - markdown "## Heading" is safe
# since a heading's "#" is followed by a space, not a word character.
_HASHTAG = re.compile(r"(?<!\w)#\w+")
# Matches a markdown heading marker ("#", "##", or "###" + a space) that the
# LLM glued onto the end of the previous sentence instead of putting on its
# own line - e.g. "...processes. ## Key Features". Without a preceding blank
# line, markdown renderers show the literal "##" text instead of a heading,
# which reads to a viewer like a stray hashtag.
_GLUED_HEADING = re.compile(r"(?<!^)(?<!\n)(#{1,3} )")


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

        existing_titles = await self._blog_posts.list_titles()
        recent_titles = existing_titles[:MAX_RECENT_TITLES_IN_PROMPT]
        existing_contents = await self._blog_posts.list_recent_contents(MAX_RECENT_POSTS_FOR_CONTENT_CHECK)
        used_subheadings = _recent_subheadings(existing_contents[:MAX_RECENT_POSTS_FOR_SUBHEADINGS])
        recent_openings = _recent_openings(existing_contents[:MAX_RECENT_OPENINGS_IN_PROMPT])

        async def _draft() -> tuple[dict, str]:
            draft, draft_provider = await self._generate_json(
                excluded_for_prompt, recent_titles, used_subheadings, recent_openings, topic_hint=topic_hint
            )
            return await self._ensure_word_limit(
                draft,
                draft_provider,
                excluded_for_prompt,
                recent_titles,
                used_subheadings,
                recent_openings,
                topic_hint=topic_hint,
            )

        data, provider = await _draft()
        topic_tag = _clean_str(data.get("topic_tag")) or "general"
        title = _clean_str(data.get("title"))

        # A directed topic (topic_hint) may repeat a prior post's subject - the
        # caller asked for it deliberately - so only the topic_tag check is
        # skipped for it. The headline and the article itself must always be
        # new: two posts may share a topic, never a heading or a body.
        def _content_duplicate_of() -> str | None:
            content = _clean_str(data.get("content"))
            match = _closest_content(content, existing_contents)
            if match is not None:
                return f"content closely matches an existing post ({match})"
            return None

        def _duplicate_of() -> str | None:
            """Returns a human-readable reason string if the current draft
            duplicates an existing post (by topic_tag, title, or content),
            else None."""
            if topic_hint is None and topic_tag.strip().lower() in normalized_existing:
                return f"topic_tag {topic_tag!r}"
            match = _closest_title(title, existing_titles)
            if match is not None:
                return f"title {title!r} closely matches existing {match!r}"
            return _content_duplicate_of()

        attempts = 0
        reason = _duplicate_of()
        while reason is not None and attempts < MAX_TOPIC_RETRIES:
            attempts += 1
            logger.warning("Generated post duplicates a prior one (%s), retrying (%s/%s)", reason, attempts, MAX_TOPIC_RETRIES)
            if topic_hint is None:
                excluded_for_prompt = list(dict.fromkeys([*excluded_for_prompt, topic_tag]))
            recent_titles = list(dict.fromkeys([title, *recent_titles]))[:MAX_RECENT_TITLES_IN_PROMPT]
            opening = _opening_line(_clean_str(data.get("content")))
            if opening:
                recent_openings = list(dict.fromkeys([opening, *recent_openings]))[:MAX_RECENT_OPENINGS_IN_PROMPT]
            data, provider = await _draft()
            topic_tag = _clean_str(data.get("topic_tag")) or "general"
            title = _clean_str(data.get("title"))
            reason = _duplicate_of()

        content_reason = _content_duplicate_of()
        if content_reason is not None:
            # Unlike a headline, a repeated body can't be patched in place -
            # skip this run rather than publish a copy of an earlier article.
            raise BlogGenerationError(f"Could not produce an original article after retries: {content_reason}")

        if reason is not None and _closest_title(title, existing_titles) is not None:
            # The article is usable but its headline still repeats an existing
            # one - rewrite just the headline rather than publish a duplicate.
            title = await self._rewrite_unique_title(data, existing_titles)
            data["title"] = title
            data["meta_title"] = ""  # re-derived from the new title in _validate_and_heal
            reason = _duplicate_of()

        if reason is not None:
            # Only the topic_tag still repeats (the title is unique by now).
            # Auto-uniquify the tag so the audit trail stays honest.
            uniquified = f"{topic_tag} ({datetime.now(timezone.utc).date().isoformat()})"
            logger.warning(
                "Publishing a repeated topic under a new headline (%s); topic_tag %r -> %r",
                reason,
                topic_tag,
                uniquified,
            )
            data["topic_tag"] = uniquified

        post_fields = self._validate_and_heal(data)
        post = await self._persist_with_unique_slug(post_fields)
        return post, provider, post.topic_tag or topic_tag

    async def _generate_json(
        self,
        excluded_topics: list[str],
        recent_titles: list[str] | None = None,
        used_subheadings: list[str] | None = None,
        recent_openings: list[str] | None = None,
        strict_word_limit: bool = False,
        topic_hint: str | None = None,
    ) -> tuple[dict, str]:
        system_prompt = _build_system_prompt(
            excluded_topics, recent_titles or [], used_subheadings or [], recent_openings or [], topic_hint=topic_hint
        )
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
        self,
        data: dict,
        provider: str,
        excluded_topics: list[str],
        recent_titles: list[str] | None = None,
        used_subheadings: list[str] | None = None,
        recent_openings: list[str] | None = None,
        topic_hint: str | None = None,
    ) -> tuple[dict, str]:
        content = data.get("content") if isinstance(data.get("content"), str) else ""
        if _word_count(content) <= MAX_WORDS:
            return data, provider
        logger.warning("Generated content exceeded %s words; regenerating once", MAX_WORDS)
        try:
            return await self._generate_json(
                excluded_topics,
                recent_titles,
                used_subheadings,
                recent_openings,
                strict_word_limit=True,
                topic_hint=topic_hint,
            )
        except BlogGenerationError:
            logger.warning("Word-limit regeneration failed; will truncate at a sentence boundary instead")
            return data, provider

    async def _rewrite_unique_title(self, data: dict, existing_titles: list[str]) -> str:
        """Asks the model for a fresh headline for an already-written article,
        rejecting any candidate that still matches an existing title. Raises
        BlogGenerationError rather than let a duplicate heading go live."""
        forbidden = list(existing_titles[:MAX_RECENT_TITLES_IN_PROMPT])
        excerpt = _clean_str(data.get("excerpt")) or _derive_excerpt(_clean_str(data.get("content")))
        for attempt in range(MAX_TITLE_REWRITES):
            system_prompt = f"""You write blog headlines for Webnest Studio, a tech consultancy.
Write ONE new headline (at most 70 characters) for the article summarised below.
It must be clearly different in wording AND structure from every headline in the
forbidden list - do not reuse their opening phrase, do not just add or swap a word
or a year. Try a different angle: a question, a number, a specific scenario, a
mistake to avoid, or a bold claim. No hashtags, no quotes.

Article summary: {excerpt}

Forbidden headlines: {"; ".join(f'"{t}"' for t in forbidden) or "(none)"}

Return ONLY valid JSON: {{"title": "..."}}"""
            raw_text, _ = await self._llm.generate_text(system_prompt, "Write the new headline as JSON.")
            try:
                candidate = _strip_hashtags(_clean_str(_parse_json(raw_text).get("title")))
            except ValueError:
                continue
            if not candidate:
                continue
            match = _closest_title(candidate, existing_titles)
            if match is None:
                logger.info("Rewrote duplicate headline to %r", candidate)
                return candidate
            logger.warning(
                "Rewritten headline %r still matches %r (%s/%s)", candidate, match, attempt + 1, MAX_TITLE_REWRITES
            )
            forbidden = [candidate, *forbidden]
        raise BlogGenerationError("Could not produce a headline distinct from existing posts")

    def _validate_and_heal(self, data: dict) -> dict:
        title = _strip_hashtags(_clean_str(data.get("title")))
        content = _normalize_headings(_strip_hashtags(_clean_str(data.get("content"))))
        if not title:
            raise BlogGenerationError("LLM response was missing a non-empty 'title'")
        if not content:
            raise BlogGenerationError("LLM response was missing a non-empty 'content'")

        words = _word_count(content)
        if words > MAX_WORDS:
            logger.warning("Truncating content at a sentence boundary: %s words -> <= %s", words, MAX_WORDS)
            content = _truncate_to_word_limit(content, MAX_WORDS)
        word_count = _word_count(content)

        excerpt = _strip_hashtags(_clean_str(data.get("excerpt"))) or _derive_excerpt(content)

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


def _build_system_prompt(
    excluded_topics: list[str],
    recent_titles: list[str],
    used_subheadings: list[str] | None = None,
    recent_openings: list[str] | None = None,
    topic_hint: str | None = None,
) -> str:
    openings_str = "; ".join(f'"{o}"' for o in recent_openings) if recent_openings else "(none yet)"
    excluded_str = "; ".join(excluded_topics) if excluded_topics else "(none yet)"
    recent_titles_str = "; ".join(f'"{t}"' for t in recent_titles) if recent_titles else "(none yet)"
    subheadings_str = "; ".join(f'"{h}"' for h in used_subheadings) if used_subheadings else "(none yet)"
    if topic_hint:
        topic_instruction = f"""Write specifically about the following topic (you may narrow or angle it,
but stay on this subject - do not substitute a different topic):
{topic_hint}"""
    else:
        topic_instruction = f"""Pick ONE fresh, specific topic from the preferred topic clusters below unless
the exact subject has already been covered. Prioritize commercial-intent posts
that attract Indian founders, marketing heads, operations teams, and B2B buyers
who are close to requesting a quote.

{_format_preferred_topic_clusters()}

If every preferred topic is already covered, choose a closely related long-tail
angle that still supports one of these service pages."""
    return f"""You are writing a blog post for Webnest Studio, a tech consultancy offering
web development, AI/ML solutions, and social media growth for small and
medium businesses.

{topic_instruction}

Rules:
- The article body ("content") must be at most 300 words, markdown format,
  with a punchy hook opening (a bold claim, surprising stat, or relatable pain
  point - not a generic "In today's world..." line), 2-3 short "##"
  subheadings, at least one concrete example, number, or actionable tip so it
  reads as genuinely useful rather than generic filler, and a closing call-to-
  action toward Webnest Studio's services.
- Write in an engaging, confident, conversational tone - vary sentence length,
  avoid clichés and corporate buzzwords, and make it something a small
  business owner would actually enjoy reading.
- Do NOT include any hashtags (e.g. "#SmallBusiness", "#WebDesign") anywhere
  in the title, excerpt, or content - this is a blog article, not a social
  media caption. Use the dedicated "keywords" field for SEO terms instead.
- Include 4-6 realistic SEO keywords/phrases naturally within the content
  (not stuffed) - prefer specific, moderately-searched, long-tail phrases a
  small business owner would actually type into Google, over generic single
  words, so the post can realistically rank and drive traffic.
- When writing cost/comparison posts, include India-specific buying context
  and practical ranges or decision factors without making unverifiable promises.
- Keep the article aligned with the selected cluster's service page so it can
  work as part of a topic cluster, not as an unrelated standalone post.
- meta_title: at most 60 characters, includes a primary keyword near the start.
- meta_description: 150-160 characters, includes a keyword and a soft call to action.
- slug: lowercase, hyphen-separated, url-safe, derived from the title.
- topic_tag: a short 2-5 word label for this topic, distinct in wording from the title.
- Do NOT reuse or closely rephrase any of these previously covered topics: {excluded_str}
- The topic clusters above are SUBJECTS, not headlines - never copy a cluster
  topic verbatim as the title.
- The title must be unique: even when the subject overlaps an earlier post, the
  headline must differ in wording AND structure. Do NOT reuse or lightly reword
  any of these already-published titles, do not start with the same lead phrase
  (e.g. the words before a ":" or "?"), and do not just add a year, "Guide", or
  one swapped word: {recent_titles_str}
- Vary the headline format - rotate between a question, a number/list, a
  specific scenario or persona, a mistake to avoid, a comparison, or a bold
  claim - and avoid the format the recent titles above use most.
- The "##" subheadings must be specific to this article's content (e.g.
  "What a ₹40,000 Website Actually Includes"), never generic labels like
  "Why It Matters", "The Bottom Line", "Conclusion", "Key Benefits", or
  "Getting Started". Do NOT reuse any of these recently used subheadings:
  {subheadings_str}
- The whole article must be original, not a remix of an earlier post: use a
  fresh angle, a new concrete example or scenario, different numbers, and a
  different structure. Do NOT open the way any of these recent posts opened
  (no same hook, stat, or first sentence pattern): {openings_str}

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


def _format_preferred_topic_clusters() -> str:
    lines: list[str] = []
    for cluster in PREFERRED_TOPIC_CLUSTERS:
        topics = "; ".join(cluster["topics"])
        lines.append(f'- {cluster["cluster"]} -> {cluster["service_page"]} service page: {topics}')
    return "\n".join(lines)


def _parse_json(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    # The prompt says "no markdown fences", but real LLM responses sometimes
    # ignore that instruction - stripping defensively is cheap and avoids
    # treating an otherwise-valid response as a hard failure.
    text = _FENCE_PATTERN.sub("", text).strip()
    try:
        # strict=False tolerates literal control characters (raw newlines/tabs)
        # inside JSON string values - LLMs frequently emit these in multi-
        # paragraph "content" fields instead of the escaped \n the spec requires.
        data = json.loads(text, strict=False)
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


def _strip_hashtags(text: str) -> str:
    if not text:
        return text
    cleaned = _HASHTAG.sub("", text)
    # Collapse any double spaces / stray blank lines left behind by removal.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", cleaned)
    return cleaned.strip()


def _normalize_headings(content: str) -> str:
    return _GLUED_HEADING.sub(r"\n\n\1", content)


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


def _normalize_title(title: str) -> str:
    return _NON_ALNUM.sub(" ", (title or "").lower()).strip()


def _title_word_set(title: str) -> set[str]:
    return {w for w in _normalize_title(title).split() if w and w not in _TITLE_STOPWORDS}


def _title_stem(title: str) -> str:
    """The headline's lead phrase - the part before a ":", "?", or " - " - so
    "X: A Guide" and "X: What to Know" are recognised as the same heading.
    Empty when the title has no such separator or the stem is too short to be
    meaningful."""
    parts = _TITLE_STEM_SPLIT.split(title or "", maxsplit=1)
    if len(parts) < 2:
        return ""
    stem_words = _title_word_set(parts[0])
    return " ".join(sorted(stem_words)) if len(stem_words) >= 3 else ""


def _closest_title(candidate: str, existing: list[str]) -> str | None:
    """Returns the first existing title the candidate duplicates - a high
    character-level similarity ratio, a high content-word overlap, one title's
    words mostly contained in the other's, or an identical headline stem -
    or None if the candidate reads as genuinely new."""
    cand_norm = _normalize_title(candidate)
    if not cand_norm:
        return None
    cand_words = _title_word_set(candidate)
    cand_stem = _title_stem(candidate)
    for other in existing:
        other_norm = _normalize_title(other)
        if not other_norm:
            continue
        if cand_norm == other_norm:
            return other
        if difflib.SequenceMatcher(None, cand_norm, other_norm).ratio() >= TITLE_SIMILARITY_RATIO:
            return other
        if cand_stem and cand_stem == _title_stem(other):
            return other
        other_words = _title_word_set(other)
        if cand_words and other_words:
            shared = len(cand_words & other_words)
            if shared / len(cand_words | other_words) >= TITLE_TOKEN_OVERLAP:
                return other
            if min(len(cand_words), len(other_words)) >= 3 and shared / min(
                len(cand_words), len(other_words)
            ) >= TITLE_TOKEN_CONTAINMENT:
                return other
    return None


def _extract_subheadings(content: str) -> list[str]:
    return [m.group(1).strip() for m in _SUBHEADING.finditer(content or "") if m.group(1).strip()]


def _plain_words(content: str) -> list[str]:
    return _normalize_title(_MARKDOWN_NOISE.sub(" ", content or "")).split()


def _shingles(content: str) -> set[tuple[str, ...]]:
    words = _plain_words(content)
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def _opening_line(content: str) -> str:
    """The article's first real sentence (its hook), skipping headings."""
    for line in (content or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            first = _SENTENCE_BOUNDARY.split(_MARKDOWN_NOISE.sub("", line).strip(), maxsplit=1)[0]
            return _truncate(first, 160)
    return ""


def _recent_openings(contents: list[str]) -> list[str]:
    return list(dict.fromkeys(o for o in (_opening_line(c) for c in contents) if o))


def _closest_content(candidate: str, existing: list[str]) -> str | None:
    """Returns a short description of the first existing post the candidate
    article copies - heavy 3-word-phrase overlap or a near-identical opening
    hook - or None if the article reads as original."""
    cand_shingles = _shingles(candidate)
    cand_opening = _normalize_title(_opening_line(candidate))
    for other in existing:
        other_opening_raw = _opening_line(other)
        other_opening = _normalize_title(other_opening_raw)
        if cand_opening and other_opening and (
            difflib.SequenceMatcher(None, cand_opening, other_opening).ratio() >= OPENING_SIMILARITY_RATIO
        ):
            return f"same opening as {other_opening_raw!r}"
        other_shingles = _shingles(other)
        if cand_shingles and other_shingles:
            overlap = len(cand_shingles & other_shingles) / len(cand_shingles | other_shingles)
            if overlap >= CONTENT_SHINGLE_OVERLAP:
                return f"{overlap:.0%} phrase overlap with the post opening {other_opening_raw!r}"
    return None


def _recent_subheadings(contents: list[str]) -> list[str]:
    """Distinct subheadings from recent posts (case-insensitive), newest first."""
    seen: dict[str, str] = {}
    for content in contents:
        for heading in _extract_subheadings(content):
            seen.setdefault(heading.lower(), heading)
    return list(seen.values())[:MAX_SUBHEADINGS_IN_PROMPT]


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
