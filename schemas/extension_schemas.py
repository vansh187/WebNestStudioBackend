"""Requests and responses of WebNest AI for VS Code (/api/extension/*, the contract in
VSCODE_EXTENSION_BACKEND.md section 2). Unknown fields are ignored, so newer extensions keep
working. Text that arrives too long is cut, not refused, and text the database or an encoder
couldn't take (a NUL byte, a lone surrogate) is replaced, so a stray byte in a user's file
never fails their request. What is refused is what the contract refuses: more than 8 file
segments, any attachment, an unknown role, a bad PKCE challenge."""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

MAX_BODY_BYTES = 512 * 1024
MAX_AUTH_BODY_BYTES = 8 * 1024
MAX_MESSAGE_CHARS = 4000
# What extension 0.1.0 sends; the adapter then cuts it to extension_max_context_chars.
MAX_FILE_CONTENT_CHARS = 60_000
MAX_HISTORY_TEXT_CHARS = 60_000
MAX_TOOL_RESULT_CHARS = 20_000
MAX_WORKSPACE_FILES = 2000
MAX_DIAGNOSTICS = 50
MAX_DIAGNOSTIC_CHARS = 2000
MAX_SEGMENTS = 8
MAX_ATTACHMENTS = 0
MAX_PATH_CHARS = 1000
MAX_RECEIVED_MESSAGES = 500
MAX_TOOL_CALLS_PER_MESSAGE = 50
MAX_LINE_NUMBER = 100_000_000
MAX_AGENT_STEPS = 25
MAX_DEVICE_NAME_CHARS = 100
MAX_APP_SCHEME_CHARS = 30
MAX_VERSION_CHARS = 32
MAX_ID_CHARS = 100
# A refresh token or login code is ~50 characters; anything far longer isn't one.
MAX_SECRET_CHARS = 200
MAX_REDIRECT_URI_CHARS = 2000


def safe_text(value: str) -> str:
    """Text every encoder and Postgres can take: lone surrogates become U+FFFD and NUL bytes
    go. Valid text, the usual case, is returned as it is."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        value = value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return value.replace("\x00", "") if "\x00" in value else value


def one_line(value: str) -> str:
    """For names shown on a web page: no control characters, single spaces."""
    return " ".join("".join(char if char.isprintable() else " " for char in value).split())


def _clean(limit: int, single_line: bool = False) -> Callable[[Any], Any]:
    def clean(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        cleaned = safe_text(value)
        if single_line:
            cleaned = one_line(cleaned)
        return cleaned[:limit]

    return clean


def _newest(limit: int) -> Callable[[Any], Any]:
    """A long list keeps its last `limit` items, the newest messages."""

    def cut(value: Any) -> Any:
        return value[-limit:] if isinstance(value, list) and len(value) > limit else value

    return cut


def _first(limit: int) -> Callable[[Any], Any]:
    def cut(value: Any) -> Any:
        return value[:limit] if isinstance(value, list) and len(value) > limit else value

    return cut


def _blank_to_none(value: Any) -> Any:
    return None if isinstance(value, str) and not value.strip() else value


Line = Annotated[int, Field(ge=0, le=MAX_LINE_NUMBER)]
PathText = Annotated[str, BeforeValidator(_clean(MAX_PATH_CHARS, single_line=True))]
ShortText = Annotated[str, BeforeValidator(_clean(200, single_line=True))]
FileText = Annotated[str, BeforeValidator(_clean(MAX_FILE_CONTENT_CHARS))]
HistoryText = Annotated[str, BeforeValidator(_clean(MAX_HISTORY_TEXT_CHARS))]


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---- 2.1 login codes, 2.3 token, 2.4 refresh, 2.5 logout --------------------------------


class CodeRequest(_Lenient):
    # An S256 challenge is always 43 base64url characters: a SHA-256 digest, no padding.
    code_challenge: Annotated[str, Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$")]
    code_challenge_method: Literal["S256"] = "S256"
    redirect_uri: Annotated[str, Field(min_length=1, max_length=MAX_REDIRECT_URI_CHARS)]
    device_name: Annotated[str | None, BeforeValidator(_clean(MAX_DEVICE_NAME_CHARS, single_line=True))] = None
    app_scheme: Annotated[str | None, BeforeValidator(_clean(MAX_APP_SCHEME_CHARS, single_line=True))] = None
    ext_version: Annotated[str | None, BeforeValidator(_clean(MAX_VERSION_CHARS, single_line=True))] = None

    @field_validator("device_name", "app_scheme", "ext_version")
    @classmethod
    def _empty_is_none(cls, value: str | None) -> str | None:
        return value or None


class CodeResponse(BaseModel):
    code: str
    expires_in: int


class TokenRequest(_Lenient):
    """Checked in the service, after the code is spent, so a wrong verifier burns it too."""

    code: Annotated[str, Field(min_length=1, max_length=MAX_SECRET_CHARS)]
    code_verifier: Annotated[str, Field(min_length=1, max_length=MAX_SECRET_CHARS)]
    redirect_uri: Annotated[str, Field(min_length=1, max_length=MAX_REDIRECT_URI_CHARS)]


class RefreshRequest(_Lenient):
    refresh_token: Annotated[str, Field(min_length=1, max_length=MAX_SECRET_CHARS)]


class TokenResponse(BaseModel):
    access_token: str
    access_expires_in: int
    refresh_token: str
    refresh_expires_at: datetime
    session_id: uuid.UUID
    token_type: Literal["Bearer"] = "Bearer"


# ---- 2.9 connected devices ----------------------------------------------------------------


class SessionOut(BaseModel):
    id: uuid.UUID
    device_name: str | None
    app_name: str | None
    ext_version: str | None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    current: bool


class SessionList(BaseModel):
    sessions: list[SessionOut]


# ---- 2.6 /me ------------------------------------------------------------------------------

Cap = Literal["day", "week", "pass"]


class MeUser(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None


class MeAccess(BaseModel):
    """`active` is true whenever the user may chat now, Prime or not."""

    active: bool
    is_prime: bool
    member_label: str | None
    expires_at: datetime | None
    tokens_limit: int
    tokens_used: int
    daily_tokens_limit: int
    daily_tokens_used: int
    weekly_tokens_limit: int
    weekly_tokens_used: int
    resets_at: datetime | None
    price_paise: int
    price_display: str
    pass_days: int
    cap: Cap | None


class MeService(BaseModel):
    enabled: bool
    provider_name: str
    message: str | None


class MeLimits(BaseModel):
    max_message_chars: int
    max_input_tokens: int
    max_context_chars: int
    chars_per_token: int
    max_attachments: int
    max_attachment_bytes: int
    max_attachments_total_bytes: int
    max_body_bytes: int
    max_tool_result_chars: int
    max_agent_steps: int
    allowed_attachment_kinds: list[str]
    supports_images: bool


class MeLinks(BaseModel):
    upgrade_url: str
    signup_url: str
    manage_url: str
    terms_url: str
    privacy_url: str
    support_url: str


class MeExtension(BaseModel):
    min_version: str
    latest_version: str


class MeResponse(BaseModel):
    user: MeUser
    access: MeAccess
    service: MeService
    limits: MeLimits
    links: MeLinks
    extension: MeExtension


# ---- 2.7 chat -----------------------------------------------------------------------------


class ExtToolCall(_Lenient):
    id: Annotated[str, BeforeValidator(_clean(200, single_line=True)), Field(min_length=1)]
    name: Annotated[str, BeforeValidator(_clean(100, single_line=True)), Field(min_length=1)]
    # A JSON string is accepted too, as some clients send it; anything not an object is {}.
    arguments: Any = None


class ExtMessage(_Lenient):
    role: Literal["user", "assistant", "tool"]
    content: HistoryText | None = None
    tool_calls: Annotated[list[ExtToolCall] | None, Field(max_length=MAX_TOOL_CALLS_PER_MESSAGE)] = None
    tool_call_id: Annotated[str | None, BeforeValidator(_clean(200, single_line=True))] = None
    is_error: bool | None = None


class Selection(_Lenient):
    start_line: Line
    end_line: Line
    text: FileText | None = None

    @model_validator(mode="after")
    def _in_order(self) -> "Selection":
        if self.end_line < self.start_line:
            self.start_line, self.end_line = self.end_line, self.start_line
        return self


class Segment(_Lenient):
    start_line: Line
    end_line: Line
    # "top", "focus" or "definition"; any other short word is shown as sent.
    reason: Annotated[str | None, BeforeValidator(_clean(20, single_line=True))] = None
    text: FileText = ""

    @model_validator(mode="after")
    def _in_order(self) -> "Segment":
        if self.end_line < self.start_line:
            self.start_line, self.end_line = self.end_line, self.start_line
        return self


class ActiveFile(_Lenient):
    path: Annotated[PathText, Field(min_length=1)]
    language: Annotated[str | None, BeforeValidator(_clean(100, single_line=True))] = None
    content: FileText | None = None
    content_start_line: Line | None = None
    truncated: bool | None = None
    selection: Selection | None = None
    segments: Annotated[list[Segment] | None, Field(max_length=MAX_SEGMENTS)] = None
    # "protected" or "secret": the extension refused to send the file, so only its path came.
    withheld: Annotated[str | None, BeforeValidator(_blank_to_none), BeforeValidator(_clean(20, single_line=True))] = None


class Diagnostic(_Lenient):
    path: PathText | None = None
    line: Line | None = None
    severity: Annotated[str | None, BeforeValidator(_clean(20, single_line=True))] = None
    message: Annotated[str, BeforeValidator(_clean(MAX_DIAGNOSTIC_CHARS))] = ""
    source: Annotated[str | None, BeforeValidator(_clean(100, single_line=True))] = None


class ExtContext(_Lenient):
    workspace_name: ShortText | None = None
    os: ShortText | None = None
    shell: ShortText | None = None
    ui_language: ShortText | None = None
    files: Annotated[list[PathText], BeforeValidator(_first(MAX_WORKSPACE_FILES))] = Field(default_factory=list)
    files_truncated: bool | None = None
    active_file: ActiveFile | None = None
    diagnostics: Annotated[list[Diagnostic], BeforeValidator(_first(MAX_DIAGNOSTICS))] = Field(default_factory=list)
    attachments: Annotated[list[Any], Field(max_length=MAX_ATTACHMENTS)] = Field(default_factory=list)

    @field_validator("files", "diagnostics", "attachments", mode="before")
    @classmethod
    def _null_is_empty(cls, value: Any) -> Any:
        return [] if value is None else value


class ExtensionChatRequest(_Lenient):
    conversation_id: Annotated[str | None, BeforeValidator(_clean(MAX_ID_CHARS, single_line=True))] = None
    client_request_id: Annotated[str | None, BeforeValidator(_clean(MAX_ID_CHARS, single_line=True))] = None
    # Unknown or missing values are handled as Ask by the backend adapter.
    mode: Annotated[str | None, BeforeValidator(_clean(20, single_line=True))] = None
    messages: Annotated[list[ExtMessage], BeforeValidator(_newest(MAX_RECEIVED_MESSAGES)), Field(min_length=1)]
    context: ExtContext | None = None
