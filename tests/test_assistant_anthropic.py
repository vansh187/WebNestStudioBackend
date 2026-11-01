"""Claude Haiku behind the assistant, and Groq as its fallback. The real anthropic SDK runs
against a fake HTTP transport, so its real parsing and errors are tested, with no real calls."""

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from core.exceptions import PayloadTooLargeError
from schemas.assistant_schemas import ChatContext, ChatMessage, ChatRequest, ChatToolCall
from services import assistant_service
from services.assistant_llm import (
    AnthropicAssistantProvider,
    FinishedEvent,
    GroqAssistantProvider,
    ProviderEvent,
    ProviderRateLimitedError,
    ProviderRequest,
    ProviderRequestTooLargeError,
    ProviderStreamError,
    ProviderUnavailableError,
    StreamOpened,
    TextDelta,
    ToolCallEvent,
    UsageEvent,
    anthropic_messages,
    build_assistant_fallback,
    build_assistant_provider,
    provider_has_key,
)
from services.assistant_prompt_optimizer import optimize_conversation
from services.assistant_prompts import system_prompt, tools_for
from services.assistant_service import ActiveStreamRegistry, AssistantRateLimitedError, AssistantService
from tests.test_assistant import BODY, USER_ID, FakeProvider, FakeStore, client_for, make_settings, parse_sse

API_KEY = "sk-placeholder-not-a-real-key"
MODEL = "claude-haiku-5-5"
REQUEST = ProviderRequest(system="Be brief.", messages=[{"role": "user", "content": "hi"}], tools=[], max_output_tokens=100)


def settings(**changes: Any) -> SimpleNamespace:
    return make_settings(assistant_provider="anthropic", assistant_anthropic_api_key=API_KEY, assistant_anthropic_model=MODEL, **changes)


def sse(*events: dict[str, Any]) -> bytes:
    return b"".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events)


def message_start(input_tokens: int = 100, written: int = 0, read: int = 0) -> dict[str, Any]:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 1, "cache_creation_input_tokens": written, "cache_read_input_tokens": read},
        },
    }


def text_block(text: str, index: int = 0) -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": index},
    ]


def tool_block(call_id: str, name: str, *pieces: str, index: int = 0) -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}},
        *[{"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": piece}} for piece in pieces],
        {"type": "content_block_stop", "index": index},
    ]


def message_end(stop_reason: str = "end_turn", output_tokens: int = 20) -> list[dict[str, Any]]:
    return [
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": output_tokens}},
        {"type": "message_stop"},
    ]


def provider_for(handler: Callable[[httpx2.Request], httpx2.Response]) -> AnthropicAssistantProvider:
    client = anthropic.AsyncAnthropic(
        api_key=API_KEY,
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicAssistantProvider(settings(), client)  # type: ignore[arg-type]


def stream_provider(*events: dict[str, Any], seen: list[dict[str, Any]] | None = None) -> AnthropicAssistantProvider:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(*events))

    return provider_for(handler)


def run(provider: AnthropicAssistantProvider, request: ProviderRequest = REQUEST) -> list[Any]:
    """Every event, and an exception as the last item if the stream raised one."""

    async def main() -> list[Any]:
        events: list[Any] = []
        try:
            async for event in provider.stream(request):
                events.append(event)
        except Exception as exc:  # the test looks at which one
            events.append(exc)
        await provider.aclose()
        return events

    return asyncio.run(main())


# ---- the request ------------------------------------------------------------------------


def test_the_request_uses_haiku_with_thinking_off_and_no_sampling_settings() -> None:
    seen: list[dict[str, Any]] = []
    run(stream_provider(message_start(), *message_end(), seen=seen))
    sent = seen[0]
    assert sent["model"] == "claude-haiku-5-5"
    assert sent["max_tokens"] == 100
    assert sent["system"] == "Be brief."
    assert sent["thinking"] == {"type": "disabled"}
    assert sent["stream"] is True
    assert "temperature" not in sent and "top_p" not in sent and "top_k" not in sent
    assert "tools" not in sent


@pytest.mark.parametrize("language", ["python", "web", "backend"])
def test_agent_tools_are_converted_to_claudes_format(language: str) -> None:
    seen: list[dict[str, Any]] = []
    tools = tools_for("agent", language)  # type: ignore[arg-type]
    run(stream_provider(message_start(), *message_end(), seen=seen), ProviderRequest("s", [{"role": "user", "content": "x"}], tools, 10))
    sent = seen[0]["tools"]
    assert len(sent) == len(tools)
    for converted, original in zip(sent, tools):
        assert converted == {
            "name": original["function"]["name"],
            "description": original["function"]["description"],
            "input_schema": original["function"]["parameters"],
        }


def test_tool_history_becomes_content_blocks_in_one_user_turn() -> None:
    messages = [
        {"role": "user", "content": "read both"},
        {
            "role": "assistant",
            "content": "Reading.",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
                {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "b.py"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "AAA"},
        {"role": "tool", "tool_call_id": "c2", "content": "BBB"},
    ]
    assert anthropic_messages(messages) == [
        {"role": "user", "content": [{"type": "text", "text": "read both"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Reading."},
                {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "a.py"}},
                {"type": "tool_use", "id": "c2", "name": "read_file", "input": {"path": "b.py"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "AAA"},
                {"type": "tool_result", "tool_use_id": "c2", "content": "BBB"},
            ],
        },
    ]


def test_an_id_from_another_provider_is_made_safe_on_both_sides() -> None:
    messages = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call:1/é", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call:1/é", "content": "r"},
    ]
    converted = anthropic_messages(messages)
    assert converted[1]["content"][0]["id"] == converted[2]["content"][0]["tool_use_id"] == "call_1__"


@pytest.mark.parametrize(
    ("messages", "first_role"),
    [
        ([{"role": "assistant", "content": "old"}, {"role": "user", "content": "new"}], "user"),
        ([{"role": "assistant", "content": ""}], "user"),
        ([], "user"),
    ],
    ids=["starts_on_assistant", "only_blank", "empty"],
)
def test_a_conversation_always_starts_with_the_user(messages: list[dict[str, Any]], first_role: str) -> None:
    assert anthropic_messages(messages)[0]["role"] == first_role


def test_blank_text_is_dropped_and_empty_tool_output_is_filled() -> None:
    converted = anthropic_messages(
        [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "   ", "tool_calls": [{"id": "c", "function": {"name": "n", "arguments": "not json"}}]},
            {"role": "tool", "tool_call_id": "c", "content": ""},
        ]
    )
    assert converted[1]["content"] == [{"type": "tool_use", "id": "c", "name": "n", "input": {}}]
    assert converted[2]["content"][0]["content"] == "(no output)"


def test_flattened_ask_history_merges_so_roles_alternate() -> None:
    converted = anthropic_messages(
        [
            {"role": "user", "content": "one"},
            {"role": "user", "content": "<tool_result>x</tool_result>"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "two"},
        ]
    )
    assert [message["role"] for message in converted] == ["user", "assistant", "user"]
    assert len(converted[0]["content"]) == 2


# ---- the reply --------------------------------------------------------------------------


def test_text_streams_and_every_billed_field_is_counted() -> None:
    events = run(stream_provider(message_start(100, written=30, read=70), *text_block("Hello"), *message_end(output_tokens=20)))
    assert events == [StreamOpened(), UsageEvent(200, 1, 70, partial=True), TextDelta("Hello"), UsageEvent(200, 20, 70), FinishedEvent("stop")]


def test_tool_calls_are_assembled_from_their_pieces() -> None:
    events = run(stream_provider(message_start(), *text_block("Reading."), *tool_block("t1", "read_file", '{"pa', 'th": "a.py"}', index=1), *message_end("tool_use")))
    assert events[-2:] == [ToolCallEvent("t1", "read_file", {"path": "a.py"}), FinishedEvent("tool_calls")]


def test_a_tool_with_no_input_is_an_empty_object() -> None:
    assert ToolCallEvent("t1", "list_files", {}) in run(stream_provider(message_start(), *tool_block("t1", "list_files"), *message_end("tool_use")))


def test_broken_tool_json_is_an_invalid_tool_call_and_still_counts_its_tokens() -> None:
    events = run(stream_provider(message_start(), *tool_block("t1", "read_file", '{"path":'), *message_end("tool_use")))
    assert UsageEvent(100, 20, 0) in events
    assert isinstance(events[-1], ProviderStreamError) and events[-1].code == "invalid_tool_call"


def test_tool_json_that_is_not_an_object_is_refused() -> None:
    events = run(stream_provider(message_start(), *tool_block("t1", "read_file", "[1]"), *message_end("tool_use")))
    assert isinstance(events[-1], ProviderStreamError) and events[-1].code == "invalid_tool_call"


def test_a_reply_cut_off_by_the_limit_sends_no_tool_calls() -> None:
    events = run(stream_provider(message_start(), *tool_block("t1", "write_file", '{"path": "a.py"}'), *message_end("max_tokens")))
    assert events[-1] == FinishedEvent("length")
    assert not any(isinstance(event, ToolCallEvent) for event in events)


def test_a_refusal_is_a_clear_error_and_still_counts_its_tokens() -> None:
    events = run(stream_provider(message_start(), *text_block("Sorry"), *message_end("refusal")))
    assert UsageEvent(100, 20, 0) in events
    assert isinstance(events[-1], ProviderStreamError) and events[-1].code == "refused"


def test_thinking_never_reaches_the_client() -> None:
    thinking = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "SECRET THOUGHT"}},
        {"type": "content_block_stop", "index": 0},
    ]
    events = run(stream_provider(message_start(), *thinking, *text_block("Hi", 1), *message_end()))
    assert events == [StreamOpened(), UsageEvent(100, 1, 0, partial=True), TextDelta("Hi"), UsageEvent(100, 20, 0), FinishedEvent("stop")]


def test_a_client_leaving_mid_reply_is_charged_the_real_input_and_what_it_received() -> None:
    async def main() -> None:
        provider = stream_provider(message_start(5000, written=100, read=900), *text_block("x" * 400), *message_end(output_tokens=2000))
        store = FakeStore()
        service = AssistantService(settings(), provider, store, ActiveStreamRegistry())  # type: ignore[arg-type]
        stream = await service.open_chat(USER_ID, ChatRequest.model_validate(BODY))
        events = stream.events()
        while b"event: text" not in await anext(events):
            pass
        await events.aclose()
        await stream.close()
        await provider.aclose()
        assert store.recorded == [(6000, 100)], "real input, and output estimated from the 400 characters sent"
        assert store.call_log[0].exact_usage is False
        assert store.call_log[0].cached_tokens == 900

    asyncio.run(main())


def test_a_partial_usage_is_replaced_by_the_final_one() -> None:
    provider = stream_provider(message_start(100), *text_block("x" * 4000), *message_end(output_tokens=20))
    store = FakeStore()
    service = AssistantService(settings(), provider, store, ActiveStreamRegistry())  # type: ignore[arg-type]
    client_for(service).post("/api/assistant/chat", json=BODY)
    assert store.recorded == [(100, 20)], "the provider's real output, not the estimate"
    assert store.call_log[0].exact_usage is True


def test_pings_are_ignored() -> None:
    assert TextDelta("Hi") in run(stream_provider({"type": "ping"}, message_start(), {"type": "ping"}, *text_block("Hi"), *message_end()))


def test_a_stream_that_ends_early_is_an_error_after_counting() -> None:
    events = run(stream_provider(message_start(), *text_block("Hi")))
    assert UsageEvent(100, 1, 0) in events
    assert isinstance(events[-1], ProviderStreamError) and events[-1].code == "provider_error"


def test_an_empty_stream_is_unavailable_so_it_can_fall_back() -> None:
    assert isinstance(run(stream_provider())[-1], ProviderUnavailableError)


def error_body(kind: str, message: str = "SECRET quoted prompt") -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}


@pytest.mark.parametrize(
    ("status", "kind", "expected"),
    [
        (429, "rate_limit_error", ProviderRateLimitedError),
        (413, "request_too_large", ProviderRequestTooLargeError),
        (400, "invalid_request_error", ProviderUnavailableError),
        (401, "authentication_error", ProviderUnavailableError),
        (402, "billing_error", ProviderUnavailableError),
        (403, "permission_error", ProviderUnavailableError),
        (404, "not_found_error", ProviderUnavailableError),
        (500, "api_error", ProviderUnavailableError),
        (529, "overloaded_error", ProviderUnavailableError),
    ],
    ids=["429", "413", "400", "401", "402", "403", "404", "500", "529"],
)
def test_a_refusal_before_any_reply_is_a_provider_error_with_our_own_words(status: int, kind: str, expected: type[Exception], caplog: pytest.LogCaptureFixture) -> None:
    events = run(provider_for(lambda _: httpx2.Response(status, json=error_body(kind))))
    assert isinstance(events[-1], expected)
    assert len(events) == 1
    assert "SECRET" not in str(events[-1])
    assert "SECRET" not in caplog.text


def test_running_out_of_credit_can_fall_back() -> None:
    # The API reports this as a 400 with a message, which is deliberately not read.
    events = run(provider_for(lambda _: httpx2.Response(400, json=error_body("invalid_request_error", "Your credit balance is too low"))))
    assert isinstance(events[-1], ProviderUnavailableError)


def test_a_rate_limit_passes_on_retry_after() -> None:
    events = run(provider_for(lambda _: httpx2.Response(429, headers={"retry-after": "7"}, json=error_body("rate_limit_error"))))
    assert isinstance(events[-1], ProviderRateLimitedError) and events[-1].retry_after == 8


def test_a_connection_failure_before_any_reply_is_unavailable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down", request=request)

    assert isinstance(run(provider_for(handler))[-1], ProviderUnavailableError)


def test_overload_as_the_first_stream_event_can_still_fall_back() -> None:
    assert isinstance(run(stream_provider(error_body("overloaded_error")))[-1], ProviderUnavailableError)


def test_overload_mid_reply_is_a_stream_error_after_counting() -> None:
    events = run(stream_provider(message_start(), *text_block("Hi"), error_body("overloaded_error")))
    assert events[:3] == [StreamOpened(), UsageEvent(100, 1, 0, partial=True), TextDelta("Hi")]
    assert UsageEvent(100, 1, 0) in events
    assert isinstance(events[-1], ProviderStreamError) and events[-1].code == "provider_error"


def test_the_key_is_sent_only_in_the_auth_header() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(message_start(), *message_end()))

    run(provider_for(handler))
    assert seen[0].headers["authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in seen[0].content.decode()
    assert API_KEY not in str(seen[0].url)


def test_closing_twice_is_safe_and_a_new_call_opens_a_new_client() -> None:
    provider = AnthropicAssistantProvider(settings())  # type: ignore[arg-type]

    async def main() -> None:
        await provider.aclose()
        first = provider._get_client()
        assert provider._get_client() is first
        await provider.aclose()
        await provider.aclose()
        assert provider._client is None

    asyncio.run(main())


# ---- choosing a provider ----------------------------------------------------------------


def choose(**changes: Any) -> tuple[Any, Any]:
    values = make_settings(**{"assistant_anthropic_api_key": "", "assistant_anthropic_model": MODEL, **changes})
    return build_assistant_provider(values), build_assistant_fallback(values)  # type: ignore[arg-type]


def test_both_keys_means_claude_first_and_groq_behind_it() -> None:
    first, fallback = choose(assistant_provider="anthropic", assistant_anthropic_api_key=API_KEY)
    assert isinstance(first, AnthropicAssistantProvider) and first.model == "claude-haiku-5-5"
    assert isinstance(fallback, GroqAssistantProvider)


def test_no_claude_key_stays_on_groq_so_a_deploy_changes_nothing() -> None:
    first, fallback = choose(assistant_provider="anthropic")
    assert isinstance(first, GroqAssistantProvider)
    assert fallback is None


def test_claude_with_no_groq_key_has_no_fallback() -> None:
    first, fallback = choose(assistant_provider="anthropic", assistant_anthropic_api_key=API_KEY, assistant_groq_api_key="")
    assert isinstance(first, AnthropicAssistantProvider)
    assert fallback is None


def test_choosing_groq_never_spends_claude_credit() -> None:
    first, fallback = choose(assistant_provider="groq", assistant_anthropic_api_key=API_KEY)
    assert isinstance(first, GroqAssistantProvider)
    assert fallback is None


def test_an_unknown_provider_is_none() -> None:
    assert choose(assistant_provider="other") == (None, None)


def test_each_provider_needs_its_own_key() -> None:
    values = make_settings(assistant_anthropic_api_key="", assistant_groq_api_key="x")
    assert provider_has_key(values, "groq") is True  # type: ignore[arg-type]
    assert provider_has_key(values, "anthropic") is False  # type: ignore[arg-type]


def test_the_default_provider_is_claude_haiku(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import Settings

    for name in ("ASSISTANT_PROVIDER", "ASSISTANT_ANTHROPIC_MODEL"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.assistant_provider == "anthropic"
    assert defaults.assistant_anthropic_model == "claude-haiku-5-5"


@pytest.mark.parametrize("name", ["ASSISTANT_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"])
def test_the_claude_key_is_read_from_each_accepted_name(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import Settings

    for other in ("ASSISTANT_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, "from-this-name")
    assert Settings(_env_file=None).assistant_anthropic_api_key == "from-this-name"


# ---- falling back -----------------------------------------------------------------------


class FakeClaude(FakeProvider):
    name = "anthropic"
    model = MODEL


GOOD_REPLY = [StreamOpened(), TextDelta("From Groq"), UsageEvent(50, 5), FinishedEvent("stop")]


def service_with(primary_events: list[Any], fallback_events: list[Any] | None = None, store: FakeStore | None = None) -> tuple[AssistantService, FakeClaude, FakeProvider, FakeStore]:
    primary = FakeClaude(primary_events)
    backup = FakeProvider(GOOD_REPLY if fallback_events is None else fallback_events)
    usage_store = store or FakeStore()
    service = AssistantService(settings(), primary, usage_store, ActiveStreamRegistry(), fallback=backup)  # type: ignore[arg-type]
    return service, primary, backup, usage_store


@pytest.mark.parametrize(
    "failure",
    [ProviderRateLimitedError(5), ProviderUnavailableError("status 529"), ProviderUnavailableError("status 400")],
    ids=["rate_limited", "overloaded", "out_of_credit"],
)
def test_a_refusal_from_claude_is_answered_by_groq_with_no_visible_error(failure: Exception) -> None:
    service, primary, backup, store = service_with([failure])
    response = client_for(service).post("/api/assistant/chat", json=BODY)
    assert response.status_code == 200
    events = parse_sse(response.text)
    assert events[0] == ("text", {"delta": "From Groq"})
    assert events[-1][0] == "done"
    assert len(primary.requests) == 1 and len(backup.requests) == 1


def test_a_fallback_counts_once_under_the_provider_that_answered() -> None:
    service, _, _, store = service_with([ProviderUnavailableError("down")])
    client_for(service).post("/api/assistant/chat", json=BODY)
    assert store.calls == 1, "Claude's reservation was given back"
    assert store.released == 1
    assert store.recorded == [(50, 5)]
    outcomes = {entry.key.provider: entry.outcome for entry in store.call_log}
    assert outcomes == {"anthropic": "refused_unavailable", "groq": "completed"}


def test_when_claude_answers_groq_is_never_called() -> None:
    service, _, backup, store = service_with([StreamOpened(), TextDelta("From Claude"), UsageEvent(10, 2, 3), FinishedEvent("stop")])
    response = client_for(service).post("/api/assistant/chat", json=BODY)
    assert ("text", {"delta": "From Claude"}) in parse_sse(response.text)
    assert backup.requests == []
    assert store.call_log[0].key.provider == "anthropic"
    assert store.call_log[0].cached_tokens == 3


def test_a_failure_mid_reply_never_falls_back_so_two_replies_cant_mix() -> None:
    service, _, backup, _ = service_with([StreamOpened(), TextDelta("Half"), ProviderStreamError("provider_error")])
    events = parse_sse(client_for(service).post("/api/assistant/chat", json=BODY).text)
    assert events[-1][0] == "error"
    assert backup.requests == []


def test_a_refusal_mid_reply_reaches_the_user_as_a_clear_message() -> None:
    service, _, backup, _ = service_with([StreamOpened(), UsageEvent(10, 2), ProviderStreamError("refused")])
    events = parse_sse(client_for(service).post("/api/assistant/chat", json=BODY).text)
    assert events[-1] == ("error", {"code": "refused", "message": "The AI declined to answer this request. Please rephrase it and try again."})
    assert backup.requests == []


def test_a_request_too_large_for_claude_does_not_try_groq() -> None:
    service, _, backup, _ = service_with([ProviderRequestTooLargeError("big")])
    assert client_for(service).post("/api/assistant/chat", json=BODY).status_code == 413
    assert backup.requests == []


def test_when_both_refuse_the_user_gets_the_fallbacks_answer_and_nothing_is_charged() -> None:
    service, _, _, store = service_with([ProviderUnavailableError("down")], [ProviderRateLimitedError(9)])
    response = client_for(service).post("/api/assistant/chat", json=BODY)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "9"
    assert store.calls == 0
    assert store.released == 2


def test_with_no_fallback_a_refusal_reaches_the_user_as_before() -> None:
    primary = FakeClaude([ProviderUnavailableError("down")])
    service = AssistantService(settings(), primary, FakeStore(), ActiveStreamRegistry())  # type: ignore[arg-type]
    assert client_for(service).post("/api/assistant/chat", json=BODY).status_code == 503


def test_a_fallback_frees_the_users_stream_slot_afterwards() -> None:
    service, _, _, _ = service_with([ProviderUnavailableError("down")])
    client = client_for(service)
    assert [client.post("/api/assistant/chat", json=BODY).status_code for _ in range(2)] == [200, 200]


def test_a_fallback_can_be_stopped_by_the_daily_limit_after_the_refund() -> None:
    async def main() -> None:
        store = FakeStore(calls=39)
        service, _, backup, _ = service_with([ProviderUnavailableError("down")], store=store)
        stream = await service.open_chat(USER_ID, ChatRequest.model_validate(BODY))
        await stream.close()
        assert len(backup.requests) == 1, "the refund made room for the one call"

    asyncio.run(main())


def test_the_limit_stops_a_call_before_any_provider_and_no_fallback_is_tried() -> None:
    async def main() -> None:
        store = FakeStore(calls=40)
        service, primary, backup, _ = service_with([StreamOpened()], store=store)
        with pytest.raises(AssistantRateLimitedError):
            await service.open_chat(USER_ID, ChatRequest.model_validate(BODY))
        assert primary.requests == [] and backup.requests == []

    asyncio.run(main())


def test_a_prompt_near_the_expensive_size_never_reaches_any_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_service, "MAX_PROMPT_TOKENS", 10)

    async def main() -> None:
        service, primary, backup, store = service_with([StreamOpened()])
        with pytest.raises(PayloadTooLargeError):
            await service.open_chat(USER_ID, ChatRequest.model_validate(BODY))
        assert primary.requests == [] and backup.requests == []
        assert store.reservations == []

    asyncio.run(main())


def test_the_expensive_size_guard_stays_under_100000() -> None:
    assert assistant_service.MAX_PROMPT_TOKENS == 90_000


def test_the_size_guard_allows_for_the_estimate_being_low(monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> None:
        service, primary, _, _ = service_with([StreamOpened(), FinishedEvent("stop")])
        built = service.build_request(ChatRequest.model_validate(BODY))
        # Under the limit as estimated, over it once the margin is applied.
        monkeypatch.setattr(assistant_service, "MAX_PROMPT_TOKENS", int(built.estimated_tokens * 1.2))
        with pytest.raises(PayloadTooLargeError):
            await service.open_chat(USER_ID, ChatRequest.model_validate(BODY))
        assert primary.requests == []

    asyncio.run(main())


def test_the_assistant_is_on_only_when_the_first_providers_key_is_set() -> None:
    primary = FakeClaude()
    with_key = AssistantService(settings(), primary, FakeStore(), ActiveStreamRegistry())  # type: ignore[arg-type]
    assert with_key.is_enabled() is True
    no_key = make_settings(assistant_anthropic_api_key="")
    assert AssistantService(no_key, primary, FakeStore(), ActiveStreamRegistry()).is_enabled() is False  # type: ignore[arg-type]


def test_events_stay_the_same_shape_whichever_provider_answers() -> None:
    def names(service: AssistantService) -> list[str]:
        return [name for name, _ in parse_sse(client_for(service).post("/api/assistant/chat", json=BODY).text)]

    claude_service, _, _, _ = service_with([StreamOpened(), TextDelta("a"), UsageEvent(1, 1), FinishedEvent("stop")])
    groq_service, _, _, _ = service_with([ProviderUnavailableError("down")])
    assert names(claude_service) == names(groq_service) == ["text", "usage", "done"]


def _event_types(events: list[ProviderEvent]) -> list[str]:
    return [type(event).__name__ for event in events]


def test_real_sdk_events_drive_the_whole_service() -> None:
    provider = stream_provider(message_start(100, read=40), *text_block("Hello"), *tool_block("t1", "read_file", '{"path": "a.py"}', index=1), *message_end("tool_use", 7))
    store = FakeStore()
    service = AssistantService(settings(), provider, store, ActiveStreamRegistry())  # type: ignore[arg-type]
    events = parse_sse(client_for(service).post("/api/assistant/chat", json={**BODY, "mode": "agent"}).text)
    assert [name for name, _ in events] == ["text", "tool_call", "usage", "done"]
    assert events[1][1] == {"id": "t1", "name": "read_file", "arguments": {"path": "a.py"}}
    assert events[2][1]["input_tokens"] == 140
    assert events[3][1] == {"finish_reason": "tool_calls"}
    assert store.recorded == [(140, 7)]
    assert store.call_log[0].cached_tokens == 40


# ---- the reply rules --------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["ask", "agent"])
@pytest.mark.parametrize("language", ["python", "web", "backend", "java"])
def test_every_prompt_has_the_reply_rules(mode: Any, language: Any) -> None:
    prompt = system_prompt(mode, language)
    assert "Hinglish in Roman letters" in prompt
    assert "under about 250 words" in prompt
    assert "Want me to continue?" in prompt
    assert "I can't share my setup, but I'm happy to help with your question." in prompt
    assert "Decline harmful" in prompt


def test_ask_mode_still_requires_complete_files_for_apply() -> None:
    assert "<every line of the file>" in system_prompt("ask", "python")
    assert "unless a file change needs all of it" in system_prompt("ask", "python")


def turn_pairs(count: int) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for index in range(count):
        messages += [ChatMessage(role="user", content=f"question {index}"), ChatMessage(role="assistant", content=f"answer {index}")]
    return messages


def test_only_the_last_ten_messages_of_history_are_sent() -> None:
    messages = [*turn_pairs(20), ChatMessage(role="user", content="newest")]
    sent = optimize_conversation(messages, ChatContext(language="python"), "ask", 100_000).messages
    text = json.dumps(sent)
    assert len(sent) == 9, "four earlier turns of two, plus the newest: a fifth would make eleven"
    assert "question 15" not in text
    assert "question 16" in text and "answer 19" in text and "newest" in text
    assert "left out to save space" in text


def test_a_short_chat_loses_nothing() -> None:
    messages = [*turn_pairs(4), ChatMessage(role="user", content="newest")]
    sent = optimize_conversation(messages, ChatContext(language="python"), "ask", 100_000).messages
    assert len(sent) == 9
    assert "left out" not in json.dumps(sent)


def test_a_long_agent_task_keeps_every_round_of_its_current_turn() -> None:
    messages: list[ChatMessage] = [ChatMessage(role="user", content="older"), ChatMessage(role="assistant", content="ok"), ChatMessage(role="user", content="do the task")]
    for index in range(8):
        call = ChatToolCall(id=f"c{index}", name="read_file", arguments={"path": f"{index}.py"})
        messages += [ChatMessage(role="assistant", tool_calls=[call]), ChatMessage(role="tool", tool_call_id=f"c{index}", content="text")]
    sent = optimize_conversation(messages, ChatContext(language="python"), "agent", 100_000).messages
    called = [call["id"] for message in sent for call in message.get("tool_calls") or []]
    answered = [message["tool_call_id"] for message in sent if message["role"] == "tool"]
    assert called == answered == [f"c{index}" for index in range(8)]
    assert "older" not in json.dumps(sent), "the current turn is over ten messages, so earlier turns go"


def test_history_trimming_never_splits_a_tool_call_from_its_result() -> None:
    messages: list[ChatMessage] = []
    for index in range(6):
        call = ChatToolCall(id=f"c{index}", name="read_file", arguments={"path": "a.py"})
        messages += [ChatMessage(role="user", content=f"q{index}"), ChatMessage(role="assistant", tool_calls=[call]), ChatMessage(role="tool", tool_call_id=f"c{index}", content="r")]
    messages.append(ChatMessage(role="user", content="newest"))
    for message in anthropic_messages(optimize_conversation(messages, ChatContext(language="python"), "agent", 100_000).messages):
        blocks = message["content"]
        if message["role"] == "assistant":
            assert [block["id"] for block in blocks if block["type"] == "tool_use"]
    sent = optimize_conversation(messages, ChatContext(language="python"), "agent", 100_000).messages
    called = [call["id"] for message in sent for call in message.get("tool_calls") or []]
    answered = [message["tool_call_id"] for message in sent if message["role"] == "tool"]
    assert called == answered
