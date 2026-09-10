import uuid
from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.session import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    full_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    phone_number: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, default="client")
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")


class OtpVerification(Base):
    __tablename__ = "otp_verifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    otp_code: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    full_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text, index=True)
    phone_number: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, index=True)
    project_type: Mapped[str | None] = mapped_column(Text)
    budget_range: Mapped[str | None] = mapped_column(Text)
    timeline: Mapped[str | None] = mapped_column(Text)
    preferred_date: Mapped[datetime | None] = mapped_column(Date)
    preferred_time_slot: Mapped[str | None] = mapped_column(Text)
    resource_name: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    brief_file_url: Mapped[str | None] = mapped_column(Text)
    utm_source: Mapped[str | None] = mapped_column(Text)
    utm_medium: Mapped[str | None] = mapped_column(Text)
    utm_campaign: Mapped[str | None] = mapped_column(Text)
    referrer_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="new", index=True)
    consent_given: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class NewsletterSubscriber(Base):
    __tablename__ = "newsletter_subscribers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    subscribed: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Service(Base):
    __tablename__ = "services"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    title: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, unique=True, index=True)
    short_description: Mapped[str | None] = mapped_column(Text)
    full_description: Mapped[str | None] = mapped_column(Text)
    icon_url: Mapped[str | None] = mapped_column(Text)
    tech_tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    display_order: Mapped[int | None] = mapped_column(Integer)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PortfolioItem(Base):
    __tablename__ = "portfolio_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    title: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, unique=True, index=True)
    category: Mapped[str | None] = mapped_column(Text, index=True)
    short_description: Mapped[str | None] = mapped_column(Text)
    full_description: Mapped[str | None] = mapped_column(Text)
    tech_stack: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    cover_image_url: Mapped[str | None] = mapped_column(Text)
    gallery_urls: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    result_metrics: Mapped[str | None] = mapped_column(Text)
    client_name: Mapped[str | None] = mapped_column(Text)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    display_order: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Testimonial(Base):
    __tablename__ = "testimonials"
    __table_args__ = (CheckConstraint("rating BETWEEN 1 AND 5", name="testimonials_rating_check"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    client_name: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    quote: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int | None] = mapped_column(Integer)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Faq(Base):
    __tablename__ = "faqs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    question: Mapped[str | None] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    display_order: Mapped[int | None] = mapped_column(Integer)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BlogPost(Base):
    __tablename__ = "blog_posts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    title: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, unique=True, index=True)
    excerpt: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    cover_image_url: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # SEO metadata + auto-expiry, populated by the automated blog generation
    # pipeline. Nullable so existing/manually-created posts stay valid without
    # a backfill. expires_at drives both the public read filter and the daily
    # archive sweep - every post (including the 2 original static ones) is
    # subject to the same 15-day lifetime, there is no "permanent" post.
    meta_title: Mapped[str | None] = mapped_column(Text)
    meta_description: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    topic_tag: Mapped[str | None] = mapped_column(Text, index=True)
    word_count: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class BlogGenerationLog(Base):
    """Audit trail for every automated generation attempt (success or failure),
    queryable via the admin API so operators can see what happened without
    needing shell access to Render's logs."""

    __tablename__ = "blog_generation_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    llm_used: Mapped[str | None] = mapped_column(Text)
    topic_tag: Mapped[str | None] = mapped_column(Text)
    blog_post_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("blog_posts.id", ondelete="SET NULL"))
    error_message: Mapped[str | None] = mapped_column(Text)
    trigger_source: Mapped[str] = mapped_column(Text, nullable=False)


class Generation(Base):
    __tablename__ = "generations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    initial_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    latest_html: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    messages: Mapped[list["GenerationMessage"]] = relationship(back_populates="generation", cascade="all, delete-orphan", order_by="GenerationMessage.created_at")


class GenerationMessage(Base):
    __tablename__ = "generation_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    generation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("generations.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    html_snapshot: Mapped[str | None] = mapped_column(Text)
    provider_used: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    generation: Mapped["Generation"] = relationship(back_populates="messages")


class GenerationLimit(Base):
    __tablename__ = "generation_limits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ProjectStatus(Base):
    __tablename__ = "project_status"
    __table_args__ = (CheckConstraint("percent_complete BETWEEN 0 AND 100", name="project_status_percent_check"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    client_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_name: Mapped[str | None] = mapped_column(Text)
    phase: Mapped[str | None] = mapped_column(Text)
    percent_complete: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ChatbotThread(Base):
    """One project-enquiry conversation: accumulates structured requirements
    (project type/goal/pages/budget/timeline) turn-by-turn until a plan can
    be generated. collected_fields_json is the exact state replayed to the
    LLM as context each turn; the individual columns mirror it for querying."""

    __tablename__ = "chatbot_threads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="in_progress")
    collected_fields_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    project_type: Mapped[str | None] = mapped_column(Text)
    project_goal: Mapped[str | None] = mapped_column(Text)
    pages_features: Mapped[str | None] = mapped_column(Text)
    budget_range: Mapped[str | None] = mapped_column(Text)
    timeline_expectation: Mapped[str | None] = mapped_column(Text)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    messages: Mapped[list["ChatbotMessage"]] = relationship(back_populates="thread", cascade="all, delete-orphan", order_by="ChatbotMessage.created_at")


class ChatbotMessage(Base):
    __tablename__ = "chatbot_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chatbot_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    thread: Mapped["ChatbotThread"] = relationship(back_populates="messages")


class ChatbotLimit(Base):
    __tablename__ = "chatbot_limits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ChatbotPlan(Base):
    """One row per generated plan document (append-only - regenerating a plan
    on the same thread inserts a new row rather than overwriting, the latest
    one is what's served). plan_weeks_json is the single source rendered into
    both the PDF and the inline HTML summary so they can never disagree."""

    __tablename__ = "chatbot_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chatbot_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_type: Mapped[str] = mapped_column(Text, nullable=False)
    total_weeks: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_weeks_json: Mapped[str] = mapped_column(Text, nullable=False)
    provider_used: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ChatThread(Base):
    """The single conversation the chat widget reads/writes - owns the
    unified transcript (ChatMessage) and, once its first real message
    resolves an intent, locks into either "page_builder" (delegating to the
    existing Generation/GenerationService) or "enquiry" (delegating to
    ChatbotThread/ChatbotService) for the rest of its life."""

    __tablename__ = "chat_threads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False, default="undecided")
    generation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("generations.id", ondelete="SET NULL"))
    enquiry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("chatbot_threads.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    messages: Mapped[list["ChatMessage"]] = relationship(back_populates="thread", cascade="all, delete-orphan", order_by="ChatMessage.created_at")


class ChatMessage(Base):
    """The transcript the widget actually reads - every user/assistant turn
    regardless of which mode handled it, so the frontend never has to merge
    separate GenerationMessage/ChatbotMessage timelines itself."""

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    chat_thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    thread: Mapped["ChatThread"] = relationship(back_populates="messages")


class CodingProject(Base):
    __tablename__ = "coding_projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    language: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    files: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    stdin: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    share_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True)


class CodingShare(Base):
    __tablename__ = "coding_shares"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    share_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("coding_projects.id", ondelete="SET NULL"))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    files: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    stdin: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stdout: Mapped[str] = mapped_column(Text, nullable=False, default="")
    author_display_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Conversation(Base):
    """A user-to-user chat room: a named 'group' (many members) or an
    unnamed 'direct' 1:1. Distinct from ChatThread / ChatbotThread, which are
    human<->LLM conversations. last_message_at / last_message_preview are
    denormalised copies bumped on every send so the conversation list can be
    ordered and previewed without touching the messages table."""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("type in ('group','direct')", name="ck_conversation_type"),
        # One conversation per project (spec section 11). Partial so ordinary
        # groups and DMs, which leave project_id NULL, are unconstrained. Kept
        # in sync with migrations/005_messaging_project_link.sql so a fresh
        # create_all database has the same idempotency backstop as a migrated
        # one.
        Index(
            "uq_conversations_project",
            "project_id",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_message_preview: Mapped[str | None] = mapped_column(Text)
    # Set when this group is a project team room (spec section 11); NULL for
    # every ordinary group and DM. Deliberately NOT an ORM ForeignKey: the
    # `projects` table (project-progress-backend-spec.md section 2) is not part
    # of this service's metadata, and Base.metadata.create_all would fail to
    # resolve it at startup. The DB-level FK to projects(id) ON DELETE SET NULL
    # and the one-per-project partial unique index are added by
    # migrations/005_messaging_project_link.sql once that table exists.
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    participants: Mapped[list["ConversationParticipant"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at"
    )


class ConversationParticipant(Base):
    """Membership row. A member who leaves keeps their row with left_at set so
    their past messages stay attributable and re-adding them is a resurrection,
    not a fresh join. Authorisation always filters on left_at IS NULL."""

    __tablename__ = "conversation_participants"
    __table_args__ = (
        UniqueConstraint("conversation_id", "user_id", name="uq_participant"),
        CheckConstraint("role in ('owner','admin','member')", name="ck_participant_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="member")
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped["Conversation"] = relationship(back_populates="participants")
    user: Mapped["User"] = relationship(foreign_keys=[user_id])


class Message(Base):
    """One message in a Conversation. body NULL => attachment-only. Deletion is
    soft (is_deleted): the row stays so replies pointing at it still resolve,
    but body/attachments/reactions are cleared on the way out."""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conv_created", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str | None] = mapped_column(Text)
    reply_to_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    attachments: Mapped[list[dict] | None] = mapped_column(JSONB)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
    sender: Mapped["User"] = relationship(foreign_keys=[sender_id])
    reactions: Mapped[list["MessageReaction"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )
    reply_to: Mapped["Message | None"] = relationship(
        remote_side="Message.id", foreign_keys=[reply_to_message_id]
    )


class MessageReaction(Base):
    __tablename__ = "message_reactions"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", "emoji", name="uq_reaction"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    emoji: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    message: Mapped["Message"] = relationship(back_populates="reactions")
    user: Mapped["User"] = relationship(foreign_keys=[user_id])


class CodingExecution(Base):
    __tablename__ = "coding_executions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True)
    ip_hash: Mapped[str | None] = mapped_column(Text, index=True)
    language: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    time_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# =========================================================================== #
# Webnest CodeLab - Phase 1 learning platform (browser-first execution; the
# backend never runs visitor code). See CODELAB_LEARNING_API.md.
# =========================================================================== #
class CodelabTrack(Base):
    __tablename__ = "codelab_tracks"

    # Short stable identifier used as the ?track= filter value, e.g. "python".
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    runner: Mapped[str] = mapped_column(Text, nullable=False, server_default="iframe")  # pyodide | iframe
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="published")  # draft | published
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CodelabProblem(Base):
    __tablename__ = "codelab_problems"
    __table_args__ = (
        CheckConstraint("difficulty in ('easy','medium','hard')", name="ck_codelab_problem_difficulty"),
        CheckConstraint("status in ('draft','published','archived')", name="ck_codelab_problem_status"),
        Index("ix_codelab_problems_track_status", "track", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    track: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    difficulty: Mapped[str] = mapped_column(Text, nullable=False, server_default="easy")
    statement: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    constraints: Mapped[list | None] = mapped_column(JSONB)          # list[str]
    hints: Mapped[list | None] = mapped_column(JSONB)                # list[str]
    starter_files: Mapped[list | None] = mapped_column(JSONB)        # list[{name,language,content}]
    examples: Mapped[list | None] = mapped_column(JSONB)            # list[{input,expected_output,explanation}]
    topics: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    points: Mapped[int] = mapped_column(Integer, nullable=False, server_default="20")
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="10")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    test_cases: Mapped[list["CodelabTestCase"]] = relationship(
        back_populates="problem", cascade="all, delete-orphan", order_by="CodelabTestCase.display_order"
    )


class CodelabTestCase(Base):
    __tablename__ = "codelab_test_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("codelab_problems.id", ondelete="CASCADE"), nullable=False, index=True
    )
    input: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    expected_output: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    weight: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    problem: Mapped["CodelabProblem"] = relationship(back_populates="test_cases")


class CodelabSubmission(Base):
    __tablename__ = "codelab_submissions"
    __table_args__ = (Index("ix_codelab_submissions_user_created", "user_id", "submitted_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("codelab_problems.id", ondelete="CASCADE"), nullable=False, index=True
    )
    language: Mapped[str] = mapped_column(Text, nullable=False)
    files: Mapped[list | None] = mapped_column(JSONB)               # list[{name,language,content}]
    result: Mapped[dict | None] = mapped_column(JSONB)             # raw client-reported result blob (audit)
    status: Mapped[str] = mapped_column(Text, nullable=False)       # passed|failed|runtime_error|timeout|manual_review
    score: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    passed_tests: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_tests: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    runtime_ms: Mapped[int | None] = mapped_column(Integer)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    problem: Mapped["CodelabProblem"] = relationship()


class CodelabProblemProgress(Base):
    __tablename__ = "codelab_problem_progress"
    __table_args__ = (UniqueConstraint("user_id", "problem_id", name="uq_codelab_problem_progress"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("codelab_problems.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="not_started")  # not_started|in_progress|solved
    best_score: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    solved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CodelabUserStats(Base):
    __tablename__ = "codelab_user_stats"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    xp: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    current_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    best_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    solved_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_activity_date: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CodelabAchievement(Base):
    __tablename__ = "codelab_achievements"
    __table_args__ = (UniqueConstraint("user_id", "achievement_key", name="uq_codelab_achievement"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    achievement_key: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    earned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Course(Base):
    __tablename__ = "courses"
    __table_args__ = (CheckConstraint("status in ('draft','published','archived')", name="ck_course_status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False, server_default="beginner")
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft", index=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    modules: Mapped[list["CourseModule"]] = relationship(
        back_populates="course", cascade="all, delete-orphan", order_by="CourseModule.display_order"
    )


class CourseModule(Base):
    __tablename__ = "course_modules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="published")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    course: Mapped["Course"] = relationship(back_populates="modules")
    lessons: Mapped[list["Lesson"]] = relationship(
        back_populates="module", cascade="all, delete-orphan", order_by="Lesson.display_order"
    )


class Lesson(Base):
    __tablename__ = "lessons"
    __table_args__ = (CheckConstraint("status in ('draft','published','archived')", name="ck_lesson_status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("course_modules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalised so lesson lookups can return course_slug without walking up.
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict | None] = mapped_column(JSONB)             # {format:"html", body:"..."}
    resources: Mapped[list | None] = mapped_column(JSONB)          # list[{type,language,content,...}]
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="8")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft", index=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    module: Mapped["CourseModule"] = relationship(back_populates="lessons")
    course: Mapped["Course"] = relationship(foreign_keys=[course_id])
    practice_links: Mapped[list["LessonProblemMap"]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )


class LessonProblemMap(Base):
    __tablename__ = "lesson_problem_map"
    __table_args__ = (UniqueConstraint("lesson_id", "problem_id", name="uq_lesson_problem_map"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("codelab_problems.id", ondelete="CASCADE"), nullable=False, index=True
    )

    lesson: Mapped["Lesson"] = relationship(back_populates="practice_links")
    problem: Mapped["CodelabProblem"] = relationship()


class Quiz(Base):
    __tablename__ = "quizzes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    lesson_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lessons.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="Quiz")
    pass_percent: Mapped[int] = mapped_column(Integer, nullable=False, server_default="70")
    xp_reward: Mapped[int] = mapped_column(Integer, nullable=False, server_default="10")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    questions: Mapped[list["QuizQuestion"]] = relationship(
        back_populates="quiz", cascade="all, delete-orphan", order_by="QuizQuestion.display_order"
    )


class QuizQuestion(Base):
    __tablename__ = "quiz_questions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    quiz_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="single")  # single | boolean
    options: Mapped[list | None] = mapped_column(JSONB)            # list[{id,text}] for single-choice
    correct_answer: Mapped[dict | None] = mapped_column(JSONB)     # {"value": <option id | bool>}
    explanation: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    quiz: Mapped["Quiz"] = relationship(back_populates="questions")


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"
    __table_args__ = (Index("ix_quiz_attempts_user_quiz", "user_id", "quiz_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quiz_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    score: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    answers: Mapped[list | None] = mapped_column(JSONB)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class UserCourseProgress(Base):
    __tablename__ = "user_course_progress"
    __table_args__ = (UniqueConstraint("user_id", "course_id", name="uq_user_course_progress"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    completion_percent: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class UserLessonProgress(Base):
    __tablename__ = "user_lesson_progress"
    __table_args__ = (UniqueConstraint("user_id", "lesson_id", name="uq_user_lesson_progress"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="not_started")  # not_started|in_progress|completed
    completed_percent: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    time_spent_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class UserLessonBookmark(Base):
    __tablename__ = "user_lesson_bookmarks"
    __table_args__ = (UniqueConstraint("user_id", "lesson_id", name="uq_user_lesson_bookmark"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bookmarked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class UserLessonNote(Base):
    __tablename__ = "user_lesson_notes"
    __table_args__ = (UniqueConstraint("user_id", "lesson_id", name="uq_user_lesson_note"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
