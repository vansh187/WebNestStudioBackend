"""Request and response models for the CodeLab AI assistant. Unknown fields such
as model or system are ignored: the server alone chooses those."""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from schemas.coding_schemas import Text

MAX_BODY_BYTES = 200 * 1024
MAX_MESSAGES = 60
MAX_USER_MESSAGE_CHARS = 500
# Where any question is welcome, a letter or resume to work on has no file to live in.
MAX_ANY_QUESTION_CHARS = 4000
MAX_CONTEXT_FILES = 200
MAX_CONTEXT_CONTENT_CHARS = 12_000
MAX_FILE_NAME_CHARS = 500

AssistantMode = Literal["ask", "agent"]
# "general" is the AI Agent screen's Anything space: no project to run, and Agent mode only.
AssistantLanguage = Literal["backend", "python", "java", "web", "spring", "node", "express", "general"]
GENERAL_LANGUAGE: AssistantLanguage = "general"
ContextText = Annotated[Text, Field(max_length=MAX_CONTEXT_CONTENT_CHARS)]
FileName = Annotated[Text, Field(min_length=1, max_length=MAX_FILE_NAME_CHARS)]


class ChatToolCall(BaseModel):
    id: Annotated[Text, Field(min_length=1, max_length=200)]
    name: Annotated[Text, Field(min_length=1, max_length=100)]
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: Text | None = None
    tool_calls: list[ChatToolCall] | None = None
    tool_call_id: Annotated[Text, Field(min_length=1, max_length=200)] | None = None
    is_error: bool = False

    @model_validator(mode="after")
    def check_role_fields(self) -> "ChatMessage":
        if self.role == "user" and not (self.content and self.content.strip()):
            raise ValueError("A user message needs content")
        if self.role == "user" and len(self.content or "") > MAX_ANY_QUESTION_CHARS:
            raise ValueError(f"A user message can be at most {MAX_ANY_QUESTION_CHARS} characters")
        if self.role == "assistant" and not self.content and not self.tool_calls:
            raise ValueError("An assistant message needs content or tool calls")
        if self.role == "tool" and (self.tool_call_id is None or self.content is None):
            raise ValueError("A tool message needs tool_call_id and content")
        return self


class OpenFile(BaseModel):
    name: FileName
    content: ContextText


class ChatContext(BaseModel):
    language: AssistantLanguage
    files: list[FileName] = Field(default_factory=list, max_length=MAX_CONTEXT_FILES)
    open_file: OpenFile | None = None
    last_error: ContextText | None = None


class ChatRequest(BaseModel):
    mode: AssistantMode
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    context: ChatContext

    @model_validator(mode="after")
    def check_general_is_agent_only(self) -> "ChatRequest":
        if self.context.language == GENERAL_LANGUAGE and self.mode != "agent":
            raise ValueError("The general space is only available in Agent mode")
        return self

    @model_validator(mode="after")
    def check_coding_agent_message_length(self) -> "ChatRequest":
        if self.mode == "agent" and self.context.language != GENERAL_LANGUAGE:
            for message in self.messages:
                if message.role == "user" and len(message.content or "") > MAX_USER_MESSAGE_CHARS:
                    raise ValueError(f"A user message can be at most {MAX_USER_MESSAGE_CHARS} characters")
        return self


class AccessResponse(BaseModel):
    """`active`: may use the AI now. `is_prime`: inside a paid period, so shows the badge,
    even after the tokens run out."""

    active: bool
    expires_at: datetime | None
    price_paise: int
    pass_days: int
    tokens_limit: int
    tokens_used: int
    is_prime: bool
    member_label: str | None


class AssistantUsageResponse(BaseModel):
    """`provider_name` and `access` are left out completely while the paywall is off."""

    enabled: bool
    calls_used_today: int
    calls_limit: int
    provider_name: str | None = None
    access: AccessResponse | None = None


class MembershipResponse(BaseModel):
    is_prime: bool
    member_label: str | None
    expires_at: datetime | None


class OrderResponse(BaseModel):
    order_id: str
    amount: int
    currency: str
    key_id: str
    name: str
    description: str


class VerifyRequest(BaseModel):
    # Razorpay's ids are like order_ABC123. Anything else, such as a NUL byte Postgres
    # would refuse, is a bad request and never reaches the database.
    razorpay_order_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_]+$")
    razorpay_payment_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_]+$")
    razorpay_signature: str = Field(min_length=1, max_length=256)


class VerifyResponse(BaseModel):
    access: AccessResponse


class GooglePlayVerifyRequest(BaseModel):
    """As the Android app sends it. Every field is optional here so the service, not the
    parser, decides each status: a wrong product is a 404 and a missing token a 400."""

    model_config = ConfigDict(extra="ignore")

    product_id: str | None = None
    purchase_token: str | None = None
    # Only for logs and support: Google's own order id is the one that is saved.
    order_id: str | None = None
    terms_version: str | None = None
    terms_accepted_at: str | None = None


AssistantReportReason = Literal["harmful", "sexual", "hateful", "misleading", "privacy", "other"]
MAX_REPORT_DETAILS_CHARS = 1000
MAX_REPORT_REPLY_CHARS = 8000
MAX_REPORT_PROMPT_CHARS = 4000
MAX_REPORT_MESSAGE_ID_CHARS = 64


def _cut_to(limit: int) -> Callable[[Any], Any]:
    """Long text is cut, not refused. NUL bytes go too, since Postgres can't store them."""

    def cut(value: Any) -> Any:
        return value.replace("\x00", "")[:limit] if isinstance(value, str) else value

    return cut


class AssistantReportCreate(BaseModel):
    reason: AssistantReportReason
    details: Annotated[Text | None, BeforeValidator(_cut_to(MAX_REPORT_DETAILS_CHARS))] = None
    reply_text: Annotated[Text, BeforeValidator(_cut_to(MAX_REPORT_REPLY_CHARS))]
    prompt_text: Annotated[Text | None, BeforeValidator(_cut_to(MAX_REPORT_PROMPT_CHARS))] = None
    client_message_id: Annotated[Text | None, BeforeValidator(_cut_to(MAX_REPORT_MESSAGE_ID_CHARS))] = None

    @model_validator(mode="after")
    def check_reply_text(self) -> "AssistantReportCreate":
        if not self.reply_text.strip():
            raise ValueError("reply_text is required")
        return self


class AssistantReportCreated(BaseModel):
    id: uuid.UUID
    status: Literal["open", "resolved"]


class AssistantReportOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    reporter_email: str | None
    reason: str
    details: str | None
    reply_text: str
    prompt_text: str | None
    client_message_id: str | None
    provider_name: str | None
    model: str | None
    status: str
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: uuid.UUID | None


class AssistantReportList(BaseModel):
    reports: list[AssistantReportOut]
