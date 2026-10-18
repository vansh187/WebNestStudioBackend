"""Unit tests of the exam infrastructure and randomized property tests of
the draw and grading. No database and no network (httpx MockTransport)."""

import asyncio
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from core.error_handlers import register_error_handlers
from exams.domain.draw import DrawCandidate, DrawModule, draw
from exams.domain.grading import KeyEntry, grade
from exams.errors import AttemptLost, NotEligible, PoolNotReady, ProviderFailure, QuotaExhausted, RateLimited
from exams.infra.attempt_cache import LayeredAttemptCache, MemoryAttemptCache
from exams.infra.code_runner import ExamCodeRunner, _run_sql_blocking
from exams.infra.llm_client import ExamLlmClient, GeminiEmbedder
from exams.infra.system import SlidingWindowRateLimiter
from exams.routing import ExamContractRoute

NOW = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def llm_settings(**overrides):
    values = dict(gemini_api_key="g-key", groq_api_key="q-key", gemini_model="gm", groq_model="qm", llm_timeout_seconds=5,
                  groq_max_output_tokens=8000, exam_embedding_model="emb", jdoodle_client_id="", jdoodle_client_secret="")  # fmt: skip
    values.update(overrides)
    return SimpleNamespace(**values)


def client_with(handler, **overrides):
    return ExamLlmClient(llm_settings(**overrides), transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# LLM client
# --------------------------------------------------------------------------- #
def test_gemini_success_uses_json_mode_and_reports_usage():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers.get("x-goog-api-key")
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True}, {"text": '{"ok": 1}'}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20, "thoughtsTokenCount": 5},
        })  # fmt: skip

    reply = run(client_with(handler).complete_json("gemini", "sys", "user", 0.3, 100))
    assert reply.text == '{"ok": 1}' and reply.prompt_tokens == 10 and reply.completion_tokens == 25
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert seen["body"]["generationConfig"]["temperature"] == 0.3 and seen["key"] == "g-key"


def test_groq_success_captures_rate_limit_headers_and_caps_tokens():
    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"} and body["max_tokens"] == 8000
        return httpx.Response(200, headers={"x-ratelimit-remaining-requests": "99", "x-other": "x"},
                              json={"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 4}})  # fmt: skip

    reply = run(client_with(handler).complete_json("groq", "s", "u", 0.0, 99999))
    assert reply.rate_limits == {"x-ratelimit-remaining-requests": "99"} and reply.prompt_tokens == 3


@pytest.mark.parametrize(
    "response,error",
    [
        (httpx.Response(429, headers={"retry-after": "30"}), QuotaExhausted),
        (httpx.Response(400, text='{"error": {"status": "RESOURCE_EXHAUSTED"}}'), QuotaExhausted),
        (httpx.Response(500, text="boom"), ProviderFailure),
        (httpx.Response(401, text="bad key"), ProviderFailure),
        (httpx.Response(200, json={"candidates": []}), ProviderFailure),
        (httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "  "}]}}]}), ProviderFailure),
        (httpx.Response(200, text="<html>not json</html>"), ProviderFailure),
    ],
)
def test_gemini_failures(response, error):
    with pytest.raises(error) as raised:
        run(client_with(lambda request: response).complete_json("gemini", "s", "u", 0, 10))
    if response.status_code == 429:
        assert raised.value.retry_after == 30
    assert "g-key" not in str(raised.value), "the API key never appears in errors"


def test_timeouts_and_transport_errors_are_provider_failures():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def broken(request):
        raise httpx.ConnectError("dns", request=request)

    for handler in (timeout, broken):
        with pytest.raises(ProviderFailure):
            run(client_with(handler).complete_json("groq", "s", "u", 0, 10))
    with pytest.raises(ProviderFailure):
        run(client_with(lambda r: httpx.Response(200, json={})).complete_json("other", "s", "u", 0, 10))


def test_available_providers_skip_blank_keys():
    assert ExamLlmClient(llm_settings(gemini_api_key="  ")).available_providers() == ["groq"]
    assert ExamLlmClient(llm_settings(gemini_api_key="", groq_api_key="")).available_providers() == []


def test_embedder_batches_and_validates_shape():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(len(body["requests"]))
        assert all(r["outputDimensionality"] == 768 for r in body["requests"])
        return httpx.Response(200, json={"embeddings": [{"values": [0.1] * 768} for _ in body["requests"]]})

    embedder = GeminiEmbedder(llm_settings(), transport=httpx.MockTransport(handler))
    vectors, _ = run(embedder.embed([f"t{i}" for i in range(120)]))
    assert len(vectors) == 120 and calls == [50, 50, 20]
    assert run(embedder.embed([])) == ([], 0)
    bad = GeminiEmbedder(llm_settings(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"embeddings": [{"values": [1, 2]}]})))
    with pytest.raises(ProviderFailure):
        run(bad.embed(["a"]))
    quota = GeminiEmbedder(llm_settings(), transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(QuotaExhausted):
        run(quota.embed(["a"]))
    with pytest.raises(ProviderFailure):
        run(GeminiEmbedder(llm_settings(gemini_api_key="")).embed(["a"]))


# --------------------------------------------------------------------------- #
# Code runner
# --------------------------------------------------------------------------- #
def test_sql_runner_output_format():
    out = _run_sql_blocking("CREATE TABLE t (a INT, b TEXT);\nINSERT INTO t VALUES (1, 'x'), (2, NULL);\nSELECT a, b FROM t ORDER BY a;", 5)
    assert out.ran and out.output == "1 | x\n2 | NULL"


@pytest.mark.parametrize(
    "script,fragment",
    [
        ("ATTACH DATABASE 'x.db' AS other;", "sql error"),
        ("PRAGMA max_page_count = 100000;", "sql error"),
        ("SELECT load_extension('evil');", "sql error"),
        ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c;", "timed out"),
        ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 1000) SELECT x FROM c;", "too many result rows"),
        ("SELEC oops;", "sql error"),
        ("SELECT 1;" * 2000, "too long"),
    ],
)
def test_sql_runner_refuses_unsafe_or_runaway_scripts(script, fragment):
    out = _run_sql_blocking(script, 1.0)
    assert not out.ran and fragment in out.error


def test_python_runner_and_unsupported_languages():
    runner = ExamCodeRunner(llm_settings())
    ok = run(runner.run("python", "print(6 * 7)"))
    assert ok.ran and ok.output.strip() == "42"
    unsafe = run(runner.run("python", "import os\nprint(os.listdir('.'))"))
    assert not unsafe.ran and "not allowed" in unsafe.error
    assert not run(runner.run("java", "class Main {}")).ran, "no JDoodle credentials: Java can't run"
    assert runner.supports("python") and runner.supports("sql") and not runner.supports("java") and not runner.supports("html")
    assert runner.is_external("java") and not runner.is_external("python")


# --------------------------------------------------------------------------- #
# Rate limiter and caches
# --------------------------------------------------------------------------- #
def test_rate_limiter_window_retry_after_and_sweep():
    t = [0.0]
    limiter = SlidingWindowRateLimiter(monotonic=lambda: t[0])
    assert [limiter.hit("k", 2, 60) for _ in range(3)] == [0, 0, 60]
    t[0] = 30
    assert limiter.hit("k", 2, 60) == 30
    t[0] = 60.5
    assert limiter.hit("k", 2, 60) == 0
    t[0] = 10_000
    limiter.hit("other", 1, 1)
    assert "k" not in limiter._events, "idle keys are swept"


def test_memory_cache_expiry_cap_and_sweep():
    cache = MemoryAttemptCache(max_entries=2)
    u = uuid.uuid4()
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cache.put(a, u, [1], NOW + timedelta(minutes=1))
    cache.put(b, u, [2], NOW + timedelta(minutes=5))
    assert cache.get(a, u, NOW) == [1] and cache.get(a, uuid.uuid4(), NOW) is None, "keyed by user too"
    cache.put(c, u, [3], NOW + timedelta(minutes=5))
    assert len(cache) == 2 and cache.get(b, u, NOW) is None, "least recently used evicted"
    assert cache.get(a, u, NOW + timedelta(minutes=2)) is None, "expired entries are dropped"
    assert cache.sweep(NOW + timedelta(hours=1)) == 1 and len(cache) == 0


class _Table:
    def __init__(self):
        self.rows = {}
        self.reads = 0

    async def get(self, aid, uid):
        self.reads += 1
        return self.rows.get(aid)

    async def exists(self, aid, uid):
        return aid in self.rows

    async def put(self, aid, uid, payload, expires_at):
        self.rows[aid] = (payload, expires_at)

    async def delete(self, aid):
        self.rows.pop(aid, None)

    async def sweep(self, now):
        return 0


def test_layered_cache_refills_after_a_restart_and_deletes_both_layers():
    clock = SimpleNamespace(now=lambda: NOW)
    table = _Table()
    u, a = uuid.uuid4(), uuid.uuid4()
    first = LayeredAttemptCache(MemoryAttemptCache(), table, clock)
    run(first.put(a, u, [{"q": 1}], NOW + timedelta(hours=1)))
    restarted = LayeredAttemptCache(MemoryAttemptCache(), table, clock)  # empty dictionary
    assert run(restarted.get(a, u)) == [{"q": 1}] and table.reads == 1
    assert run(restarted.get(a, u)) == [{"q": 1}] and table.reads == 1, "second read served from memory"
    run(restarted.delete(a, u))
    assert run(restarted.get(a, u)) is None and not run(restarted.exists(a, u))


# --------------------------------------------------------------------------- #
# Error handler shapes
# --------------------------------------------------------------------------- #
class _Body(BaseModel):
    name: str


def error_app():
    app = FastAPI()
    exam = APIRouter(route_class=ExamContractRoute)
    plain = APIRouter()

    @exam.post("/exam")
    async def exam_route(body: _Body):
        return {}

    @plain.post("/plain")
    async def plain_route(body: _Body):
        return {}

    @exam.get("/lost")
    async def lost():
        raise AttemptLost()

    @exam.get("/cooldown")
    async def cooldown():
        raise NotEligible(reason="cooldown", cooldown_until=datetime(2026, 10, 19, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))))

    @exam.get("/limited")
    async def limited():
        raise RateLimited(42)

    @exam.get("/pool")
    async def pool():
        raise PoolNotReady()

    @exam.get("/crash")
    async def crash():
        raise RuntimeError("secret stack detail")

    app.include_router(exam)
    app.include_router(plain)
    register_error_handlers(app)
    return TestClient(app, raise_server_exceptions=False)


def test_exam_error_bodies():
    client = error_app()
    assert client.get("/lost").json()["code"] == "attempt_lost" and client.get("/lost").status_code == 409
    body = client.get("/cooldown").json()
    assert body["reason"] == "cooldown" and body["cooldown_until"] == "2026-10-19T04:30:00Z", "UTC with Z"
    limited = client.get("/limited")
    assert limited.status_code == 429 and limited.headers["retry-after"] == "42" and limited.json()["retry_after"] == 42
    assert client.get("/pool").json()["retry_after"] == 600
    crash = client.get("/crash")
    assert crash.status_code == 500 and "secret" not in crash.text and isinstance(crash.json()["detail"], str)


def test_validation_shapes_are_route_scoped_and_never_echo_input():
    client = error_app()
    exam = client.post("/exam", json={"name": ["SECRET-ANSWER"]})
    assert exam.status_code == 422 and exam.json()["code"] == "validation_error"
    assert isinstance(exam.json()["detail"], str) and "SECRET-ANSWER" not in exam.text
    plain = client.post("/plain", json={"name": 5})
    assert plain.status_code == 422 and "code" not in plain.json()
    weird = client.post("/exam", content=b'{"\\ud800": 1}', headers={"content-type": "application/json"})
    assert weird.status_code == 422


# --------------------------------------------------------------------------- #
# Property tests
# --------------------------------------------------------------------------- #
def random_pool(rng: random.Random, course):
    modules = [DrawModule(id=uuid.uuid4(), lesson_count=rng.randint(1, 12)) for _ in range(rng.randint(1, 8))]
    concepts = rng.choice([10, 40, 1000])
    candidates = [
        DrawCandidate(id=uuid.uuid4(), course_id=course, module_id=rng.choice(modules).id,
                      difficulty=rng.choice(["easy", "medium", "hard"]), concept=f"c{rng.randrange(concepts)}")
        for _ in range(rng.randint(25, 120))
    ]  # fmt: skip
    return modules, candidates


def test_draw_invariants_over_many_random_pools():
    rng = random.Random(2026)
    course = uuid.uuid4()
    checked = 0
    for _ in range(400):
        modules, candidates = random_pool(rng, course)
        seen = {c.id: NOW - timedelta(days=rng.randint(0, 9)) for c in candidates if rng.random() < 0.4}
        count = rng.randint(5, 40)
        try:
            drawn = draw(course, modules, candidates, seen, count, random.Random(rng.random()))
        except PoolNotReady:
            assert len(candidates) < count
            continue
        checked += 1
        ids = [d.question_id for d in drawn]
        by_id = {c.id: c for c in candidates}
        assert len(ids) == count == len(set(ids)) and set(ids) <= set(by_id)
        assert all(sorted(d.permutation) == [0, 1, 2, 3] for d in drawn)
        assert all(d.module_id == by_id[d.question_id].module_id for d in drawn)
        unseen = [c for c in candidates if c.id not in seen]
        unseen_concepts = {c.concept for c in unseen}
        if len(unseen_concepts) >= count and len(unseen) >= count:
            assert not set(ids) & set(seen), "a seen question was drawn while enough unseen ones existed"
        if len({c.concept for c in candidates}) >= count:
            assert len({by_id[i].concept for i in ids}) == count, "concepts must not repeat when they don't have to"
    assert checked > 300


def test_grading_matches_a_reference_over_random_papers():
    rng = random.Random(7)
    for _ in range(500):
        n = rng.randint(1, 40)
        key = [KeyEntry(f"q{i}", rng.choice("ABC"), tuple(rng.sample(range(4), 4)), rng.randrange(4)) for i in range(n)]
        answers = {f"q{i}": rng.choice([0, 1, 2, 3, 7, -1, None, True]) for i in range(n) if rng.random() < 0.8}
        answers = {k: v for k, v in answers.items() if v is not None}
        expected = sum(
            1 for e in key
            if type(answers.get(e.question_id)) is int and 0 <= answers[e.question_id] < 4
            and e.permutation[answers[e.question_id]] == e.correct_index
        )  # fmt: skip
        outcome = grade(key, answers, 70)
        assert outcome.score == expected and outcome.total == n
        assert outcome.passed == (expected * 100 >= 70 * n)
        assert sum(m["total"] for m in outcome.module_breakdown) == n
