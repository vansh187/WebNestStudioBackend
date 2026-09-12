import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.datetime_utils import as_aware
from core.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from database.messaging_persistence import MessagingPersistence
from database.models import Conversation, ConversationParticipant, Message, MessageReport, User
from database.user_persistence import UserPersistence
from schemas.messaging_schemas import (
    AttachmentInput,
    AttachmentOut,
    ConversationListResponse,
    ConversationOut,
    LastMessageOut,
    MessageListResponse,
    MessageOut,
    ParticipantOut,
    ReactionGroup,
    ReactionMutationResponse,
    ReplyPreview,
    ReportListResponse,
    ReportOut,
    SignUploadResponse,
    UserSearchResponse,
    UserSummary,
)
from services.storage_service import StorageService

logger = logging.getLogger("webnest.messaging")

MAX_ATTACHMENTS_PER_MESSAGE = 10
PREVIEW_LENGTH = 200
REPLY_PREVIEW_LENGTH = 140
_ADMIN_ROLES = ("owner", "admin")
# How many signed-download lookups may be in flight at once while serialising a
# page of messages. The StorageService cache means most calls are cache hits;
# this just caps the cold-start burst against Supabase.
_SIGN_CONCURRENCY = 8


def _truncate(text: str | None, length: int) -> str | None:
    if not text:
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) <= length:
        return collapsed
    return collapsed[: length - 1].rstrip() + "…"


def _mask_email(email: str | None) -> str:
    """`aditya@example.com` -> `ad****@example.com`. Keeps enough for a human to
    recognise their own colleague in the picker, but the result is useless as a
    harvested mailing list."""
    value = (email or "").strip()
    if "@" not in value:
        return ""
    local, _, domain = value.partition("@")
    if len(local) <= 2:
        masked_local = (local[:1] or "*") + "*"
    else:
        masked_local = local[:2] + "*" * min(len(local) - 2, 6)
    return f"{masked_local}@{domain}"


class MessagingService:
    """In-app project chat: user-to-user group + 1:1 conversations with
    attachments, replies, reactions and unread counts. Enforces the section-7
    authorisation matrix, resolves stored attachment url_paths to fresh signed
    URLs on read, and de-duplicates direct conversations. Mirrors
    ChatbotService's composition style - a plain class holding persistence
    helpers plus the (stateless, shared) StorageService."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        storage_service: StorageService,
    ) -> None:
        self._messaging = MessagingPersistence(session)
        self._users = UserPersistence(session)
        self._settings = settings
        self._storage = storage_service

    # ================================================================== #
    # Conversations
    # ================================================================== #
    async def list_conversations(self, user: User) -> ConversationListResponse:
        rows = await self._messaging.list_conversations_for_user(user.id)
        latest = await self._messaging.latest_messages_for_conversations(
            [conversation.id for conversation, _ in rows]
        )
        conversations = [
            self._serialize_conversation(
                conversation,
                unread_count,
                user.id,
                self._last_message_view(conversation, latest.get(conversation.id)),
            )
            for conversation, unread_count in rows
        ]
        return ConversationListResponse(conversations=conversations)

    async def create_group(
        self, user: User, title: str, participant_ids: list[uuid.UUID]
    ) -> ConversationOut:
        wanted = {pid for pid in participant_ids if pid != user.id}
        if not wanted:
            raise BadRequestError("Add at least one other person to the group")
        found = await self._users.get_active_by_ids(list(wanted))
        if {member.id for member in found} != wanted:
            raise BadRequestError("One or more users could not be found")

        members: list[tuple[uuid.UUID, str]] = [(user.id, "owner")]
        members.extend((member_id, "member") for member_id in wanted)
        conversation = await self._messaging.create_conversation(
            conversation_type="group",
            title=title.strip(),
            created_by=user.id,
            members=members,
        )
        return await self._conversation_view(conversation, 0, user.id)

    async def create_direct(
        self, user: User, other_user_id: uuid.UUID
    ) -> tuple[ConversationOut, bool]:
        if other_user_id == user.id:
            raise BadRequestError("You cannot start a chat with yourself")
        if not await self._users.get_active_by_ids([other_user_id]):
            raise BadRequestError("User not found")

        existing = await self._messaging.find_direct_conversation(user.id, other_user_id)
        if existing is not None:
            rejoin = [
                candidate_id
                for candidate_id in (user.id, other_user_id)
                if not any(
                    participant.user_id == candidate_id and participant.left_at is None
                    for participant in existing.participants
                )
            ]
            if rejoin:
                await self._messaging.add_participants(existing, rejoin)
                existing = await self._messaging.get_conversation(existing.id) or existing
            unread = await self._unread_for(existing, user.id)
            return await self._conversation_view(existing, unread, user.id), False

        conversation = await self._messaging.create_conversation(
            conversation_type="direct",
            title=None,
            created_by=user.id,
            members=[(user.id, "member"), (other_user_id, "member")],
        )
        return await self._conversation_view(conversation, 0, user.id), True

    async def get_conversation(
        self, user: User, conversation_id: uuid.UUID
    ) -> ConversationOut:
        conversation = await self._load_conversation_or_404(conversation_id)
        self._require_participant(conversation, user.id)
        unread = await self._unread_for(conversation, user.id)
        return await self._conversation_view(conversation, unread, user.id)

    async def rename_conversation(
        self, user: User, conversation_id: uuid.UUID, title: str
    ) -> ConversationOut:
        conversation = await self._load_conversation_or_404(conversation_id)
        participant = self._require_participant(conversation, user.id)
        if conversation.type != "group":
            raise ConflictError("Direct chats cannot be renamed")
        self._require_admin(participant)
        conversation = await self._messaging.rename_conversation(conversation, title.strip())
        unread = await self._unread_for(conversation, user.id)
        return await self._conversation_view(conversation, unread, user.id)

    async def add_participants(
        self, user: User, conversation_id: uuid.UUID, user_ids: list[uuid.UUID]
    ) -> ConversationOut:
        conversation = await self._load_conversation_or_404(conversation_id)
        participant = self._require_participant(conversation, user.id)
        if conversation.type != "group":
            raise ConflictError("Cannot add members to a direct chat")

        active_ids = {
            member.user_id
            for member in conversation.participants
            if member.left_at is None
        }
        wanted = {pid for pid in user_ids if pid not in active_ids}
        if wanted:
            found = await self._users.get_active_by_ids(list(wanted))
            if {member.id for member in found} != wanted:
                raise BadRequestError("One or more users could not be found")
            await self._messaging.add_participants(conversation, list(wanted))
            conversation = await self._messaging.get_conversation(conversation_id) or conversation

        unread = await self._unread_for(conversation, user.id)
        return await self._conversation_view(conversation, unread, user.id)

    async def remove_participant(
        self, user: User, conversation_id: uuid.UUID, target_user_id: uuid.UUID
    ) -> None:
        conversation = await self._load_conversation_or_404(conversation_id)
        participant = self._require_participant(conversation, user.id)
        is_self = target_user_id == user.id
        if not is_self and participant.role not in _ADMIN_ROLES:
            raise ForbiddenError("Only group admins can remove other members")

        target = next(
            (
                member
                for member in conversation.participants
                if member.user_id == target_user_id and member.left_at is None
            ),
            None,
        )
        if target is None:
            raise NotFoundError("That person is not a member of this conversation")

        # An admin must not be able to depose the owner. The owner can only be
        # removed by the owner themselves leaving, which triggers hand-over.
        if target.role == "owner" and not is_self:
            raise ForbiddenError(
                "The group owner cannot be removed. The owner has to leave the group themselves."
            )

        new_owner_id = self._pick_successor_owner(conversation, target)
        await self._messaging.remove_participant(conversation_id, target_user_id, new_owner_id)

    async def create_project_conversation(
        self,
        *,
        owner_user_id: uuid.UUID,
        title: str,
        project_id: uuid.UUID,
    ) -> Conversation:
        """Find-or-create the team group chat for a project (spec section 11).

        Idempotent per project: a second call for the same project_id returns
        the existing conversation, and a concurrent double-create loses the
        race gracefully (the partial unique index uq_conversations_project
        raises, we swallow it and re-read) rather than surfacing a 409.

        Note: create_conversation commits on its own session, so this is NOT
        part of the caller's open transaction - ProjectService must create the
        project row and flush it before calling here, and treat a later failure
        as needing its own compensation.
        """
        existing = await self._messaging.get_conversation_by_project(project_id)
        if existing is not None:
            return existing
        clean_title = (title or "").strip()[:120] or "Project chat"
        try:
            return await self._messaging.create_conversation(
                conversation_type="group",
                title=clean_title,
                created_by=owner_user_id,
                members=[(owner_user_id, "owner")],
                project_id=project_id,
            )
        except ConflictError:
            # A concurrent call already created this project's room; the end
            # state we wanted now exists, so return it instead of erroring.
            raced = await self._messaging.get_conversation_by_project(project_id)
            if raced is not None:
                return raced
            raise

    # ================================================================== #
    # Messages
    # ================================================================== #
    async def list_messages(
        self,
        user: User,
        conversation_id: uuid.UUID,
        limit: int,
        before_id: uuid.UUID | None,
        after_id: uuid.UUID | None,
    ) -> MessageListResponse:
        conversation = await self._load_conversation_or_404(conversation_id)
        self._require_participant(conversation, user.id)
        if before_id is not None and after_id is not None:
            raise BadRequestError("Pass either 'before' or 'after', not both")

        cursor_id = before_id or after_id
        if cursor_id is not None and not await self._messaging.message_belongs_to_conversation(
            cursor_id, conversation_id
        ):
            raise BadRequestError(
                "The 'before'/'after' message id does not belong to this conversation"
            )

        rows, has_more = await self._messaging.list_messages(
            conversation_id, limit, before_id, after_id
        )
        return MessageListResponse(
            messages=await self._serialize_messages(rows, user.id),
            has_more=has_more,
        )

    async def send_message(
        self,
        user: User,
        conversation_id: uuid.UUID,
        body: str | None,
        reply_to_message_id: uuid.UUID | None,
        attachments: list[AttachmentInput] | None,
    ) -> MessageOut:
        conversation = await self._load_conversation_or_404(conversation_id)
        self._require_participant(conversation, user.id)

        clean_body = body.strip() if body and body.strip() else None
        attachment_list = attachments or []
        if clean_body is None and not attachment_list:
            raise ValidationError("Provide a message body or at least one attachment")
        if len(attachment_list) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise ValidationError(
                f"A message can have at most {MAX_ATTACHMENTS_PER_MESSAGE} attachments"
            )

        stored_attachments = [
            self._validated_attachment(item, user.id) for item in attachment_list
        ]

        if reply_to_message_id is not None:
            target = await self._messaging.get_message(reply_to_message_id)
            if (
                target is None
                or target.conversation_id != conversation_id
                or target.is_deleted
            ):
                raise BadRequestError(
                    "reply_to_message_id does not belong to this conversation"
                )

        message = await self._messaging.create_message(
            conversation=conversation,
            sender_id=user.id,
            body=clean_body,
            reply_to_message_id=reply_to_message_id,
            attachments=stored_attachments or None,
            preview=self._preview(clean_body, stored_attachments),
        )
        serialized = await self._serialize_messages([message], user.id)
        return serialized[0]

    async def mark_read(
        self, user: User, conversation_id: uuid.UUID, last_read_message_id: uuid.UUID
    ) -> None:
        conversation = await self._load_conversation_or_404(conversation_id)
        participant = self._require_participant(conversation, user.id)
        target = await self._messaging.get_message(last_read_message_id)
        if target is None or target.conversation_id != conversation_id:
            raise NotFoundError("That message is not part of this conversation")
        read_at = as_aware(target.created_at) or datetime.now(timezone.utc)
        await self._messaging.mark_read(participant, read_at)

    async def add_reaction(
        self, user: User, message_id: uuid.UUID, emoji: str
    ) -> ReactionMutationResponse:
        message = await self._require_message_participant(user, message_id)
        if message.is_deleted:
            raise BadRequestError("Cannot react to a deleted message")
        await self._messaging.add_reaction(message_id, user.id, emoji)
        return await self._reaction_response(message_id, user.id)

    async def remove_reaction(
        self, user: User, message_id: uuid.UUID, emoji: str
    ) -> ReactionMutationResponse:
        await self._require_message_participant(user, message_id)
        await self._messaging.remove_reaction(message_id, user.id, emoji)
        return await self._reaction_response(message_id, user.id)

    async def delete_message(self, user: User, message_id: uuid.UUID) -> MessageOut:
        message = await self._messaging.get_message(message_id)
        if message is None:
            raise NotFoundError("Message not found")
        conversation = await self._load_conversation_or_404(message.conversation_id)
        participant = self._require_participant(conversation, user.id)
        if message.sender_id != user.id and participant.role not in _ADMIN_ROLES:
            raise ForbiddenError("You can only delete your own messages")
        if not message.is_deleted:
            message = await self._messaging.soft_delete_message(message)
        serialized = await self._serialize_messages([message], user.id)
        return serialized[0]

    # ================================================================== #
    # Attachments & user search
    # ================================================================== #
    async def sign_upload(
        self, user: User, filename: str, mime_type: str, size_bytes: int
    ) -> SignUploadResponse:
        data = await self._storage.sign_upload(user.id, filename, mime_type, size_bytes)
        return SignUploadResponse(**data)

    async def search_users(
        self, user: User, query: str, limit: int
    ) -> UserSearchResponse:
        term = (query or "").strip()
        if len(term) < 2:
            raise BadRequestError("Search needs at least 2 characters")
        found = await self._users.search(term, user.id, limit)
        # The people-picker only needs to disambiguate names; it must not hand a
        # logged-in user a harvestable list of every active account's real
        # email. Names come through in full, the address is masked. (Full
        # emails are still shown for people you already share a conversation
        # with, via participant / sender summaries.)
        return UserSearchResponse(
            results=[
                UserSummary(
                    id=candidate.id,
                    full_name=candidate.full_name,
                    email=_mask_email(candidate.email),
                )
                for candidate in found
            ]
        )

    # ================================================================== #
    # Moderation
    # ================================================================== #
    async def report_message(
        self, user: User, message_id: uuid.UUID, reason: str
    ) -> ReportOut:
        """Any participant of the message's conversation can flag it for admin
        review. One report per (message, reporter) - the DB's unique
        constraint turns a repeat call into a clean ConflictError rather than
        piling up duplicate rows."""
        message = await self._require_message_participant(user, message_id)
        if message.is_deleted:
            raise BadRequestError("This message has been deleted and can no longer be reported")
        if message.sender_id == user.id:
            raise BadRequestError("You cannot report your own message")

        report = await self._messaging.create_report(
            message_id=message.id,
            reporter_id=user.id,
            reported_user_id=message.sender_id,
            reason=reason,
        )
        report.message = message
        report.reporter = user
        report.reported_user = message.sender
        return self._report_view(report)

    async def list_reports(self, status: str = "open") -> ReportListResponse:
        reports = await self._messaging.list_reports(status)
        return ReportListResponse(reports=[self._report_view(report) for report in reports])

    async def resolve_report(self, report_id: uuid.UUID) -> ReportOut:
        report = await self._messaging.get_report(report_id)
        if report is None:
            raise NotFoundError("Report not found")
        report = await self._messaging.resolve_report(report)
        return self._report_view(report)

    def _report_view(self, report: MessageReport) -> ReportOut:
        message = report.message
        return ReportOut(
            id=report.id,
            message_id=report.message_id,
            reporter=self._user_summary(report.reporter),
            reported_user=self._user_summary(report.reported_user),
            reason=report.reason,
            status=report.status,
            message_preview=None if message is None or message.is_deleted else _truncate(message.body, PREVIEW_LENGTH),
            message_deleted=bool(message is None or message.is_deleted),
            created_at=report.created_at,
        )

    # ================================================================== #
    # Authorisation helpers
    # ================================================================== #
    async def _load_conversation_or_404(self, conversation_id: uuid.UUID) -> Conversation:
        conversation = await self._messaging.get_conversation(conversation_id)
        if conversation is None:
            raise NotFoundError("Conversation not found")
        return conversation

    def _require_participant(
        self, conversation: Conversation, user_id: uuid.UUID
    ) -> ConversationParticipant:
        participant = next(
            (
                member
                for member in conversation.participants
                if member.user_id == user_id and member.left_at is None
            ),
            None,
        )
        if participant is None:
            raise ForbiddenError("You are not a participant in this conversation")
        return participant

    def _require_admin(self, participant: ConversationParticipant) -> None:
        if participant.role not in _ADMIN_ROLES:
            raise ForbiddenError("Only group owners or admins can do that")

    async def _require_message_participant(
        self, user: User, message_id: uuid.UUID
    ) -> Message:
        message = await self._messaging.get_message(message_id)
        if message is None:
            raise NotFoundError("Message not found")
        conversation = await self._load_conversation_or_404(message.conversation_id)
        self._require_participant(conversation, user.id)
        return message

    def _pick_successor_owner(
        self, conversation: Conversation, leaving: ConversationParticipant
    ) -> uuid.UUID | None:
        if conversation.type != "group" or leaving.role != "owner":
            return None
        remaining = [
            member
            for member in conversation.participants
            if member.left_at is None and member.user_id != leaving.user_id
        ]
        if not remaining:
            return None
        remaining.sort(key=lambda member: member.joined_at)
        admins = [member for member in remaining if member.role == "admin"]
        return (admins[0] if admins else remaining[0]).user_id

    # ================================================================== #
    # Serialisation
    # ================================================================== #
    async def _unread_for(self, conversation: Conversation, user_id: uuid.UUID) -> int:
        participant = next(
            (
                member
                for member in conversation.participants
                if member.user_id == user_id and member.left_at is None
            ),
            None,
        )
        if participant is None:
            return 0
        return await self._messaging.count_unread(
            conversation.id, user_id, as_aware(participant.last_read_at)
        )

    async def _conversation_view(
        self, conversation: Conversation, unread_count: int, me_id: uuid.UUID
    ) -> ConversationOut:
        """Serialise one conversation, resolving its last_message with a single
        batched query (not an N+1 - see latest_messages_for_conversations)."""
        return self._serialize_conversation(
            conversation,
            unread_count,
            me_id,
            await self._one_last_message(conversation),
        )

    async def _one_last_message(
        self, conversation: Conversation
    ) -> LastMessageOut | None:
        if conversation.last_message_at is None:
            return None
        latest = await self._messaging.latest_messages_for_conversations([conversation.id])
        return self._last_message_view(conversation, latest.get(conversation.id))

    def _serialize_conversation(
        self,
        conversation: Conversation,
        unread_count: int,
        me_id: uuid.UUID,
        last_message: LastMessageOut | None,
    ) -> ConversationOut:
        active = sorted(
            (member for member in conversation.participants if member.left_at is None),
            key=lambda member: member.joined_at,
        )
        participants = [
            ParticipantOut(
                user=self._user_summary(member.user),
                role=member.role,
                joined_at=member.joined_at,
                last_read_at=member.last_read_at,
            )
            for member in active
        ]
        return ConversationOut(
            id=conversation.id,
            type=conversation.type,
            title=conversation.title,
            project_id=conversation.project_id,
            created_by=conversation.created_by,
            participants=participants,
            last_message=last_message,
            unread_count=unread_count,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )

    def _last_message_view(
        self, conversation: Conversation, message: Message | None
    ) -> LastMessageOut | None:
        if message is None:
            return None
        sender_name = message.sender.full_name if message.sender else None
        if message.is_deleted:
            return LastMessageOut(
                id=message.id,
                sender_name=sender_name,
                preview="This message was deleted",
                created_at=message.created_at,
                has_attachment=False,
            )
        has_attachment = bool(message.attachments)
        preview = (
            conversation.last_message_preview
            or _truncate(message.body, PREVIEW_LENGTH)
            or ("Attachment" if has_attachment else None)
        )
        return LastMessageOut(
            id=message.id,
            sender_name=sender_name,
            preview=preview,
            created_at=message.created_at,
            has_attachment=has_attachment,
        )

    async def _serialize_messages(
        self, messages: list[Message], me_id: uuid.UUID
    ) -> list[MessageOut]:
        if not messages:
            return []
        groups_by_id = await self._messaging.reaction_groups_for_messages(
            [message.id for message in messages], me_id
        )

        # De-duplicate signing work: the same url_path can appear on many
        # messages, and StorageService caches per path anyway. Cap the cold
        # burst with a semaphore so one big page can't fan out hundreds of
        # concurrent Storage calls.
        paths: set[str] = set()
        for message in messages:
            if message.is_deleted or not message.attachments:
                continue
            for attachment in message.attachments:
                if isinstance(attachment, dict) and attachment.get("url_path"):
                    paths.add(str(attachment["url_path"]))

        signed_by_path: dict[str, str | None] = {}
        if paths:
            semaphore = asyncio.Semaphore(_SIGN_CONCURRENCY)

            async def _sign(path: str) -> tuple[str, str | None]:
                async with semaphore:
                    try:
                        return path, await self._storage.sign_download(path)
                    except Exception:  # noqa: BLE001 - never let one bad path 500 the list
                        logger.warning("sign_download failed for %s", path, exc_info=True)
                        return path, None

            for path, url in await asyncio.gather(*(_sign(path) for path in paths)):
                signed_by_path[path] = url

        signed: dict[tuple[uuid.UUID, int], str | None] = {}
        for message in messages:
            if message.is_deleted or not message.attachments:
                continue
            for index, attachment in enumerate(message.attachments):
                if isinstance(attachment, dict) and attachment.get("url_path"):
                    signed[(message.id, index)] = signed_by_path.get(
                        str(attachment["url_path"])
                    )

        return [
            self._build_message_out(message, groups_by_id.get(message.id, []), signed)
            for message in messages
        ]

    def _build_message_out(
        self,
        message: Message,
        reaction_rows: list[tuple[str, int, bool]],
        signed: dict[tuple[uuid.UUID, int], str | None],
    ) -> MessageOut:
        attachments: list[AttachmentOut] = []
        if not message.is_deleted and message.attachments:
            for index, attachment in enumerate(message.attachments):
                if not isinstance(attachment, dict):
                    continue
                mime = str(attachment.get("mime_type") or "application/octet-stream")
                attachments.append(
                    AttachmentOut(
                        url_path=str(attachment.get("url_path") or ""),
                        url=signed.get((message.id, index)),
                        name=str(attachment.get("name") or "file"),
                        mime_type=mime,
                        size_bytes=_coerce_int(attachment.get("size_bytes")),
                        kind=self._storage.derive_kind(mime),
                        width=_coerce_optional_int(attachment.get("width")),
                        height=_coerce_optional_int(attachment.get("height")),
                    )
                )

        reply_to = None
        if message.reply_to is not None:
            replied = message.reply_to
            reply_to = ReplyPreview(
                id=replied.id,
                sender=self._user_summary(replied.sender),
                body_preview=None if replied.is_deleted else _truncate(replied.body, REPLY_PREVIEW_LENGTH),
                is_deleted=replied.is_deleted,
            )

        reactions = (
            []
            if message.is_deleted
            else [
                ReactionGroup(emoji=emoji, count=count, reacted_by_me=reacted_by_me)
                for emoji, count, reacted_by_me in reaction_rows
            ]
        )

        return MessageOut(
            id=message.id,
            conversation_id=message.conversation_id,
            sender=self._user_summary(message.sender),
            body=message.body,
            attachments=attachments,
            reply_to=reply_to,
            reactions=reactions,
            is_deleted=message.is_deleted,
            created_at=message.created_at,
            edited_at=message.edited_at,
        )

    def _user_summary(self, user: User | None) -> UserSummary:
        # sender_id / reaction user_id are NOT NULL with ON DELETE CASCADE, so a
        # loaded message always has its sender row. Guard anyway rather than
        # dereference None.
        if user is None:
            return UserSummary(id=uuid.UUID(int=0), full_name=None, email="")
        return UserSummary(id=user.id, full_name=user.full_name, email=user.email)

    async def _reaction_response(
        self, message_id: uuid.UUID, me_id: uuid.UUID
    ) -> ReactionMutationResponse:
        rows = await self._messaging.reaction_groups(message_id, me_id)
        return ReactionMutationResponse(
            message_id=message_id,
            reactions=[
                ReactionGroup(emoji=emoji, count=count, reacted_by_me=reacted_by_me)
                for emoji, count, reacted_by_me in rows
            ],
        )

    def _validated_attachment(self, item: AttachmentInput, owner_id: uuid.UUID) -> dict:
        # The client echoes back a url_path it got from sign-upload. Only accept
        # one that sits under THIS sender's own object prefix - otherwise a user
        # who has merely seen someone else's attachment could re-attach that
        # private object into a conversation the owner isn't even in, and the
        # read path would happily mint a fresh signed URL for it.
        if not self._storage.owns_object_path(owner_id, item.url_path):
            raise ForbiddenError(
                "Attachment url_path was not issued to you. Upload the file first via "
                "/api/messaging/attachments/sign-upload."
            )
        self._storage.validate_descriptor(item.mime_type, item.size_bytes)
        return {
            "url_path": item.url_path,
            "name": item.name,
            "mime_type": item.mime_type,
            "size_bytes": item.size_bytes,
            # The client sends its guess; the server re-derives and wins.
            "kind": self._storage.derive_kind(item.mime_type),
            "width": item.width,
            "height": item.height,
        }

    def _preview(self, body: str | None, attachments: list[dict]) -> str | None:
        if body:
            return _truncate(body, PREVIEW_LENGTH)
        if attachments:
            first = attachments[0].get("name") or "attachment"
            extra = len(attachments) - 1
            return f"\U0001f4ce {first}" + (f" +{extra}" if extra > 0 else "")
        return None


def _coerce_int(value: object) -> int:
    try:
        return max(int(value), 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _coerce_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
