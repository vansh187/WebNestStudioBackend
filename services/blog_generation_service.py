import difflib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.constants import BLOG_MIN_RECOMMENDED_WORDS
from core.exceptions import ConflictError
from database.blog_generation_log_persistence import BlogGenerationLogPersistence
from database.blog_persistence import BlogPersistence
from database.models import BlogGenerationLog, BlogPost
from services.deploy_hook_service import DeployHookService
from services.email_service import EmailService
from services.llm_provider import GenerationFailedError, LLMProvider

logger = logging.getLogger("webnest.blog_generation")

# Target article length. Below MIN_WORDS a post is too thin to rank; the
# generator regenerates once when a draft lands outside the range. A draft is
# only cut down when it exceeds HARD_MAX_WORDS, since trimming removes the
# closing FAQ / call to action.
MIN_WORDS = BLOG_MIN_RECOMMENDED_WORDS
MAX_WORDS = 1200
HARD_MAX_WORDS = 1400
# How many times the model is asked for a different topic when its draft
# matches an existing post. Each retry is a full-length article, so this is
# kept low; if a draft still matches after the retries, the matching post is
# refreshed in place instead of a new one being created.
MAX_TOPIC_RETRIES = 2
MAX_JSON_RETRIES = 2
# The scheduler's cron fires once a day at a fixed time, while the previous
# run's timestamp lands a little after that time - so "exactly N days ago" is
# always a few minutes short. Without this tolerance every interval silently
# stretches by one day.
RECURRING_SCHEDULE_TOLERANCE = timedelta(hours=2)
# How many historical topics get sent into the prompt's exclusion list. The
# *validation* below still checks the freshly generated draft against every
# existing post - this cap only bounds prompt token usage.
MAX_EXCLUDED_TOPICS_IN_PROMPT = 40
# How many recent post titles get shown to the model as "already published,
# don't rephrase these". Same rationale as the topic cap.
MAX_RECENT_TITLES_IN_PROMPT = 60
# How many recent posts' "##" subheadings are shown to the model as ones not to
# reuse, so every article doesn't end up with the same "Why It Matters" /
# "The Bottom Line" skeleton.
MAX_RECENT_POSTS_FOR_SUBHEADINGS = 15
MAX_SUBHEADINGS_IN_PROMPT = 45
# How many recent opening lines are shown to the model as hooks not to repeat.
MAX_RECENT_OPENINGS_IN_PROMPT = 15
# A new article counts as a copy of an existing one when this share of its
# 3-word phrases (Jaccard over word trigrams) also appear in that post, or its
# opening sentence is this close to that post's opening. Two different posts
# in the same niche typically share well under 10% of trigrams; a reworded
# rerun of the same article shares far more.
CONTENT_SHINGLE_OVERLAP = 0.2
OPENING_SIMILARITY_RATIO = 0.75
# A freshly generated title is treated as a duplicate of an existing one when
# the normalized character-level similarity ratio, the word-set overlap
# (Jaccard), or the overlap relative to the shorter title is at or above these
# thresholds, or when both titles share the same headline stem (the part
# before a ":", "?", or " - ") - so "Website Development Cost in India: 2026
# Guide" and "Website Development Cost in India: What Drives Pricing" count as
# the same heading.
TITLE_SIMILARITY_RATIO = 0.7
TITLE_TOKEN_OVERLAP = 0.5
TITLE_TOKEN_CONTAINMENT = 0.8
# How much two headlines must share before an identical primary keyword is
# taken to mean "the same article" (see _find_matching_post).
KEYWORD_MATCH_TITLE_OVERLAP = 0.3
_TITLE_STEM_SPLIT = re.compile(r"\s*(?::|\?|\s[-–—|]\s)\s*")
_SUBHEADING = re.compile(r"^##\s+(.+?)\s*#*\s*$", re.MULTILINE)
_FAQ_HEADING = re.compile(r"^(faqs?|frequently asked)", re.IGNORECASE)
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
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
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
_GLUED_HEADING = re.compile(r"(?<!^)(?<!\n)(?<!#)(#{1,3} )")


class BlogGenerationError(Exception):
    """Raised when a generation attempt could not produce a publishable post
    even after every self-healing fallback. Callers must catch this (and
    GenerationFailedError) so a bad run is logged rather than left unhandled."""


class BlogGenerationService:
    """Orchestrates automated blog post generation: LLM call with Gemini/Groq
    fallback, SEO-field validation with graceful self-healing, a duplicate
    guard that refreshes the existing post on a topic instead of publishing a
    second one, persistence, and the frontend rebuild trigger.
    Designed so a single bad run (malformed JSON, off-length content, missing
    SEO fields, repeated topic) degrades gracefully instead of throwing - the
    only exceptions that ever leave generate_and_publish are
    BlogGenerationError (logged failure, no post) when every fallback was
    exhausted."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._blog_posts = BlogPersistence(session)
        self._logs = BlogGenerationLogPersistence(session)
        self._settings = settings
        self._llm = LLMProvider(settings)
        self._email = EmailService(settings)
        self._deploy_hook = DeployHookService(settings)
        # Human-readable result of the last successful generate_and_publish
        # call, for the admin "Generate now" response.
        self.last_outcome_detail = ""

    async def list_recent_logs(self, limit: int = 20) -> list[BlogGenerationLog]:
        return await self._logs.list_recent(limit=limit)

    # ---- Idempotency guards (used by the scheduler, bypassed by manual admin trigger) ----

    async def should_run_recurring(self) -> bool:
        # A run that refreshes an existing post leaves published_at untouched,
        # so the last successful generation is consulted as well as the newest
        # post - otherwise a refresh would be followed by another run the very
        # next day.
        latest = await self._blog_posts.get_latest()
        last_run_candidates = [
            latest.published_at if latest is not None else None,
            await self._logs.latest_success_at(),
        ]
        last_runs = [_as_aware(value) for value in last_run_candidates if value is not None]
        if not last_runs:
            return True
        elapsed = datetime.now(timezone.utc) - max(last_runs)
        interval = timedelta(days=self._settings.blog_generation_interval_days)
        return elapsed >= interval - RECURRING_SCHEDULE_TOLERANCE

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
            post, provider, topic_tag, outcome = await self._run_pipeline(topic_hint=topic_hint)
        except GenerationFailedError as exc:
            logger.critical("Blog generation failed: both LLM providers are unavailable (%s)", exc)
            await self._log_failure(trigger_source, topic_tag=None, error=str(exc))
            await self._alert_failure(str(exc))
            raise BlogGenerationError(str(exc)) from exc
        except BlogGenerationError as exc:
            logger.error("Blog generation failed: %s", exc)
            await self._log_failure(trigger_source, topic_tag=None, error=str(exc))
            raise
        except Exception as exc:
            # Anything unforeseen (the database being unreachable, a reply
            # shaped in a way nothing anticipated) is still a logged, reported
            # failure - callers only ever have BlogGenerationError to handle.
            logger.critical("Unexpected error in blog generation", exc_info=True)
            await self._log_failure(trigger_source, topic_tag=None, error=f"Unexpected error: {type(exc).__name__}")
            raise BlogGenerationError(f"Unexpected error: {type(exc).__name__}") from exc

        self.last_outcome_detail = _OUTCOME_DETAILS[outcome]
        try:
            await self._logs.create(
                success=True,
                llm_used=provider,
                topic_tag=topic_tag,
                blog_post_id=post.id,
                trigger_source=trigger_source,
            )
        except Exception:
            # The post itself is already saved at this point - a failure to
            # write the audit-log row must not make an otherwise-successful
            # run look like a failed one to the caller.
            logger.error("Blog post %s was saved but its generation log row could not be written", post.id, exc_info=True)
        logger.info(
            "Blog generation outcome=%s for %r (slug=%s, provider=%s, topic=%s, words=%s)",
            outcome,
            post.title,
            post.slug,
            provider,
            topic_tag,
            post.word_count,
        )
        # The frontend pre-renders blog pages at build time, so nothing is
        # visible (or re-crawlable) until it rebuilds. trigger() never raises.
        await self._deploy_hook.trigger(f"blog post {outcome}: {post.slug}")

        if outcome == "created":
            try:
                await self._email.send_blog_published_notification(post.title or "Untitled post", post.slug)
            except Exception:
                # The post is already live either way - a notification-email
                # failure must never be reported as a failed publish.
                logger.error("Blog post %s was published but the publicity notification email failed to send", post.id, exc_info=True)

        return post

    # ---- Pipeline internals ----

    async def _run_pipeline(self, topic_hint: str | None = None) -> tuple[BlogPost, str, str, str]:
        posts = await self._blog_posts.list_all()
        excluded_for_prompt = list(dict.fromkeys(p.topic_tag for p in posts if p.topic_tag))[
            :MAX_EXCLUDED_TOPICS_IN_PROMPT
        ]
        recent_titles = [p.title for p in posts if p.title][:MAX_RECENT_TITLES_IN_PROMPT]
        contents = [p.content for p in posts if p.content]
        used_subheadings = _recent_subheadings(contents[:MAX_RECENT_POSTS_FOR_SUBHEADINGS])
        recent_openings = _recent_openings(contents[:MAX_RECENT_OPENINGS_IN_PROMPT])

        async def _draft() -> tuple[dict, str]:
            draft, draft_provider = await self._generate_json(
                excluded_for_prompt, recent_titles, used_subheadings, recent_openings, topic_hint=topic_hint
            )
            return await self._ensure_word_range(
                draft,
                draft_provider,
                excluded_for_prompt,
                recent_titles,
                used_subheadings,
                recent_openings,
                topic_hint=topic_hint,
            )

        data, provider = await _draft()
        match = _find_matching_post(data, posts)

        # A directed topic (topic_hint) is the caller deliberately asking for
        # this subject, so no retry for a different one - a match goes straight
        # to refreshing the existing post on that subject.
        attempts = 0
        while match is not None and topic_hint is None and attempts < MAX_TOPIC_RETRIES:
            attempts += 1
            matched_post, reason = match
            logger.warning(
                "Draft duplicates existing post %s (%s), asking for a different topic (%s/%s)",
                matched_post.slug,
                reason,
                attempts,
                MAX_TOPIC_RETRIES,
            )
            draft_topic = _clean_str(data.get("topic_tag"))
            draft_title = _clean_str(data.get("title"))
            draft_opening = _opening_line(_clean_str(data.get("content")))
            excluded_for_prompt = list(dict.fromkeys(t for t in [*excluded_for_prompt, draft_topic] if t))
            recent_titles = list(dict.fromkeys(t for t in [draft_title, *recent_titles] if t))[:MAX_RECENT_TITLES_IN_PROMPT]
            if draft_opening:
                recent_openings = list(dict.fromkeys([draft_opening, *recent_openings]))[:MAX_RECENT_OPENINGS_IN_PROMPT]
            data, provider = await _draft()
            match = _find_matching_post(data, posts)

        fields = self._validate_and_heal(data)
        matched_post = match[0] if match is not None else None
        if matched_post is None:
            # Same slug means the same headline. Reuse that post rather than
            # ever minting a "-2" variant of an existing URL.
            matched_post = next((p for p in posts if p.slug == fields["slug"]), None)

        if matched_post is not None:
            if not matched_post.is_published:
                # Whether an unpublished post comes back is the owner's call,
                # never the generator's - so it is neither republished nor
                # shadowed by a second post on the same subject. The run is
                # logged as failed and tried again on the next cron fire.
                raise BlogGenerationError(
                    f"The draft covers the same ground as the unpublished post {matched_post.slug!r}. "
                    "Nothing was published - republish that post by hand, or generate again for a different topic."
                )
            post = await self._refresh_existing(matched_post, fields, posts)
            return post, provider, post.topic_tag or fields["topic_tag"], "refreshed"

        _make_seo_fields_unique(fields, posts)
        try:
            post = await self._blog_posts.create(**fields)
        except ConflictError as exc:
            # Only reachable through a race with a concurrent writer taking
            # the same slug between list_all() and here. Fail this run (it is
            # logged and retried on the next cron fire) rather than invent a
            # suffixed slug.
            raise BlogGenerationError(f"Could not persist blog post: {exc}") from exc
        return post, provider, post.topic_tag or fields["topic_tag"], "created"

    async def _refresh_existing(self, post: BlogPost, fields: dict, posts: list[BlogPost]) -> BlogPost:
        """Updates the existing published post on this topic with the new
        draft instead of creating a second post. The slug never changes - its
        URL may already be indexed."""
        existing_words = post.word_count or _word_count(post.content or "")
        if existing_words > fields["word_count"]:
            # Never replace a post with a thinner draft - it may have been
            # expanded by hand. Raised (and so logged as a failed run) rather
            # than reported as a success: a run that changed nothing must not
            # use up the week's publishing slot.
            raise BlogGenerationError(
                f"The draft covers the same ground as the existing post {post.slug!r}, which is longer "
                f"({existing_words} words vs {fields['word_count']}). Nothing was changed."
            )

        others = [p for p in posts if p.id != post.id]
        # Take the new headline only if no *other* post already has one like it.
        title = fields["title"]
        if post.title and _closest_title(title, [p.title for p in others if p.title]) is not None:
            title = post.title
        candidate = {**fields, "title": title}
        _make_seo_fields_unique(candidate, others)
        updates = {
            key: candidate[key]
            for key in ("title", "excerpt", "content", "meta_title", "meta_description", "keywords", "tags", "word_count")
        }
        if not post.topic_tag:
            updates["topic_tag"] = fields["topic_tag"]

        logger.info("Refreshing existing post %s instead of creating a duplicate", post.slug)
        try:
            return await self._blog_posts.update(post, **updates)
        except ConflictError as exc:
            raise BlogGenerationError(f"Could not update blog post {post.slug!r}: {exc}") from exc

    async def _generate_json(
        self,
        excluded_topics: list[str],
        recent_titles: list[str] | None = None,
        used_subheadings: list[str] | None = None,
        recent_openings: list[str] | None = None,
        length_note: str = "",
        topic_hint: str | None = None,
    ) -> tuple[dict, str]:
        system_prompt = _build_system_prompt(
            excluded_topics, recent_titles or [], used_subheadings or [], recent_openings or [], topic_hint=topic_hint
        )
        user_message = "Generate today's blog post as JSON."
        if length_note:
            user_message += f" IMPORTANT: {length_note}"

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

    async def _ensure_word_range(
        self,
        data: dict,
        provider: str,
        excluded_topics: list[str],
        recent_titles: list[str] | None = None,
        used_subheadings: list[str] | None = None,
        recent_openings: list[str] | None = None,
        topic_hint: str | None = None,
    ) -> tuple[dict, str]:
        """Regenerates once when the draft is outside MIN_WORDS..MAX_WORDS and
        keeps whichever draft is closer to the range."""
        words = _word_count(_draft_content(data))
        if MIN_WORDS <= words <= MAX_WORDS:
            return data, provider
        if words < MIN_WORDS:
            note = (
                f"the previous attempt was only {words} words - the 'content' field MUST be between "
                f"{MIN_WORDS} and {MAX_WORDS} words. Develop every section in more depth."
            )
        else:
            note = (
                f"the previous attempt was {words} words - the 'content' field MUST be between "
                f"{MIN_WORDS} and {MAX_WORDS} words. Tighten it."
            )
        logger.warning("Generated content was %s words (target %s-%s); regenerating once", words, MIN_WORDS, MAX_WORDS)
        try:
            retry, retry_provider = await self._generate_json(
                excluded_topics, recent_titles, used_subheadings, recent_openings, length_note=note, topic_hint=topic_hint
            )
        except BlogGenerationError:
            logger.warning("Word-range regeneration failed; keeping the original draft")
            return data, provider
        if _distance_from_range(_word_count(_draft_content(retry))) <= _distance_from_range(words):
            return retry, retry_provider
        return data, provider

    def _validate_and_heal(self, data: dict) -> dict:
        title = _strip_hashtags(_clean_str(data.get("title")))
        content = _normalize_headings(_strip_hashtags(_clean_str(data.get("content"))))
        if not title:
            raise BlogGenerationError("LLM response was missing a non-empty 'title'")
        if not content:
            raise BlogGenerationError("LLM response was missing a non-empty 'content'")

        words = _word_count(content)
        if words > HARD_MAX_WORDS:
            logger.warning("Trimming content at a paragraph boundary: %s words -> <= %s", words, MAX_WORDS)
            content = _truncate_to_word_limit(content, MAX_WORDS)
        word_count = _word_count(content)
        if word_count < MIN_WORDS:
            # Not blocked - the admin API flags short posts for expansion.
            logger.warning("Publishing a short post: %s words (recommended minimum %s)", word_count, MIN_WORDS)

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
            "published_at": datetime.now(timezone.utc),
            # Posts are permanent - expires_at is only ever set by hand.
            "expires_at": None,
        }

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


_OUTCOME_DETAILS = {
    "created": "Blog post generated and published",
    "refreshed": "An existing post already covers this topic - it was updated instead of creating a duplicate",
}


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
- The article body ("content") must be {MIN_WORDS}-{MAX_WORDS} words of markdown
  (aim for about 1,000 - anything under {MIN_WORDS} is rejected), in this
  structure:
  1. An introduction of 2-3 short paragraphs with no heading, opening on a
     hook (a relatable pain point, a sharp question, or a bold claim - not a
     generic "In today's world..." line).
  2. 3 to 5 sections, each under its own "##" subheading, each giving
     practical, specific guidance a business owner can act on.
  3. A "## Frequently Asked Questions" section with 3 short questions, each
     as a "###" heading followed by a 1-3 sentence answer.
  4. One closing paragraph with a single call to action toward Webnest
     Studio's services. This is the ONLY call to action in the article.
- Put a blank line before and after every heading.
- Do NOT invent facts. No made-up statistics, percentages, survey results,
  prices, rupee amounts, client names, case studies, testimonials, or
  timelines presented as fact. When discussing cost, explain what drives it
  and how to compare options instead of quoting figures. Illustrative
  examples must be clearly hypothetical ("a clinic that takes bookings by
  phone...") and never name a real or invented client.
- Write in an engaging, confident, conversational tone - vary sentence length,
  avoid clichés and corporate buzzwords, and make it something a small
  business owner would actually enjoy reading.
- Do NOT include any hashtags (e.g. "#SmallBusiness", "#WebDesign") anywhere
  in the title, excerpt, or content - this is a blog article, not a social
  media caption. Use the dedicated "keywords" field for SEO terms instead.
- Include 4-6 realistic SEO keywords/phrases naturally within the content
  (not stuffed) - prefer specific, moderately-searched, long-tail phrases a
  small business owner would actually type into Google, over generic single
  words. List the single primary keyword FIRST in the "keywords" array.
- When writing cost/comparison posts, include India-specific buying context
  and decision factors without making unverifiable promises.
- Keep the article aligned with the selected cluster's service page so it can
  work as part of a topic cluster, not as an unrelated standalone post.
- meta_title: at most 60 characters, includes the primary keyword near the
  start, and is unique to this post.
- meta_description: 150-160 characters, includes a keyword and a soft call to
  action, and is unique to this post.
- slug: lowercase, hyphen-separated, url-safe, derived from the title.
- topic_tag: a short 2-5 word label for this topic, distinct in wording from the title.
- Do NOT reuse or closely rephrase any of these previously covered topics: {excluded_str}
- The topic clusters above are SUBJECTS, not headlines - never copy a cluster
  topic verbatim as the title.
- The title must be unique. Do NOT reuse or lightly reword any of these
  already-published titles, do not start with the same lead phrase (e.g. the
  words before a ":" or "?"), and do not just add a year, "Guide", or one
  swapped word: {recent_titles_str}
- Vary the headline format - rotate between a question, a list, a specific
  scenario or persona, a mistake to avoid, a comparison, or a bold claim -
  and avoid the format the recent titles above use most.
- The "##" subheadings (other than the FAQ heading) must be specific to this
  article's content, never generic labels like "Why It Matters", "The Bottom
  Line", "Conclusion", "Key Benefits", or "Getting Started". Do NOT reuse any
  of these recently used subheadings: {subheadings_str}
- The whole article must be original, not a remix of an earlier post: use a
  fresh angle, new scenarios, and a different structure. Do NOT open the way
  any of these recent posts opened (no same hook or first sentence pattern):
  {openings_str}

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


def _draft_content(data: dict) -> str:
    return data.get("content") if isinstance(data.get("content"), str) else ""


def _distance_from_range(words: int) -> int:
    if words < MIN_WORDS:
        return MIN_WORDS - words
    if words > MAX_WORDS:
        return words - MAX_WORDS
    return 0


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
    """Drops whole trailing paragraphs/sections until the article fits, so the
    markdown structure (headings, blank lines) survives the cut."""
    if _word_count(content) <= max_words:
        return content

    kept: list[str] = []
    count = 0
    for block in _PARAGRAPH_BREAK.split(content.strip()):
        block_words = _word_count(block)
        if kept and count + block_words > max_words:
            break
        kept.append(block)
        count += block_words
    while len(kept) > 1 and kept[-1].lstrip().startswith("#"):
        kept.pop()  # a heading left with nothing under it
    truncated = "\n\n".join(kept).strip()
    if _word_count(truncated) <= max_words:
        return truncated

    # A single oversized block - fall back to cutting at a sentence boundary.
    sentences: list[str] = []
    count = 0
    for sentence in _SENTENCE_BOUNDARY.split(truncated):
        sentence_words = _word_count(sentence)
        if sentences and count + sentence_words > max_words:
            break
        sentences.append(sentence)
        count += sentence_words
    return " ".join(sentences).strip()


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
                return f"{overlap:.0%} phrase overlap"
    return None


def _normalize_phrase(value: object) -> str:
    return _normalize_title(value) if isinstance(value, str) else ""


def _primary_keyword(keywords: object) -> str:
    if isinstance(keywords, list) and keywords:
        return _normalize_phrase(keywords[0])
    return ""


def _title_overlap(first: str, second: str) -> float:
    """Share of content words two headlines have in common (Jaccard)."""
    first_words, second_words = _title_word_set(first), _title_word_set(second)
    if not first_words or not second_words:
        return 0.0
    return len(first_words & second_words) / len(first_words | second_words)


def _find_matching_post(data: dict, posts: list[BlogPost]) -> tuple[BlogPost, str] | None:
    """The existing post this draft duplicates, with the reason - same
    topic_tag, a closely matching title, the same primary keyword on an
    overlapping title, or a copied body - or None if the draft covers new
    ground. Published posts are preferred over unpublished ones when several
    match."""
    title = _clean_str(data.get("title"))
    topic_tag = _normalize_phrase(data.get("topic_tag"))
    primary_keyword = _primary_keyword(data.get("keywords"))
    content = _draft_content(data)

    for post in sorted(posts, key=lambda p: not p.is_published):
        if topic_tag and topic_tag == _normalize_phrase(post.topic_tag):
            return post, f"same topic_tag {post.topic_tag!r}"
        if post.title and _closest_title(title, [post.title]) is not None:
            return post, f"title {title!r} closely matches {post.title!r}"
        # A shared primary keyword alone is not proof of the same article -
        # two different posts can target one phrase - so it only counts when
        # the headlines overlap as well. Otherwise a new draft could overwrite
        # an unrelated older post in place.
        if (
            primary_keyword
            and primary_keyword == _primary_keyword(post.keywords)
            and _title_overlap(title, post.title or "") >= KEYWORD_MATCH_TITLE_OVERLAP
        ):
            return post, f"same primary keyword {primary_keyword!r} and an overlapping title"
        if post.content:
            content_reason = _closest_content(content, [post.content])
            if content_reason is not None:
                return post, f"content: {content_reason}"
    return None


def _make_seo_fields_unique(fields: dict, others: list[BlogPost]) -> None:
    """Guarantees meta_title and meta_description differ from every other
    post's, deriving replacements from this post's own (already unique) title
    and excerpt. Mutates `fields`."""
    taken_titles = {_normalize_phrase(p.meta_title) for p in others} - {""}
    taken_descriptions = {_normalize_phrase(p.meta_description) for p in others} - {""}

    meta_title = fields.get("meta_title") or ""
    if not meta_title or _normalize_phrase(meta_title) in taken_titles:
        for candidate in (_truncate(fields["title"], 60), _truncate(fields["title"], 70), fields["title"]):
            if _normalize_phrase(candidate) not in taken_titles:
                meta_title = candidate
                break
        else:
            raise BlogGenerationError("Could not produce a meta_title distinct from existing posts")
    fields["meta_title"] = meta_title

    meta_description = fields.get("meta_description") or ""
    if not meta_description or _normalize_phrase(meta_description) in taken_descriptions:
        for candidate in (
            _truncate(fields.get("excerpt") or "", 155),
            _truncate(_derive_excerpt(fields.get("content") or ""), 155),
            _truncate(f'{fields["title"]} - {fields.get("excerpt") or ""}', 155),
        ):
            if candidate and _normalize_phrase(candidate) not in taken_descriptions:
                meta_description = candidate
                break
        else:
            raise BlogGenerationError("Could not produce a meta_description distinct from existing posts")
    fields["meta_description"] = meta_description


def _extract_subheadings(content: str) -> list[str]:
    return [
        m.group(1).strip()
        for m in _SUBHEADING.finditer(content or "")
        if m.group(1).strip() and not _FAQ_HEADING.match(m.group(1).strip())
    ]


def _recent_subheadings(contents: list[str]) -> list[str]:
    """Distinct subheadings from recent posts (case-insensitive), newest first."""
    seen: dict[str, str] = {}
    for content in contents:
        for heading in _extract_subheadings(content):
            seen.setdefault(heading.lower(), heading)
    return list(seen.values())[:MAX_SUBHEADINGS_IN_PROMPT]


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
