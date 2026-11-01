"""The assistant's audit log: one row per model call, numbers only, and never able to
affect a reply. Offline: a fake provider and an in-memory store."""

import asyncio
import dataclasses
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database.assistant_usage_persistence import CallLogEntry
from database.models import AssistantCallLog
from schemas.assistant_schemas import ChatContext, ChatMessage, ChatRequest, ChatToolCall, OpenFile
from services.assistant_jobs import purge_call_log, register_assistant_jobs
from services.assistant_llm import (
    FinishedEvent,
    ProviderRateLimitedError,
    ProviderRequestTooLargeError,
    ProviderUnavailableError,
    StreamOpened,
    TextDelta,
    ToolCallEvent,
    UsageEvent,
    _usage_from_chunk,
)
from services.assistant_prompt_optimizer import optimize_conversation
from services.assistant_prompts import prefix_chars, system_prompt, tools_for
from services import assistant_service
from services.assistant_service import ActiveStreamRegistry, AssistantRateLimitedError, AssistantService
from tests.test_assistant import BODY, USER_ID, FakeProvider, FakeStore, body_with, client_for, make_service, parse_sse

SECRET_TEXT = "SECRET_USER_WORDS"
SECRET_CODE = "SECRET_FILE_CONTENT"
SECRET_ERROR = "SECRET_ERROR_TEXT"
SECRET_RESULT = "SECRET_TOOL_RESULT"
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION = MIGRATIONS / "015_assistant_call_log.sql"
ADDED_COLUMN = MIGRATIONS / "016_assistant_call_log_turn_chars.sql"
HUGE_FILE = "x" * 11_000


def post(service: AssistantService, **changes: Any) -> Any:
    return client_for(service).post("/api/assistant/chat", json=body_with(**changes))


def one_entry(store: FakeStore) -> CallLogEntry:
    assert len(store.call_log) == 1
    return store.call_log[0]


# ---- what gets written ------------------------------------------------------------------


def test_a_finished_call_writes_one_row_with_its_numbers() -> None:
    script = [StreamOpened(), TextDelta("Hi"), UsageEvent(input_tokens=120, output_tokens=30, cached_tokens=40), FinishedEvent("stop")]
    service, _, store, _ = make_service(FakeProvider(script))
    assert parse_sse(post(service, mode="agent").text)[-1][0] == "done"
    entry = one_entry(store)
    assert (entry.key.user_id, entry.key.provider, entry.key.model) == (USER_ID, "groq", "fake-model")
    assert (entry.mode, entry.outcome, entry.exact_usage) == ("agent", "completed", True)
    assert (entry.tokens.input_tokens, entry.tokens.output_tokens, entry.cached_tokens) == (120, 30, 40)
    assert entry.first_output_ms is not None and entry.first_output_ms >= 0
    assert entry.duration_ms >= 0
    assert entry.sizes.sent_chars > 0
    assert entry.sizes.system_chars > 0
    assert entry.sizes.tools_chars > 2, "agent mode offers tools"


def test_ask_mode_is_recorded_with_no_tool_size_beyond_the_empty_list() -> None:
    service, _, store, _ = make_service()
    post(service, mode="ask")
    entry = one_entry(store)
    assert entry.mode == "ask"
    assert entry.sizes.tools_chars == len("[]")


def test_an_error_mid_reply_is_recorded_with_what_it_really_spent() -> None:
    script = [
        StreamOpened(),
        ToolCallEvent(id="c", name="not_offered", arguments={}),
        UsageEvent(input_tokens=500, output_tokens=2000),
        FinishedEvent("tool_calls"),
    ]
    service, _, store, _ = make_service(FakeProvider(script))
    post(service)
    entry = one_entry(store)
    assert entry.outcome == "invalid_tool_call"
    assert (entry.tokens.input_tokens, entry.tokens.output_tokens, entry.exact_usage) == (500, 2000, True)


def test_a_reply_with_no_usage_is_marked_as_an_estimate() -> None:
    service, _, store, _ = make_service(FakeProvider([StreamOpened(), TextDelta("x" * 40), FinishedEvent("stop")]))
    post(service)
    entry = one_entry(store)
    assert entry.exact_usage is False
    assert entry.cached_tokens == 0
    assert entry.tokens.output_tokens == 10


def test_closing_a_stream_twice_writes_one_row() -> None:
    async def main() -> None:
        service, _, store, _ = make_service()
        stream = await service.open_chat(USER_ID, ChatRequest.model_validate(BODY))
        await stream.close()
        await stream.close()
        assert one_entry(store).outcome == "disconnected", "nothing read the reply"

    asyncio.run(main())


@pytest.mark.parametrize(
    ("failure", "status", "outcome"),
    [
        (ProviderRateLimitedError(5), 429, "refused_rate_limited"),
        (ProviderRequestTooLargeError(), 413, "refused_too_large"),
        (ProviderUnavailableError(), 503, "refused_unavailable"),
    ],
    ids=["rate_limited", "too_large", "unavailable"],
)
def test_a_call_the_provider_refused_is_recorded_with_no_tokens(failure: Exception, status: int, outcome: str) -> None:
    service, _, store, _ = make_service(FakeProvider([failure]))
    assert post(service).status_code == status
    entry = one_entry(store)
    assert entry.outcome == outcome
    assert (entry.tokens.total, entry.cached_tokens, entry.exact_usage, entry.first_output_ms) == (0, 0, False, None)
    assert entry.sizes.sent_chars > 0
    assert store.released == 1, "the refund still happens"


@pytest.mark.parametrize(
    ("limit", "store"),
    [
        ({"assistant_user_daily_calls": 0}, FakeStore()),
        ({"assistant_user_daily_tokens": 1}, FakeStore()),
        ({"assistant_global_daily_tokens": 1}, FakeStore()),
    ],
    ids=["user_calls", "user_tokens", "global_tokens"],
)
def test_a_call_stopped_by_our_own_limit_writes_no_row(limit: dict[str, int], store: FakeStore) -> None:
    # No model was called, and a logged row per refusal would let one user fill the table.
    service, provider, _, _ = make_service(store=store, **limit)
    assert post(service).status_code in (429, 503)
    assert provider.requests == []
    assert store.call_log == []


def test_a_loop_of_refused_calls_writes_one_row_per_window_but_refunds_them_all() -> None:
    now = [100.0]
    registry = ActiveStreamRegistry(clock=lambda: now[0])
    service, _, store, _ = make_service(FakeProvider([ProviderRateLimitedError(5)]), registry=registry)
    for _ in range(5):
        assert post(service).status_code == 429
    assert len(store.call_log) == 1
    assert store.released == 5
    now[0] += 61
    assert post(service).status_code == 429
    assert len(store.call_log) == 2


def test_the_refusal_window_is_per_user_and_forgets_old_users() -> None:
    now = [0.0]
    registry = ActiveStreamRegistry(clock=lambda: now[0])
    first, second = uuid.uuid4(), uuid.uuid4()
    assert registry.allow_unopened_log(first)
    assert registry.allow_unopened_log(second)
    assert not registry.allow_unopened_log(first)
    now[0] = 1000.0
    assert registry.allow_unopened_log(first)
    assert registry.allow_unopened_log(second)


def test_the_refusal_tracking_stays_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_service, "_UNOPENED_LOG_MAX_TRACKED", 3)
    now = [0.0]
    registry = ActiveStreamRegistry(clock=lambda: now[0])
    for _ in range(3):
        registry.allow_unopened_log(uuid.uuid4())
    now[0] = 1000.0
    registry.allow_unopened_log(uuid.uuid4())
    assert len(registry._unopened_logged) == 1


def test_a_reply_ended_by_the_time_limit_is_recorded_as_timed_out() -> None:
    service, provider, store, _ = make_service(FakeProvider([StreamOpened(), TextDelta("hi")], hang=True), max_stream_seconds=0.2)
    post(service)
    assert one_entry(store).outcome == "timed_out"
    assert provider.closed


def test_the_expiry_timer_records_timed_out_too() -> None:
    async def main() -> None:
        service, _, store, _ = make_service(FakeProvider([StreamOpened()], hang=True))
        stream = await service.open_chat(USER_ID, ChatRequest.model_validate(BODY))
        stream._expire()
        assert stream._expiry_task is not None
        await stream._expiry_task
        assert one_entry(store).outcome == "timed_out"

    asyncio.run(main())


def test_the_time_limit_does_not_change_a_finished_replys_outcome() -> None:
    async def main() -> None:
        service, _, store, _ = make_service()
        stream = await service.open_chat(USER_ID, ChatRequest.model_validate(BODY))
        _ = [event async for event in stream.events()]
        stream.mark_timed_out()
        await stream.close()
        assert one_entry(store).outcome == "completed"

    asyncio.run(main())


def test_a_call_cancelled_before_any_reply_is_logged_and_refunded() -> None:
    async def main() -> None:
        service, provider, store, registry = make_service(FakeProvider([], hang=True))
        opening = asyncio.create_task(service.open_chat(USER_ID, ChatRequest.model_validate(BODY)))
        await asyncio.sleep(0.01)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        await asyncio.sleep(0.01)
        assert one_entry(store).outcome == "cancelled_before_reply"
        assert store.released == 1
        assert provider.closed
        assert registry.acquire(USER_ID), "the slot is free again"

    asyncio.run(main())


def test_a_stuck_database_does_not_hold_up_a_refused_calls_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_service, "_REFUSAL_WAIT_SECONDS", 0.05)
    gate_holder: list[asyncio.Event] = []

    class StuckStore(FakeStore):
        async def release_call(self, key: Any, reserved: Any) -> None:
            await gate_holder[0].wait()
            await super().release_call(key, reserved)

    async def main() -> None:
        gate_holder.append(asyncio.Event())
        store = StuckStore()
        service, _, _, registry = make_service(FakeProvider([ProviderRateLimitedError(5)]), store)
        with pytest.raises(AssistantRateLimitedError):
            await asyncio.wait_for(service.open_chat(USER_ID, ChatRequest.model_validate(BODY)), 1)
        assert store.released == 0, "the refund is still waiting"
        assert registry.acquire(USER_ID), "but the user has their slot back"
        registry.release(USER_ID)
        gate_holder[0].set()
        await asyncio.sleep(0.01)
        assert store.released == 1, "and it finishes on its own"
        assert len(store.call_log) == 1

    asyncio.run(main())


def test_a_stuck_usage_write_does_not_hold_up_the_audit_row_or_its_duration() -> None:
    async def main() -> None:
        store = FakeStore()
        store.settle_gate = asyncio.Event()
        service, _, _, _ = make_service(store=store)
        stream = await service.open_chat(USER_ID, ChatRequest.model_validate(BODY))
        closing = asyncio.create_task(stream.close())
        await asyncio.sleep(0.3)
        entry = one_entry(store)
        assert entry.duration_ms < 250, "cleanup time is not reply time"
        assert store.recorded == []
        store.settle_gate.set()
        await closing
        assert store.recorded != []

    asyncio.run(main())


# ---- no text, ever ----------------------------------------------------------------------


def test_the_row_holds_no_text_from_the_user() -> None:
    body = {
        "mode": "agent",
        "messages": [
            {"role": "user", "content": SECRET_TEXT},
            {"role": "assistant", "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a.py"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": SECRET_RESULT},
        ],
        "context": {
            "language": "python",
            "files": ["a.py", "b.py"],
            "open_file": {"name": "b.py", "content": SECRET_CODE},
            "last_error": SECRET_ERROR,
        },
    }
    service, _, store, _ = make_service()
    assert client_for(service).post("/api/assistant/chat", json=body).status_code == 200
    stored = dataclasses.asdict(one_entry(store))
    assert not re.search("SECRET|a\\.py|b\\.py", repr(stored))
    # Every free-text column is one of ours, not the request's.
    assert stored["outcome"] == "completed"
    assert stored["mode"] == "agent"


# ---- never affects a reply --------------------------------------------------------------


def test_a_failed_audit_write_does_not_touch_the_reply_or_the_usage() -> None:
    store = FakeStore()
    store.fail_call_log = True
    service, _, _, _ = make_service(store=store)
    response = post(service)
    assert response.status_code == 200
    assert [name for name, _ in parse_sse(response.text)][-2:] == ["usage", "done"]
    assert store.recorded == [(120, 30)]


def test_a_failed_audit_write_does_not_change_a_refused_calls_status() -> None:
    store = FakeStore()
    store.fail_call_log = True
    service, _, _, _ = make_service(FakeProvider([ProviderRateLimitedError(5)]), store)
    response = post(service)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"
    assert store.released == 1


def test_a_failed_usage_write_does_not_lose_the_audit_row() -> None:
    store = FakeStore()
    store.fail_recording = True
    service, _, _, _ = make_service(store=store)
    assert post(service).status_code == 200
    assert one_entry(store).outcome == "completed"


def test_a_failed_audit_write_is_logged_with_no_details(caplog: pytest.LogCaptureFixture) -> None:
    store = FakeStore()
    store.fail_call_log = True
    service, _, _, _ = make_service(store=store)
    post(service)
    errors = [record.getMessage() for record in caplog.records if "call log" in record.getMessage()]
    assert errors == ["Could not write the assistant call log: RuntimeError"]


# ---- the sizes add up -------------------------------------------------------------------


def _tool_round(call_id: str, path: str, result: str) -> list[ChatMessage]:
    return [
        ChatMessage(role="assistant", tool_calls=[ChatToolCall(id=call_id, name="read_file", arguments={"path": path})]),
        ChatMessage(role="tool", tool_call_id=call_id, content=result),
    ]


OLD_TURN = [ChatMessage(role="user", content="first question"), ChatMessage(role="assistant", content="first answer " * 40)]
SHAPES: dict[str, tuple[list[ChatMessage], ChatContext]] = {
    "one_message": ([ChatMessage(role="user", content="please fix it")], ChatContext(language="python")),
    "open_file": (
        [ChatMessage(role="user", content="explain this")],
        ChatContext(language="python", files=["a.py"], open_file=OpenFile(name="a.py", content="print(1)\n" * 50)),
    ),
    "error_and_files": (
        [ChatMessage(role="user", content="why")],
        ChatContext(language="web", files=["a.js", "b.js"], last_error="Traceback\n" * 40),
    ),
    "history": ([*OLD_TURN, ChatMessage(role="user", content="and now?")], ChatContext(language="python")),
    "tool_round": (
        [ChatMessage(role="user", content="read a.py"), *_tool_round("c1", "a.py", "contents " * 300)],
        ChatContext(language="python", files=["a.py"]),
    ),
    "whole_read_of_open_file": (
        [ChatMessage(role="user", content="read a.py"), *_tool_round("c1", "a.py", "short")],
        ChatContext(language="python", files=["a.py"], open_file=OpenFile(name="a.py", content="short")),
    ),
    "huge_open_file_is_cut": (
        [ChatMessage(role="user", content="explain")],
        ChatContext(language="python", files=["a.py"], open_file=OpenFile(name="a.py", content=HUGE_FILE)),
    ),
    "ends_on_assistant": ([*OLD_TURN], ChatContext(language="python")),
    "no_user_at_all": (_tool_round("c1", "a.py", "text"), ChatContext(language="python")),
}


@pytest.mark.parametrize("mode", ["ask", "agent"])
@pytest.mark.parametrize("budget", [100_000, 5000, 600, 1], ids=["roomy", "default", "tight", "impossible"])
@pytest.mark.parametrize("shape", SHAPES.keys())
def test_the_parts_add_up_to_what_was_sent(shape: str, budget: int, mode: Any) -> None:
    messages, context = SHAPES[shape]
    optimized = optimize_conversation(messages, context, mode, budget)
    parts = dataclasses.asdict(optimized.parts)
    assert all(value >= 0 for value in parts.values()), parts
    assert sum(parts.values()) == optimized.optimized_chars, parts


def test_the_open_file_is_counted_only_when_it_is_really_sent() -> None:
    messages, context = SHAPES["open_file"]
    shown = optimize_conversation(messages, context, "ask", 100_000)
    assert shown.parts.file_chars == len(context.open_file.content)  # type: ignore[union-attr]
    # A budget this tight leaves the file out, so none of its characters count.
    hidden = optimize_conversation(messages, ChatContext(language="python", files=["a.py"], open_file=OpenFile(name="a.py", content=HUGE_FILE)), "ask", 1)
    assert hidden.parts.file_chars == 0
    assert HUGE_FILE not in hidden.messages[-1]["content"]


def test_a_file_already_read_whole_is_not_counted_twice() -> None:
    messages, context = SHAPES["whole_read_of_open_file"]
    optimized = optimize_conversation(messages, context, "agent", 100_000)
    assert optimized.parts.file_chars == 0
    assert optimized.parts.tool_result_chars == len("short")


def test_each_part_lands_in_its_own_column() -> None:
    messages = [*OLD_TURN, ChatMessage(role="user", content="read a.py"), *_tool_round("c1", "a.py", "R" * 500)]
    context = ChatContext(language="python", files=["a.py"], open_file=OpenFile(name="b.py", content="F" * 300), last_error="E" * 200)
    parts = optimize_conversation(messages, context, "agent", 100_000).parts
    assert parts.file_chars == 300
    assert parts.user_chars == len("read a.py")
    assert parts.tool_result_chars == 500
    assert parts.context_chars >= 200, "the error and the file list live here"
    assert parts.history_chars >= len(OLD_TURN[1].content or "")


def test_ask_mode_counts_flattened_results_as_tool_results() -> None:
    messages = [ChatMessage(role="user", content="read a.py"), *_tool_round("c1", "a.py", "R" * 500)]
    parts = optimize_conversation(messages, ChatContext(language="python"), "ask", 100_000).parts
    assert parts.tool_result_chars >= 500
    assert parts.history_chars == 0, "a single turn has no earlier history"
    assert parts.turn_assistant_chars > 0, "the note about the earlier tool call"


def test_a_large_edit_in_the_current_turn_is_not_blamed_on_history() -> None:
    edit = ChatToolCall(id="c1", name="write_file", arguments={"path": "a.py", "content": "W" * 3000})
    messages = [
        ChatMessage(role="user", content="rewrite a.py"),
        ChatMessage(role="assistant", tool_calls=[edit]),
        ChatMessage(role="tool", tool_call_id="c1", content="ok"),
    ]
    parts = optimize_conversation(messages, ChatContext(language="python"), "agent", 100_000).parts
    assert parts.history_chars == 0
    assert parts.turn_assistant_chars >= 3000


def test_a_user_message_that_looks_like_a_data_block_stays_in_history() -> None:
    fake = '<tool_result id="abc">not really</tool_result id="abc">'
    messages = [
        ChatMessage(role="user", content=fake),
        ChatMessage(role="assistant", content="ok"),
        ChatMessage(role="user", content="next"),
    ]
    parts = optimize_conversation(messages, ChatContext(language="python"), "ask", 100_000).parts
    assert parts.tool_result_chars == 0
    assert parts.history_chars == len(fake) + len("ok")


def test_system_and_tool_sizes_add_up_to_the_fixed_prefix() -> None:
    service, _, _, _ = make_service()
    payload = ChatRequest.model_validate(body_with(mode="agent"))
    sizes = service.build_request(payload).sizes
    language = payload.context.language
    assert sizes.system_chars + sizes.tools_chars == prefix_chars(system_prompt("agent", language), tools_for("agent", language))


# ---- cached tokens ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("usage", "cached"),
    [
        ({"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 7}}, 7),
        ({"prompt_tokens": 10, "completion_tokens": 2}, 0),
        ({"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": None}, 0),
        ({"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": "nonsense"}, 0),
        ({"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": "x"}}, 0),
    ],
    ids=["reported", "absent", "null", "not_an_object", "not_a_number"],
)
def test_cached_tokens_are_read_and_bad_values_become_zero(usage: dict[str, Any], cached: int) -> None:
    event = _usage_from_chunk({"usage": usage})
    assert event == UsageEvent(10, 2, cached)


def test_groq_cached_tokens_reach_the_row() -> None:
    from tests.test_assistant import finish_chunk, groq_provider, make_settings, sse_lines
    from services.assistant_service import ActiveStreamRegistry

    usage = {"prompt_tokens": 50, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 20}}
    content = sse_lines(finish_chunk("stop"), {"choices": [], "usage": usage})
    store = FakeStore()
    provider = groq_provider(lambda _: httpx.Response(200, content=content))
    service = AssistantService(make_settings(), provider, store, ActiveStreamRegistry())  # type: ignore[arg-type]
    client_for(service).post("/api/assistant/chat", json=BODY)
    assert one_entry(store).cached_tokens == 20


# ---- retention --------------------------------------------------------------------------


def test_the_purge_deletes_rows_older_than_the_retention_period() -> None:
    store = FakeStore()
    before = datetime.now(timezone.utc)
    asyncio.run(purge_call_log(store, 90))  # type: ignore[arg-type]
    assert store.purged_before is not None
    assert abs(store.purged_before - (before - timedelta(days=90))) < timedelta(seconds=5)


def test_a_failed_purge_is_logged_and_goes_no_further(caplog: pytest.LogCaptureFixture) -> None:
    store = FakeStore()
    store.fail_call_log = True
    asyncio.run(purge_call_log(store, 90))  # type: ignore[arg-type]
    assert "Assistant call log purge failed: RuntimeError" in caplog.text


def test_the_purge_job_is_registered_daily_and_survives_a_busy_moment() -> None:
    scheduler = AsyncIOScheduler()
    register_assistant_jobs(scheduler, FakeStore(), 90)  # type: ignore[arg-type]
    job = scheduler.get_job("assistant_call_log_purge")
    assert job is not None
    assert job.trigger.interval == timedelta(hours=24)
    assert job.misfire_grace_time == 3600
    assert job.coalesce is True


@pytest.mark.parametrize("days", [0, -1])
def test_no_retention_means_no_purge_job(days: int) -> None:
    scheduler = AsyncIOScheduler()
    register_assistant_jobs(scheduler, FakeStore(), days)  # type: ignore[arg-type]
    assert scheduler.get_jobs() == []


# ---- the migration file matches the model -----------------------------------------------


def test_the_migration_has_every_column_and_index_of_the_model() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    body = sql[sql.index("CREATE TABLE") : sql.index(");")]
    in_file = set(re.findall(r"^\s{4}([a-z_]+) ", body, re.MULTILINE))
    in_file |= set(re.findall(r"ADD COLUMN IF NOT EXISTS ([a-z_]+) ", ADDED_COLUMN.read_text(encoding="utf-8")))
    assert in_file == {column.name for column in AssistantCallLog.__table__.columns}
    for index in AssistantCallLog.__table__.indexes:
        assert f"IF NOT EXISTS {index.name} " in sql
