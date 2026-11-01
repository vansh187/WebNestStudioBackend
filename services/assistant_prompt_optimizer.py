"""Enhances what the user sent before it reaches the model, so a call uses fewer
tokens and needs fewer follow-up calls.

Deterministic and local on purpose: rewriting a prompt with another model call
would add latency and spend the tokens this is meant to save. A typical request
takes well under a millisecond, the longest allowed conversation a few. Code is
never reworded, only cut with a visible marker when it doesn't fit the input budget.

The output is OpenAI-style chat messages that always form a valid conversation,
whatever the browser sent."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from schemas.assistant_schemas import AssistantMode, ChatContext, ChatMessage, ChatToolCall

ProviderMessage = dict[str, Any]

CHARS_PER_TOKEN = 4
_MESSAGE_OVERHEAD_TOKENS = 4
_MAX_LISTED_FILES = 40
_MAX_MENTIONED_FILES = 5
_LONG_ARGUMENT_CHARS = 200
_CONTINUE_TEXT = "Continue."

_COURTESY = (
    r"(?:please|pls|plz|kindly|(?:can|could|would|will) you(?: please| kindly| pls)?"
    r"|i (?:want|need|would like)(?: you)? to)"
)
_GREETING = r"(?:hi+|hello|hey+|good (?:morning|afternoon|evening))"
# A dot only counts when a space or the end follows, so hello.py stays whole. A greeting
# only counts when punctuated or leading into a courtesy, so "hello world" stays whole.
_LEADING_FILLER = re.compile(
    rf"^(?:{_GREETING}(?:[,!]+\s*|\.+(?:\s+|$)|\s+(?={_COURTESY}\b)|\s*$)"
    rf"|{_COURTESY}(?:[\s,]+|[.!]+(?:\s+|$)|$))+",
    re.IGNORECASE,
)
# Only after punctuation: "add a button that says thanks" keeps its last word.
_TRAILING_FILLER = re.compile(
    r"(?<=[,.!?\n])\s*(?:thanks|thank you|thx|please|pls|plz)(?: (?:so|very) much)?[\s.!]*$",
    re.IGNORECASE,
)
_CODE_FENCE = re.compile(r"(```.*?(?:```|\Z))", re.DOTALL)
_BLANK_LINES = re.compile(r"\n{3,}")
_WORDS = re.compile(r"[\w./-]+")
_FIX_WORDS = re.compile(
    r"\b(?:fix|bug|error|errors|broken|crash\w*|fail\w*|traceback|exception|not working|doesn'?t work|issue)\b",
    re.IGNORECASE,
)
_QUESTION_START = re.compile(
    r"^(?:(?:what|why|how|when|where|which|who|explain|describe)\b|(?:is|are|does|do|should)\b.*\?)",
    re.IGNORECASE | re.DOTALL,
)
_CHANGE_WORDS = re.compile(
    r"\b(?:fix|add|create|build|make|implement|change|update|remove|delete|rename|refactor|write)\b",
    re.IGNORECASE,
)
_VAGUE_PROMPT_CHARS = 40
_BLOCK_ID_CHARS = 16
_OLDER_OUTPUT_NOTE = "Older output; read it again if you still need it"
_NEWEST_OUTPUT_NOTE = "Cut to save space"
_FLAT_OUTPUT_NOTE = "Part of this earlier output is no longer available"


@dataclass(frozen=True)
class _Limits:
    """How much of each kind of text one attempt keeps, in characters."""

    older_turn_tool: int
    current_turn_tool: int
    latest_tool: int
    open_file: int
    last_error: int


# Tried in order until the conversation fits. Later attempts give up history
# first, then the open file, and only last the newest tool result.
_TIERS: tuple[_Limits, ...] = (
    _Limits(older_turn_tool=400, current_turn_tool=4000, latest_tool=16_000, open_file=12_000, last_error=2000),
    _Limits(older_turn_tool=200, current_turn_tool=1000, latest_tool=16_000, open_file=6000, last_error=1500),
    _Limits(older_turn_tool=200, current_turn_tool=400, latest_tool=8000, open_file=0, last_error=1000),
    _Limits(older_turn_tool=200, current_turn_tool=200, latest_tool=4000, open_file=0, last_error=500),
)


@dataclass(frozen=True)
class OptimizedConversation:
    messages: list[ProviderMessage]
    estimated_tokens: int
    original_chars: int
    optimized_chars: int


def _strip_line_ends(text: str) -> str:
    # By line, not by regex: a regex here backtracks quadratically on a long run of spaces.
    # The last piece isn't a finished line: it may run up to a code fence.
    lines = text.split("\n")
    return "\n".join([*(line.rstrip(" \t") for line in lines[:-1]), lines[-1]])


def tidy_user_text(text: str) -> str:
    """Drops greetings, courtesy words and extra blank lines. Fenced code is untouched."""
    parts = _CODE_FENCE.split(text)
    for index in range(0, len(parts), 2):  # even parts are outside the fences
        parts[index] = _BLANK_LINES.sub("\n\n", _strip_line_ends(parts[index]))
    tidied = "".join(parts).strip()
    without_filler = _TRAILING_FILLER.sub("", _LEADING_FILLER.sub("", tidied, count=1)).rstrip(" ,\n")
    return without_filler or tidied


def task_hint(text: str, has_error: bool) -> str:
    """One line that steers an agent to the fewest calls. A wrong guess costs little:
    the hint only says where to start, and the user's own words still follow it."""
    if _QUESTION_START.search(text) and not _CHANGE_WORDS.search(text):
        return "Question: answer directly. Don't change files unless asked."
    if _FIX_WORDS.search(text) or (has_error and len(text) <= _VAGUE_PROMPT_CHARS):
        return "Bug fix: start from the error, make the smallest fix, then run_project to confirm."
    return "Change: read only the files needed, make the smallest edits, then run_project to confirm."


def mentioned_files(text: str, files: list[str]) -> list[str]:
    """Project files the user named, by path or by file name. Whole words only, so
    a file named `a` doesn't match every prompt with an "a" in it."""
    words = {word.rstrip(".") for word in _WORDS.findall(text.lower())}
    words |= {word.rsplit("/", 1)[-1] for word in words}
    found = [name for name in files if name.lower() in words or name.rsplit("/", 1)[-1].lower() in words]
    return found[:_MAX_MENTIONED_FILES]


def cut_middle(text: str, limit: int, note: str) -> str:
    """Keeps the start and end of `text`, with a marker where the middle was."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n[... {len(text) - limit} characters cut. {note} ...]\n{text[-tail:]}"


def _data_block(tag: str, body: str) -> str:
    """Untrusted text in a block only its own closing tag ends. The id is a hash of the
    text, so the text can't contain it, and nothing in the text has to be altered. The
    same text gets the same id, which keeps Groq's prompt cache working."""
    block_id = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:_BLOCK_ID_CHARS]
    return f'<{tag} id="{block_id}">\n{body}\n</{tag} id="{block_id}">'


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _repair(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Gives every tool call exactly one result right after it, and drops results
    with no call. The browser may have trimmed its history anywhere, or the user may
    have sent a new message while calls were pending, and Groq rejects either shape."""
    repaired: list[ChatMessage] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        if message.role == "tool":
            continue
        repaired.append(message)
        if not message.tool_calls:
            continue
        results: dict[str, ChatMessage] = {}
        while index < len(messages) and messages[index].role == "tool":
            result = messages[index]
            results.setdefault(result.tool_call_id or "", result)
            index += 1
        for call in message.tool_calls:
            repaired.append(results.get(call.id) or ChatMessage(role="tool", tool_call_id=call.id, content="Not run.", is_error=True))
    if not any(message.role == "user" for message in repaired):
        repaired.insert(0, ChatMessage(role="user", content=_CONTINUE_TEXT))
    return repaired


def _split_turns(messages: list[ChatMessage]) -> list[list[ChatMessage]]:
    """A turn starts at a user message, so dropping whole turns keeps tool calls paired."""
    turns: list[list[ChatMessage]] = []
    for message in messages:
        if message.role == "user" or not turns:
            turns.append([])
        turns[-1].append(message)
    return turns


def _last_tool_round(turn: list[ChatMessage]) -> int:
    """Where the newest tool round starts: the last assistant message with tool calls."""
    return max((i for i, message in enumerate(turn) if message.role == "assistant" and message.tool_calls), default=-1)


def _tool_limit(position: int, last_round: int, turn_is_current: bool, limits: _Limits) -> int:
    if not turn_is_current:
        return limits.older_turn_tool
    return limits.latest_tool if position > last_round else limits.current_turn_tool


def _has_whole_read(turn: list[ChatMessage], path: str, limits: _Limits) -> bool:
    """True when the model will see all of `path` from a read_file since the user's last
    message: the read must have worked, and its result must not be cut when sent."""
    call_ids = {
        call.id
        for message in turn
        for call in message.tool_calls or []
        if call.name == "read_file" and call.arguments.get("path") == path
    }
    last_round = _last_tool_round(turn)
    return any(
        message.role == "tool"
        and message.tool_call_id in call_ids
        and not message.is_error
        and len(message.content or "") <= _tool_limit(position, last_round, True, limits)
        for position, message in enumerate(turn)
    )


def _context_block(context: ChatContext, turn: list[ChatMessage], mentioned: list[str], limits: _Limits, mode: AssistantMode) -> str:
    lines = [f"language: {context.language}"]
    if context.files:
        # The files most likely to matter first, so the cap never hides them.
        open_name = context.open_file.name if context.open_file else None
        priority = [name for name in context.files if name in mentioned or name == open_name]
        rest = [name for name in context.files if name not in priority]
        listed = [*priority, *rest][:_MAX_LISTED_FILES]
        more = len(context.files) - len(listed)
        lines.append("files: " + ", ".join(_one_line(name) for name in listed) + (f" (+{more} more)" if more else ""))
    if mentioned:
        lines.append("files_named: " + ", ".join(_one_line(name) for name in mentioned))
    if context.open_file is not None:
        name = _one_line(context.open_file.name)
        # Not in Ask mode: flattened history is cut, and a full-file answer needs all of it.
        if mode == "agent" and _has_whole_read(turn, context.open_file.name, limits):
            lines.append(f"open_file: {name} (content is in the read_file result below)")
        elif limits.open_file == 0:
            lines.append(f"open_file: {name} (content left out to save space)")
        else:
            note = (
                "You have not seen this part, so don't use a path-labelled block for this file"
                if mode == "ask"
                else "Use read_file for all of it"
            )
            lines.append(f"open_file: {name}\n{cut_middle(context.open_file.content, limits.open_file, note)}")
    if context.last_error:
        # A traceback's last lines say the most.
        error = context.last_error
        if len(error) > limits.last_error:
            error = f"[... earlier output cut ...]\n{error[-limits.last_error:]}"
        lines.append(f"last_error:\n{error}")
    return _data_block("context", "\n".join(lines))


def _shrink_argument(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _LONG_ARGUMENT_CHARS:
        return f"{value[:_LONG_ARGUMENT_CHARS]}[... cut ...]"
    return value


def _call_arguments(call: ChatToolCall, keep_full: bool) -> dict[str, Any]:
    if keep_full:
        return call.arguments
    return {key: _shrink_argument(value) for key, value in call.arguments.items()}


def _tool_text(message: ChatMessage, limit: int, note: str) -> str:
    text = cut_middle(message.content or "", limit, note)
    return f"ERROR: {text}" if message.is_error else text


def _render_tool_turn(turn: list[ChatMessage], turn_is_current: bool, limits: _Limits) -> list[ProviderMessage]:
    """Assistant and tool messages in the provider's tool-call format."""
    last_assistant = _last_tool_round(turn)
    rendered: list[ProviderMessage] = []
    for position, message in enumerate(turn):
        if message.role == "assistant":
            entry: ProviderMessage = {"role": "assistant", "content": message.content or ""}
            if message.tool_calls:
                keep_full = turn_is_current and position == last_assistant
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(_call_arguments(call, keep_full), ensure_ascii=False)},
                    }
                    for call in message.tool_calls
                ]
            rendered.append(entry)
        elif message.role == "tool":
            limit = _tool_limit(position, last_assistant, turn_is_current, limits)
            note = _NEWEST_OUTPUT_NOTE if turn_is_current and position > last_assistant else _OLDER_OUTPUT_NOTE
            rendered.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": _tool_text(message, limit, note)})
    return rendered


def _render_flat_turn(turn: list[ChatMessage], turn_is_current: bool, limits: _Limits) -> list[ProviderMessage]:
    """Ask mode has no tools, so earlier tool activity becomes plain text. That is valid
    whether or not the provider accepts tool messages without tools. A result is untrusted,
    so it goes in a data block like the context, not in the assistant's own words."""
    rendered: list[ProviderMessage] = []
    limit = limits.current_turn_tool if turn_is_current else limits.older_turn_tool
    for message in turn:
        if message.role == "assistant":
            pieces = [message.content] if message.content else []
            for call in message.tool_calls or []:
                arguments = json.dumps(_call_arguments(call, keep_full=False), ensure_ascii=False)
                pieces.append(f"[earlier tool call: {call.name} {arguments}]")
            rendered.append({"role": "assistant", "content": "\n".join(pieces)})
        elif message.role == "tool":
            rendered.append({"role": "user", "content": _data_block("tool_result", _tool_text(message, limit, _FLAT_OUTPUT_NOTE))})
    return rendered


def _render(
    turns: list[list[ChatMessage]],
    context: ChatContext,
    mode: AssistantMode,
    limits: _Limits,
    mentioned: list[str],
    dropped_turns: int,
) -> list[ProviderMessage]:
    rendered: list[ProviderMessage] = []
    for position, turn in enumerate(turns):
        turn_is_current = position == len(turns) - 1
        first = turn[0]
        if first.role == "user":
            text = first.content or ""
            if turn_is_current:
                text = _current_user_text(text, turn, context, mode, limits, mentioned, dropped_turns)
            rendered.append({"role": "user", "content": text})
            rest = turn[1:]
        else:
            rest = turn
        render_turn = _render_tool_turn if mode == "agent" else _render_flat_turn
        rendered.extend(render_turn(rest, turn_is_current, limits))
    if rendered[-1]["role"] == "assistant" and not rendered[-1].get("tool_calls"):
        # A conversation ending on the assistant would be taken as text to continue.
        rendered.append({"role": "user", "content": _CONTINUE_TEXT})
    return rendered


def _current_user_text(
    text: str,
    turn: list[ChatMessage],
    context: ChatContext,
    mode: AssistantMode,
    limits: _Limits,
    mentioned: list[str],
    dropped_turns: int,
) -> str:
    pieces = [_context_block(context, turn, mentioned, limits, mode)]
    if mode == "agent":
        pieces.append(task_hint(text, bool(context.last_error)))
    if dropped_turns:
        pieces.append("Earlier messages in this chat were left out to save space.")
    pieces.append(text)
    return "\n".join(pieces)


def _estimate_tokens(messages: list[ProviderMessage]) -> int:
    total = 0
    for message in messages:
        total += _MESSAGE_OVERHEAD_TOKENS + len(message.get("content") or "") // CHARS_PER_TOKEN
        for call in message.get("tool_calls") or []:
            total += (len(call["function"]["name"]) + len(call["function"]["arguments"])) // CHARS_PER_TOKEN
    return total


def _total_chars(messages: list[ChatMessage], context: ChatContext) -> int:
    total = sum(len(name) for name in context.files)
    total += len(context.open_file.content) if context.open_file else 0
    total += len(context.last_error or "")
    for message in messages:
        total += len(message.content or "")
        for call in message.tool_calls or []:
            total += len(json.dumps(call.arguments, ensure_ascii=False))
    return total


def _without_oldest_round(turn: list[ChatMessage]) -> list[ChatMessage] | None:
    """The turn minus its oldest tool round (a call and its results), or None when
    only the newest round is left. A turn always starts with its user message here."""
    rounds = [index for index, message in enumerate(turn) if message.role == "assistant"]
    if len(rounds) < 2:
        return None
    return [*turn[: rounds[0]], *turn[rounds[1] :]]


def _tidied(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Done once here, not on each of the many attempts to fit the budget."""
    return [
        message.model_copy(update={"content": tidy_user_text(message.content or "")}) if message.role == "user" else message
        for message in messages
    ]


def _fit_to_budget(turns: list[list[ChatMessage]], context: ChatContext, mode: AssistantMode, budget: int) -> list[ProviderMessage]:
    mentioned = mentioned_files(turns[-1][0].content or "", context.files)
    rendered: list[ProviderMessage] = []
    # First give up the oldest turns, with history trimmed but the current turn whole.
    for start in range(len(turns)):
        rendered = _render(turns[start:], context, mode, _TIERS[0], mentioned, start)
        if _estimate_tokens(rendered) <= budget:
            return rendered

    # The current turn alone is too big: trim its text, keeping the newest result longest.
    current = turns[-1]
    dropped = len(turns) - 1
    for limits in _TIERS[1:-1]:
        rendered = _render([current], context, mode, limits, mentioned, dropped)
        if _estimate_tokens(rendered) <= budget:
            return rendered

    # Still too big, as in a long agent task: give up its oldest tool rounds.
    remaining = _without_oldest_round(current)
    while remaining is not None:
        current = remaining
        dropped += 1
        rendered = _render([current], context, mode, _TIERS[-2], mentioned, dropped)
        if _estimate_tokens(rendered) <= budget:
            return rendered
        remaining = _without_oldest_round(current)

    # Last resort. If even this is too big, the provider's refusal becomes our 413.
    return _render([current], context, mode, _TIERS[-1], mentioned, dropped)


def optimize_conversation(
    messages: list[ChatMessage],
    context: ChatContext,
    mode: AssistantMode,
    max_input_tokens: int,
    prefix_tokens: int = 0,
) -> OptimizedConversation:
    """The conversation to send: history trimmed by age, the newest user message
    tidied and given its context, and the whole fitted to `max_input_tokens`."""
    budget = max(max_input_tokens - prefix_tokens, 1)
    rendered = _fit_to_budget(_split_turns(_tidied(_repair(messages))), context, mode, budget)
    optimized_chars = sum(
        len(message.get("content") or "") + sum(len(call["function"]["arguments"]) for call in message.get("tool_calls") or [])
        for message in rendered
    )
    return OptimizedConversation(
        messages=rendered,
        estimated_tokens=_estimate_tokens(rendered),
        original_chars=_total_chars(messages, context),
        optimized_chars=optimized_chars,
    )
