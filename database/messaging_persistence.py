import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.datetime_utils import as_aware
from core.exceptions import ConflictError
from database.base_persistence import BasePersistence
from database.models import Conversation, ConversationParticipant, Message, MessageReaction


class MessagingPersistence(BasePersistence):
    """CRUD for the user-to-user chat tables (conversations,
    conversation_participants, messages, message_reactions). All error handling
    goes through BasePersistence so the service and router only ever see
    DomainErrors, never raw driver exceptions."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    # ------------------------------------------------------------------ #
    # Conversations
    # ------------------------------------------------------------------ #
    async def list_conversations_for_user(self, user_id: uuid.UUID) -> list[tuple[Conversation, int]]:
        """Every conversation the user is an ACTIVE participant of, newest
        activity first, each paired with the user's unread count (non-deleted
        messages from other people newer than their last_read_at)."""
        last_read = ConversationParticipant.last_read_at
        unread_count = (
            select(func.count(Message.id))
            .where(
                Message.conversation_id == Conversation.id,
                Message.sender_id != user_id,
                Message.is_deleted.is_(False),
                or_(last_read.is_(None), Message.created_at > last_read),
            )
            .correlate(Conversation, ConversationParticipant)
            .scalar_subquery()
        )
        statement = (
            select(Conversation, unread_count)
            .join(ConversationParticipant, ConversationParticipant.conversation_id == Conversation.id)
            .where(
                ConversationParticipant.user_id == user_id,
                ConversationParticipant.left_at.is_(None),
            )
            .order_by(Conversation.last_message_at.desc().nullslast(), Conversation.created_at.desc())
            .options(
                selectinload(Conversation.participants).selectinload(ConversationParticipant.user)
            )
        )
        result = await self._execute(statement)
        return [(row[0], row[1] or 0) for row in result.all()]

    async def get_conversation(self, conversation_id: uuid.UUID) -> Conversation | None:
        result = await self._execute(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .options(selectinload(Conversation.participants).selectinload(ConversationParticipant.user))
        )
        return result.scalar_one_or_none()

    async def get_conversation_by_project(
        self, project_id: uuid.UUID
    ) -> Conversation | None:
        """The team room for a project (spec section 11), if one exists. Used by
        create_project_conversation to stay idempotent per project."""
        result = await self._execute(
            select(Conversation)
            .where(Conversation.project_id == project_id)
            .order_by(Conversation.created_at.asc())
            .limit(1)
            .options(
                selectinload(Conversation.participants).selectinload(ConversationParticipant.user)
            )
        )
        return result.scalar_one_or_none()

    async def find_direct_conversation(
        self, user_a: uuid.UUID, user_b: uuid.UUID
    ) -> Conversation | None:
        """The 1:1 conversation shared by both users, if one already exists -
        including one either of them previously left (it gets resurrected
        rather than duplicated)."""
        has_a = select(ConversationParticipant.conversation_id).where(
            ConversationParticipant.user_id == user_a
        )
        has_b = select(ConversationParticipant.conversation_id).where(
            ConversationParticipant.user_id == user_b
        )
        result = await self._execute(
            select(Conversation)
            .where(
                Conversation.type == "direct",
                Conversation.id.in_(has_a),
                Conversation.id.in_(has_b),
            )
            .order_by(Conversation.created_at.asc())
            .limit(1)
            .options(selectinload(Conversation.participants).selectinload(ConversationParticipant.user))
        )
        return result.scalar_one_or_none()

    async def create_conversation(
        self,
        conversation_type: str,
        title: str | None,
        created_by: uuid.UUID,
        members: Sequence[tuple[uuid.UUID, str]],
        project_id: uuid.UUID | None = None,
    ) -> Conversation:
        conversation = Conversation(
            type=conversation_type,
            title=title,
            created_by=created_by,
            project_id=project_id,
        )
        self._session.add(conversation)
        await self._session.flush()
        seen: set[uuid.UUID] = set()
        for member_id, role in members:
            if member_id in seen:
                continue
            seen.add(member_id)
            self._session.add(
                ConversationParticipant(
                    conversation_id=conversation.id, user_id=member_id, role=role
                )
            )
        await self._commit(conflict_message="This conversation already exists")
        reloaded = await self.get_conversation(conversation.id)
        # get_conversation only returns None if the row vanished between the
        # commit and the read (another request deleting it); treat that as the
        # freshly created object rather than raising.
        return reloaded if reloaded is not None else conversation

    async def rename_conversation(self, conversation: Conversation, title: str) -> Conversation:
        conversation.title = title
        await self._commit()
        reloaded = await self.get_conversation(conversation.id)
        return reloaded if reloaded is not None else conversation

    # ------------------------------------------------------------------ #
    # Participants
    # ------------------------------------------------------------------ #
    async def get_participant(
        self, conversation_id: uuid.UUID, user_id: uuid.UUID, active_only: bool = True
    ) -> ConversationParticipant | None:
        conditions = [
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.user_id == user_id,
        ]
        if active_only:
            conditions.append(ConversationParticipant.left_at.is_(None))
        result = await self._execute(
            select(ConversationParticipant)
            .where(*conditions)
            .options(selectinload(ConversationParticipant.user))
        )
        return result.scalar_one_or_none()

    async def list_participants(
        self, conversation_id: uuid.UUID, active_only: bool = True
    ) -> list[ConversationParticipant]:
        conditions = [ConversationParticipant.conversation_id == conversation_id]
        if active_only:
            conditions.append(ConversationParticipant.left_at.is_(None))
        result = await self._execute(
            select(ConversationParticipant)
            .where(*conditions)
            .order_by(ConversationParticipant.joined_at.asc())
            .options(selectinload(ConversationParticipant.user))
        )
        return list(result.scalars().all())

    async def add_participants(
        self, conversation: Conversation, user_ids: Sequence[uuid.UUID]
    ) -> None:
        existing = {
            participant.user_id: participant
            for participant in await self.list_participants(conversation.id, active_only=False)
        }
        for user_id in user_ids:
            participant = existing.get(user_id)
            if participant is None:
                self._session.add(
                    ConversationParticipant(
                        conversation_id=conversation.id, user_id=user_id, role="member"
                    )
                )
            elif participant.left_at is not None:
                participant.left_at = None
                participant.role = "member"
        await self._commit()

    async def remove_participant(
        self,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        new_owner_id: uuid.UUID | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        for participant in await self.list_participants(conversation_id, active_only=False):
            if participant.user_id == user_id and participant.left_at is None:
                participant.left_at = now
            if (
                new_owner_id is not None
                and participant.user_id == new_owner_id
                and participant.left_at is None
            ):
                participant.role = "owner"
        await self._commit()

    async def count_unread(
        self, conversation_id: uuid.UUID, user_id: uuid.UUID, last_read_at: datetime | None
    ) -> int:
        conditions = [
            Message.conversation_id == conversation_id,
            Message.sender_id != user_id,
            Message.is_deleted.is_(False),
        ]
        if last_read_at is not None:
            conditions.append(Message.created_at > last_read_at)
        result = await self._execute(select(func.count(Message.id)).where(*conditions))
        return int(result.scalar_one() or 0)

    async def mark_read(self, participant: ConversationParticipant, read_at: datetime) -> None:
        current = as_aware(participant.last_read_at)
        if current is not None and read_at <= current:
            return
        participant.last_read_at = read_at
        await self._commit()

    # ------------------------------------------------------------------ #
    # Messages
    # ------------------------------------------------------------------ #
    def _message_options(self):
        return (
            selectinload(Message.sender),
            selectinload(Message.reactions),
            selectinload(Message.reply_to).selectinload(Message.sender),
        )

    async def get_message(self, message_id: uuid.UUID) -> Message | None:
        result = await self._execute(
            select(Message).where(Message.id == message_id).options(*self._message_options())
        )
        return result.scalar_one_or_none()

    async def message_belongs_to_conversation(
        self, message_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> bool:
        result = await self._execute(
            select(Message.id)
            .where(Message.id == message_id, Message.conversation_id == conversation_id)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def list_messages(
        self,
        conversation_id: uuid.UUID,
        limit: int,
        before_id: uuid.UUID | None = None,
        after_id: uuid.UUID | None = None,
    ) -> tuple[list[Message], bool]:
        """Returns (messages ascending by created_at, has_more). has_more is
        DIRECTIONAL: for a forward (`after`) page it means newer messages exist
        beyond the page; otherwise it means older messages exist before it. The
        caller is expected to have already validated that the cursor id belongs
        to this conversation; if it somehow does not resolve we fall back to the
        latest page rather than erroring here.

        Pagination is keyset on the full ``(created_at, id)`` tuple that the
        result is ordered by - not on ``created_at`` alone - so messages that
        share an exact timestamp with the cursor (or with each other) are never
        skipped or double-counted across pages."""
        limit = min(max(limit, 1), 100)
        forward = after_id is not None
        pivot_id = before_id or after_id
        pivot_key: tuple[datetime, uuid.UUID] | None = None
        if pivot_id is not None:
            pivot = await self._execute(
                select(Message.created_at).where(
                    Message.id == pivot_id, Message.conversation_id == conversation_id
                )
            )
            pivot_created_at = pivot.scalar_one_or_none()
            if pivot_created_at is not None:
                pivot_key = (pivot_created_at, pivot_id)

        row_key = tuple_(Message.created_at, Message.id)
        base = select(Message).where(Message.conversation_id == conversation_id).options(
            *self._message_options()
        )

        if forward and pivot_key is not None:
            statement = base.where(row_key > pivot_key).order_by(
                Message.created_at.asc(), Message.id.asc()
            ).limit(limit)
            rows = list((await self._execute(statement)).scalars().all())
        elif before_id is not None and pivot_key is not None:
            statement = base.where(row_key < pivot_key).order_by(
                Message.created_at.desc(), Message.id.desc()
            ).limit(limit)
            rows = list(reversed((await self._execute(statement)).scalars().all()))
        else:
            statement = base.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)
            rows = list(reversed((await self._execute(statement)).scalars().all()))

        has_more = False
        if rows:
            if forward:
                probe = select(Message.id).where(
                    Message.conversation_id == conversation_id,
                    row_key > (rows[-1].created_at, rows[-1].id),
                )
            else:
                probe = select(Message.id).where(
                    Message.conversation_id == conversation_id,
                    row_key < (rows[0].created_at, rows[0].id),
                )
            has_more = (await self._execute(probe.limit(1))).scalar_one_or_none() is not None
        return rows, has_more

    async def latest_messages_for_conversations(
        self, conversation_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Message]:
        """One query -> newest message per conversation (sender eager-loaded),
        so the conversation list can render each last_message without an
        N+1."""
        ids = list({conversation_id for conversation_id in conversation_ids})
        if not ids:
            return {}
        statement = (
            select(Message)
            .where(Message.conversation_id.in_(ids))
            .order_by(
                Message.conversation_id,
                Message.created_at.desc(),
                Message.id.desc(),
            )
            .distinct(Message.conversation_id)
            .options(selectinload(Message.sender))
        )
        result = await self._execute(statement)
        return {message.conversation_id: message for message in result.scalars().all()}

    async def create_message(
        self,
        conversation: Conversation,
        sender_id: uuid.UUID,
        body: str | None,
        reply_to_message_id: uuid.UUID | None,
        attachments: list[dict] | None,
        preview: str | None,
    ) -> Message:
        now = datetime.now(timezone.utc)
        message = Message(
            conversation_id=conversation.id,
            sender_id=sender_id,
            body=body,
            reply_to_message_id=reply_to_message_id,
            attachments=attachments,
            created_at=now,
        )
        self._session.add(message)
        conversation.last_message_at = now
        conversation.last_message_preview = preview
        await self._commit()
        reloaded = await self.get_message(message.id)
        if reloaded is None:
            # Only possible if the row was deleted between commit and read.
            await self._refresh(message)
            return message
        return reloaded

    async def soft_delete_message(self, message: Message) -> Message:
        message.is_deleted = True
        message.body = None
        message.attachments = None
        message.edited_at = datetime.now(timezone.utc)
        await self._execute(delete(MessageReaction).where(MessageReaction.message_id == message.id))
        await self._commit()
        reloaded = await self.get_message(message.id)
        return reloaded if reloaded is not None else message

    # ------------------------------------------------------------------ #
    # Reactions
    # ------------------------------------------------------------------ #
    async def add_reaction(self, message_id: uuid.UUID, user_id: uuid.UUID, emoji: str) -> None:
        existing = await self._execute(
            select(MessageReaction.id).where(
                MessageReaction.message_id == message_id,
                MessageReaction.user_id == user_id,
                MessageReaction.emoji == emoji,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return
        self._session.add(
            MessageReaction(message_id=message_id, user_id=user_id, emoji=emoji)
        )
        try:
            await self._commit()
        except ConflictError:
            # A concurrent identical reaction won the race; the end state is the
            # one we wanted, so this is a no-op, not an error.
            pass

    async def remove_reaction(self, message_id: uuid.UUID, user_id: uuid.UUID, emoji: str) -> None:
        await self._execute(
            delete(MessageReaction).where(
                MessageReaction.message_id == message_id,
                MessageReaction.user_id == user_id,
                MessageReaction.emoji == emoji,
            )
        )
        await self._commit()

    async def reaction_groups(
        self, message_id: uuid.UUID, me_user_id: uuid.UUID
    ) -> list[tuple[str, int, bool]]:
        result = await self._execute(
            select(
                MessageReaction.emoji,
                func.count(MessageReaction.id),
                func.bool_or(MessageReaction.user_id == me_user_id),
            )
            .where(MessageReaction.message_id == message_id)
            .group_by(MessageReaction.emoji)
            .order_by(func.min(MessageReaction.created_at).asc())
        )
        return [(row[0], row[1], bool(row[2])) for row in result.all()]

    async def reaction_groups_for_messages(
        self, message_ids: Sequence[uuid.UUID], me_user_id: uuid.UUID
    ) -> dict[uuid.UUID, list[tuple[str, int, bool]]]:
        ids = list({message_id for message_id in message_ids})
        if not ids:
            return {}
        result = await self._execute(
            select(
                MessageReaction.message_id,
                MessageReaction.emoji,
                func.count(MessageReaction.id),
                func.bool_or(MessageReaction.user_id == me_user_id),
                func.min(MessageReaction.created_at).label("first_at"),
            )
            .where(MessageReaction.message_id.in_(ids))
            .group_by(MessageReaction.message_id, MessageReaction.emoji)
            .order_by("first_at")
        )
        grouped: dict[uuid.UUID, list[tuple[str, int, bool]]] = {}
        for message_id, emoji, count, reacted_by_me, _first_at in result.all():
            grouped.setdefault(message_id, []).append((emoji, count, bool(reacted_by_me)))
        return grouped
