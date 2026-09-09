import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# Shared value shapes (section 5 of the spec)
# --------------------------------------------------------------------------- #
class UserSummary(BaseModel):
    id: uuid.UUID
    full_name: str | None = None
    email: str

    model_config = {"from_attributes": True}


class AttachmentInput(BaseModel):
    """One attachment as sent by the client on POST .../messages - each was
    previously returned by the sign-upload endpoint and is re-validated
    (MIME + size) server-side before the message is stored."""

    url_path: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    kind: str | None = None
    width: int | None = None
    height: int | None = None


class AttachmentOut(BaseModel):
    url_path: str
    url: str | None = None
    name: str
    mime_type: str
    size_bytes: int
    kind: str
    width: int | None = None
    height: int | None = None


class ReactionGroup(BaseModel):
    emoji: str
    count: int
    reacted_by_me: bool


class ReplyPreview(BaseModel):
    id: uuid.UUID
    sender: UserSummary
    body_preview: str | None = None
    is_deleted: bool


class MessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    sender: UserSummary
    body: str | None = None
    attachments: list[AttachmentOut] = Field(default_factory=list)
    reply_to: ReplyPreview | None = None
    reactions: list[ReactionGroup] = Field(default_factory=list)
    is_deleted: bool
    created_at: datetime
    edited_at: datetime | None = None


class MessageListResponse(BaseModel):
    messages: list[MessageOut]
    has_more: bool


class ParticipantOut(BaseModel):
    user: UserSummary
    role: str
    joined_at: datetime
    last_read_at: datetime | None = None


class LastMessageOut(BaseModel):
    id: uuid.UUID
    sender_name: str | None = None
    preview: str | None = None
    created_at: datetime
    has_attachment: bool


class ConversationOut(BaseModel):
    id: uuid.UUID
    type: str
    title: str | None = None
    created_by: uuid.UUID
    participants: list[ParticipantOut]
    last_message: LastMessageOut | None = None
    unread_count: int
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    conversations: list[ConversationOut]


# --------------------------------------------------------------------------- #
# Request bodies (section 6)
# --------------------------------------------------------------------------- #
class CreateGroupRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    participant_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Title must not be blank")
        return cleaned


class CreateDirectRequest(BaseModel):
    user_id: uuid.UUID


class RenameConversationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Title must not be blank")
        return cleaned


class AddParticipantsRequest(BaseModel):
    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)


class SendMessageRequest(BaseModel):
    # Emptiness ("body or at least one attachment") and the 1-10 attachment
    # count are enforced in the service so the spec's exact 422 detail string
    # is returned rather than a generic validation-errors payload.
    body: str | None = Field(default=None, max_length=4000)
    reply_to_message_id: uuid.UUID | None = None
    attachments: list[AttachmentInput] | None = None


class MarkReadRequest(BaseModel):
    last_read_message_id: uuid.UUID


class ReactionRequest(BaseModel):
    emoji: str = Field(min_length=1, max_length=32)

    @field_validator("emoji")
    @classmethod
    def _strip_emoji(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("emoji must not be blank")
        return cleaned


class ReactionMutationResponse(BaseModel):
    message_id: uuid.UUID
    reactions: list[ReactionGroup]


# --------------------------------------------------------------------------- #
# Attachment upload signing (section 6.14)
# --------------------------------------------------------------------------- #
class SignUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)


class SignUploadResponse(BaseModel):
    url_path: str
    storage_path: str
    upload_url: str
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str]
    expires_in: int


# --------------------------------------------------------------------------- #
# User search (section 6.15)
# --------------------------------------------------------------------------- #
class UserSearchResponse(BaseModel):
    results: list[UserSummary]
