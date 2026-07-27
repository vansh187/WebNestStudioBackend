import json
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.exceptions import ConflictError, ForbiddenError, NotFoundError
from database.chat_persistence import ChatPersistence
from database.models import ChatThread, User
from services.chatbot_pipeline import COLLECTED_FIELD_KEYS
from services.chatbot_service import ChatbotService
from services.generation_service import GenerationService
from services.llm_provider import GenerationFailedError, LLMProvider

logger = logging.getLogger("webnest.chat")

WELCOME_MESSAGE = "Hi! I can build you a page right now, or help you plan out a project - what would you like to do?"
CLARIFY_MESSAGE = "Would you like me to build a page right now, or talk through a project for a build plan?"
PAGE_BUILDER_ACK = "Here's your generated page."

INTENT_SYSTEM_PROMPT = """Classify the user's message as exactly one word:
PAGE_REQUEST - they want a generated HTML page/website/landing page built right now.
ENQUIRY - they want to discuss/plan a project, get a build plan, or talk through requirements before anything is built.
UNCLEAR - neither is clearly the case.
Respond with only that one word, nothing else."""


class ChatOrchestratorService:
    """The single entry point the chat widget talks to. Owns the unified
    transcript (ChatThread/ChatMessage) and, on a thread's first real
    message, classifies intent and locks the thread into either the existing
    AI Page Builder (GenerationService, untouched) or the project-enquiry
    flow (ChatbotService) for the rest of its life."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._chats = ChatPersistence(session)
        self._generation = GenerationService(session=session, settings=settings)
        self._chatbot = ChatbotService(session=session, settings=settings)
        self._llm = LLMProvider(settings)

    async def start_thread(self, user: User) -> tuple[ChatThread, str]:
        thread = await self._chats.create_thread(user.id)
        await self._chats.append_message(thread.id, role="assistant", content=WELCOME_MESSAGE, mode="undecided")
        return thread, WELCOME_MESSAGE

    async def send_message(self, user: User, thread_id: uuid.UUID, message: str) -> dict:
        thread = await self._chats.get_by_id(thread_id)
        if thread is None:
            raise NotFoundError("Chat thread not found")
        if thread.user_id != user.id:
            raise ForbiddenError("This conversation does not belong to you")

        if thread.mode == "undecided":
            return await self._handle_undecided(user, thread, message)
        if thread.mode == "page_builder":
            return await self._handle_page_builder(user, thread, message)
        return await self._handle_enquiry(user, thread, message)

    async def generate_plan(self, user: User, thread_id: uuid.UUID):
        thread = await self._chats.get_by_id(thread_id)
        if thread is None:
            raise NotFoundError("Chat thread not found")
        if thread.user_id != user.id:
            raise ForbiddenError("This conversation does not belong to you")
        if thread.mode != "enquiry" or thread.enquiry_id is None:
            raise ConflictError("This conversation is not in project-enquiry mode yet")
        return await self._chatbot.generate_plan(user, thread.enquiry_id)

    async def get_thread_detail(self, user_id: uuid.UUID, thread_id: uuid.UUID) -> ChatThread:
        thread = await self._chats.get_with_messages(thread_id)
        if thread is None:
            raise NotFoundError("Chat thread not found")
        if thread.user_id != user_id:
            raise ForbiddenError("This conversation does not belong to you")
        return thread

    async def list_threads(self, user_id: uuid.UUID) -> list[tuple[ChatThread, int]]:
        return await self._chats.list_by_user(user_id)

    # ---- internals ----

    async def _handle_undecided(self, user: User, thread: ChatThread, message: str) -> dict:
        # Classification is itself an LLM call and happens before the thread
        # locks into either rate-limited feature below - without this, a
        # thread stuck in "undecided" mode could burn unlimited LLM quota.
        await self._chatbot.reject_if_over_limit(user.id)
        intent = await self._classify_intent(user.id, message)

        if intent == "PAGE_REQUEST":
            generation, _provider = await self._generation.generate(user.id, message)
            await self._chats.lock_mode(thread, "page_builder", generation_id=generation.id)
            await self._log_turn(thread.id, message, PAGE_BUILDER_ACK, "page_builder")
            return {
                "mode": "page_builder",
                "reply": PAGE_BUILDER_ACK,
                "html": generation.latest_html,
                "generation_id": generation.id,
            }

        if intent == "ENQUIRY":
            enquiry_thread, reply, ready_for_plan = await self._chatbot.start_thread_with_message(user, message)
            await self._chats.lock_mode(thread, "enquiry", enquiry_id=enquiry_thread.id)
            await self._log_turn(thread.id, message, reply, "enquiry")
            return {
                "mode": "enquiry",
                "reply": reply,
                "collected_fields": _collected_fields(enquiry_thread.collected_fields_json),
                "ready_for_plan": ready_for_plan,
            }

        await self._log_turn(thread.id, message, CLARIFY_MESSAGE, "undecided")
        return {"mode": "undecided", "reply": CLARIFY_MESSAGE}

    async def _handle_page_builder(self, user: User, thread: ChatThread, message: str) -> dict:
        generation, _provider = await self._generation.refine(user.id, thread.generation_id, message)
        reply = "Here's your updated page."
        await self._log_turn(thread.id, message, reply, "page_builder")
        return {"mode": "page_builder", "reply": reply, "html": generation.latest_html, "generation_id": generation.id}

    async def _handle_enquiry(self, user: User, thread: ChatThread, message: str) -> dict:
        enquiry_thread, reply, ready_for_plan = await self._chatbot.send_message(user.id, thread.enquiry_id, message)
        await self._log_turn(thread.id, message, reply, "enquiry")
        return {
            "mode": "enquiry",
            "reply": reply,
            "collected_fields": _collected_fields(enquiry_thread.collected_fields_json),
            "ready_for_plan": ready_for_plan,
        }

    async def _log_turn(self, chat_thread_id: uuid.UUID, user_message: str, assistant_reply: str, mode: str) -> None:
        await self._chats.append_message(chat_thread_id, role="user", content=user_message.strip(), mode=mode)
        await self._chats.append_message(chat_thread_id, role="assistant", content=assistant_reply, mode=mode)

    async def _classify_intent(self, user_id: uuid.UUID, message: str) -> str:
        try:
            raw, _provider = await self._llm.generate_text(INTENT_SYSTEM_PROMPT, message.strip())
        except GenerationFailedError as exc:
            # Provider failure - don't charge the user's quota for a call
            # that produced nothing (mirrors GenerationService's rule).
            logger.warning("Intent classification failed, defaulting to UNCLEAR: %s", exc)
            return "UNCLEAR"
        await self._chatbot.consume_rate_limit(user_id)
        cleaned = raw.strip().upper()
        if "PAGE_REQUEST" in cleaned:
            return "PAGE_REQUEST"
        if "ENQUIRY" in cleaned:
            return "ENQUIRY"
        return "UNCLEAR"


def _collected_fields(collected_fields_json: str) -> dict:
    try:
        data = json.loads(collected_fields_json or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {key: data.get(key) for key in (*COLLECTED_FIELD_KEYS, "contact_name", "contact_email", "contact_phone")}
