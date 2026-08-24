import uuid
from datetime import datetime

from pydantic import BaseModel

from schemas.chatbot_schemas import CollectedFields


class ChatStartResponse(BaseModel):
    thread_id: uuid.UUID
    mode: str
    reply: str


class ChatMessageRequest(BaseModel):
    message: str


class ChatMessageResponse(BaseModel):
    thread_id: uuid.UUID
    mode: str
    reply: str | None = None
    html: str | None = None
    generation_id: uuid.UUID | None = None
    collected_fields: CollectedFields | None = None
    ready_for_plan: bool | None = None


class ChatHistoryItem(BaseModel):
    role: str
    content: str
    mode: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatThreadListItem(BaseModel):
    thread_id: uuid.UUID
    mode: str
    created_at: datetime
    updated_at: datetime
    message_count: int


class ChatThreadListResponse(BaseModel):
    threads: list[ChatThreadListItem]


class ChatThreadDetailResponse(BaseModel):
    thread_id: uuid.UUID
    mode: str
    messages: list[ChatHistoryItem]
