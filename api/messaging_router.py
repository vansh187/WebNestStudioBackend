import uuid

from fastapi import APIRouter, Depends, Path, Query, Response, status

from core.dependencies import get_current_user, get_messaging_service
from database.models import User
from schemas.messaging_schemas import (
    AddParticipantsRequest,
    ConversationListResponse,
    ConversationOut,
    CreateDirectRequest,
    CreateGroupRequest,
    MarkReadRequest,
    MessageListResponse,
    MessageOut,
    ReactionMutationResponse,
    ReactionRequest,
    RenameConversationRequest,
    SendMessageRequest,
    SignUploadRequest,
    SignUploadResponse,
)
from services.messaging_service import MessagingService

router = APIRouter(prefix="/api/messaging", tags=["messaging"])


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> ConversationListResponse:
    return await messaging.list_conversations(current_user)


@router.post(
    "/conversations/group",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_group_conversation(
    payload: CreateGroupRequest,
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> ConversationOut:
    return await messaging.create_group(current_user, payload.title, payload.participant_ids)


@router.post("/conversations/direct", response_model=ConversationOut)
async def create_direct_conversation(
    payload: CreateDirectRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> ConversationOut:
    conversation, created = await messaging.create_direct(current_user, payload.user_id)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return conversation


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> ConversationOut:
    return await messaging.get_conversation(current_user, conversation_id)


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
async def rename_conversation(
    conversation_id: uuid.UUID,
    payload: RenameConversationRequest,
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> ConversationOut:
    return await messaging.rename_conversation(current_user, conversation_id, payload.title)


@router.post(
    "/conversations/{conversation_id}/participants", response_model=ConversationOut
)
async def add_participants(
    conversation_id: uuid.UUID,
    payload: AddParticipantsRequest,
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> ConversationOut:
    return await messaging.add_participants(current_user, conversation_id, payload.user_ids)


@router.delete(
    "/conversations/{conversation_id}/participants/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_participant(
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> Response:
    await messaging.remove_participant(current_user, conversation_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/conversations/{conversation_id}/messages", response_model=MessageListResponse
)
async def list_messages(
    conversation_id: uuid.UUID,
    limit: int = Query(default=30, ge=1, le=100),
    before: uuid.UUID | None = Query(default=None),
    after: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> MessageListResponse:
    return await messaging.list_messages(current_user, conversation_id, limit, before, after)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> MessageOut:
    return await messaging.send_message(
        current_user,
        conversation_id,
        payload.body,
        payload.reply_to_message_id,
        payload.attachments,
    )


@router.post(
    "/conversations/{conversation_id}/read", status_code=status.HTTP_204_NO_CONTENT
)
async def mark_conversation_read(
    conversation_id: uuid.UUID,
    payload: MarkReadRequest,
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> Response:
    await messaging.mark_read(current_user, conversation_id, payload.last_read_message_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/messages/{message_id}/reactions", response_model=ReactionMutationResponse
)
async def add_reaction(
    message_id: uuid.UUID,
    payload: ReactionRequest,
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> ReactionMutationResponse:
    return await messaging.add_reaction(current_user, message_id, payload.emoji)


@router.delete(
    "/messages/{message_id}/reactions/{emoji}", response_model=ReactionMutationResponse
)
async def remove_reaction(
    message_id: uuid.UUID,
    emoji: str = Path(min_length=1, max_length=32),
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> ReactionMutationResponse:
    return await messaging.remove_reaction(current_user, message_id, emoji)


@router.delete("/messages/{message_id}", response_model=MessageOut)
async def delete_message(
    message_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> MessageOut:
    return await messaging.delete_message(current_user, message_id)


@router.post("/attachments/sign-upload", response_model=SignUploadResponse)
async def sign_attachment_upload(
    payload: SignUploadRequest,
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> SignUploadResponse:
    return await messaging.sign_upload(
        current_user, payload.filename, payload.mime_type, payload.size_bytes
    )
