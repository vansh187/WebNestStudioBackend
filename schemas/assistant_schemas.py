"""Request and response models for the CodeLab AI assistant. Unknown fields such
as model or system are ignored: the server alone chooses those."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from schemas.coding_schemas import Text

MAX_BODY_BYTES = 200 * 1024
MAX_MESSAGES = 60
MAX_USER_MESSAGE_CHARS = 500
MAX_CONTEXT_FILES = 200
MAX_CONTEXT_CONTENT_CHARS = 12_000
MAX_FILE_NAME_CHARS = 500

AssistantMode = Literal["ask", "agent"]
AssistantLanguage = Literal["backend", "python", "java", "web"]
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
        if self.role == "user" and len(self.content or "") > MAX_USER_MESSAGE_CHARS:
            raise ValueError(f"A user message can be at most {MAX_USER_MESSAGE_CHARS} characters")
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


class AssistantUsageResponse(BaseModel):
    enabled: bool
    calls_used_today: int
    calls_limit: int
