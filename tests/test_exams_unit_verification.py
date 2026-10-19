"""Unit tests of the verification pipeline and the refill job with fake
models, embedder and code runner: every rejection path, quota pauses,
retries, answer balancing and model-output robustness."""

import asyncio
import json
import os
import random
from dataclasses import dataclass

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest

from exams.domain.validation import DraftQuestion
from exams.errors import ProviderFailure, QuotaExhausted
from exams.records import ProviderUsage
from exams.services import verification as verification_module
from exams.services.generation_prompts import extract_json_object, parse_check_reply, parse_draft_item, parse_draft_reply
from exams.services.generation_service import GenerationService
from exams.services.pool_service import PoolService
from exams.services.verification import Candidate, QuotaPause, VerificationPipeline
from tests import exam_fakes as f


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr(verification_module, "CALL_RETRY_DELAYS", (0.0, 0.0))


@dataclass
class Reply:
    text: str
    provider: str
    model: str = "m"
    prompt_tokens: int = 1
    completion_tokens: int = 1
    rate_limits: dict | None = None


class Llm:
    def __init__(self, providers=("gemini", "groq")):
        self.providers = list(providers)
        self.calls = []
        self.check_answers = None  # callable(questions) -> answers
        self.failures = []  # exceptions to raise on the next calls
        self.draft_text = None

    def available_providers(self):
        return list(self.providers)

    async def complete_json(self, provider, system, user, temperature, max_tokens):
        self.calls.append((provider, "draft" if "Write exactly" in user else "check", temperature))
        if self.failures:
            raise self.failures.pop(0)
        if "Write exactly" in user:
            return Reply(self.draft_text, provider)
        questions = json.loads(user.split("Questions (JSON):\n", 1)[1].split("\n\nReturn", 1)[0])
        return Reply(json.dumps({"answers": self.check_answers(questions)}), provider)


KNOWN_RIGHT = {"`42`", "42", "Divides by zero"}


def agree(questions):
    return [{"id": q["id"], "answer_index": next(i for i, o in enumerate(q["options"]) if o.startswith("Right") or o in KNOWN_RIGHT),
             "single_correct": True, "version_issue": False, "confidence": 0.95} for q in questions]  # fmt: skip


class Embedder:
    def __init__(self, error=None, same=False):
        self.error, self.same = error, same

    async def embed(self, texts):
        if self.error:
            raise self.error
        out = []
        for t in texts:
            rng = random.Random(0 if self.same else t)
            out.append([rng.gauss(0, 1) for _ in range(8)])
        return out, 1


@dataclass
class Outcome:
    ran: bool
    output: str
    error: str = ""


class Runner:
    def __init__(self, output="42", ran=True, external=False, supported=True):
        self.output, self.ran, self.external, self.supported = output, ran, external, supported
        self.runs = 0

    def supports(self, language):
        return self.supported

    def is_external(self, language):
        return self.external

    async def run(self, language, code):
        self.runs += 1
        return Outcome(self.ran, self.output, "" if self.ran else "boom")


class Env:
    def __init__(self, slug="java-core", **overrides):
        self.settings = f.settings(**overrides)
        self.clock = f.Clock()
        self.courses = f.Courses()
        self.course = self.courses.add(slug=slug, ready=False)
        self.module = self.courses.modules[self.course.id][0]
        self.lesson = next(l for l in self.courses.lessons[self.course.id] if l.module_id == self.module.id)
        self.questions = f.Questions()
        self.logs = f.Logs()
        self.llm = Llm()
        self.llm.check_answers = agree
        self.embedder = Embedder()
        self.runner = Runner()
        self.context = {
            "lessons": [{"lesson_id": self.lesson.key, "title": "Compilers", "summary": "The javac compiler turns source into bytecode.",
                         "key_points": ["The JVM verifies bytecode before running it"], "avoid_prompts": ["What does javac do exactly"]}]
        }  # fmt: skip
        self.pipeline = VerificationPipeline(
            self.questions, self.logs, self.llm, self.embedder, self.runner, self.clock, random.Random(3), self.settings
        )

    def verify(self, drafts, mode="ai", drafter="gemini", contexts=None, expected_module=True):
        candidates = [Candidate(line=i, draft=d, expected_module_id=self.module.id if expected_module else None) for i, d in enumerate(drafts)]
        contexts = {self.module.id: self.context} if contexts is None else contexts
        run(self.pipeline.verify(self.course, self.courses.lessons[self.course.id], contexts, candidates, mode, drafter))
        return candidates


def draft(n=1, **overrides):
    values = dict(
        prompt=f"Which tool turns source into bytecode in scenario {n} " + " ".join(f"t{n}_{i}" for i in range(5)),
        options=[f"Right {n}", f"Wrong a {n}", f"Wrong b {n}", f"Wrong c {n}"],
        correct_index=0, explanation="javac does.", difficulty="easy", kind="concept", concept=f"concept {n}",
        lesson_ref=None, source_fact="The javac compiler turns source into bytecode",
        why_others_wrong=["no", "nope", "never"],
    )  # fmt: skip
    values.update(overrides)
    return DraftQuestion(**values)


@pytest.fixture
def env():
    e = Env()
    return e


def mk(env, n=1, **overrides):
    return draft(n, lesson_ref=overrides.pop("lesson_ref", env.lesson.key), **overrides)


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def test_a_good_draft_goes_live_and_is_balanced(env):
    c = env.verify([mk(env)])[0]
    assert c.status == "live", c.reason
    assert c.draft.options[c.draft.correct_index] == "Right 1"
    assert c.checks["grounded"] is True and c.checks["checker"] == "passed" and c.embedding is not None
    assert [p for p, kind, _ in env.llm.calls] == ["groq"], "the drafter (gemini) never checks itself"
    assert env.llm.calls[0][2] == 0.0, "cross-checks run at temperature 0"


def test_balance_keeps_rationales_aligned(env):
    d = mk(env, why_others_wrong=["why a", "why b", "why c"])
    c = env.verify([d])[0]
    wrong = [o for i, o in enumerate(c.draft.options) if i != c.draft.correct_index]
    assert [w.split()[1] for w in wrong] == [r.split()[1] for r in c.draft.why_others_wrong]


@pytest.mark.parametrize(
    "overrides,status,check",
    [
        ({"lesson_ref": "lesson_unknown"}, "invalid", "lesson"),
        ({"options": ["a", "b"]}, "invalid", "shape"),
        ({"correct_index": 9}, "invalid", "shape"),
        ({"explanation": ""}, "rejected", "shape"),
        ({"why_others_wrong": []}, "rejected", "shape"),
        ({"source_fact": None}, "rejected", "grounding"),
        ({"source_fact": "Dragons compile Fortran quickly"}, "rejected", "grounding"),
        ({"prompt": "What does javac do exactly"}, "rejected", "duplicate"),
    ],
)
def test_ai_rejections(env, overrides, status, check):
    c = env.verify([mk(env, **overrides)])[0]
    assert (c.status, c.check) == (status, check), c.reason
    assert not any(kind == "check" for _, kind, _ in env.llm.calls), "no model call for locally rejected drafts"


def test_lesson_of_another_module_is_a_course_mismatch(env):
    other_lesson = next(l for l in env.courses.lessons[env.course.id] if l.module_id != env.module.id)
    c = env.verify([mk(env, lesson_ref=other_lesson.key)])[0]
    assert (c.status, c.reason) == ("invalid", "course_mismatch")


def test_lesson_of_another_course_is_refused(env):
    other = env.courses.add(slug="python", code="PY")
    foreign = env.courses.lessons[other.id][0]
    c = env.verify([mk(env, lesson_ref=str(foreign.id))], expected_module=False)[0]
    assert (c.status, c.reason) == ("invalid", "unknown_lesson")


def test_archived_lessons_are_not_usable(env):
    lessons = env.courses.lessons[env.course.id]
    lessons[0] = lessons[0].__class__(**{**vars(lessons[0]), "status": "archived"})
    c = env.verify([mk(env, lesson_ref=lessons[0].key)])[0]
    assert c.status == "invalid"


def test_no_context_in_ai_mode_is_rejected(env):
    assert env.verify([mk(env)], contexts={})[0].reason == "no_module_context"


def test_duplicates_inside_a_batch_and_against_the_pool(env):
    same = [mk(env, 1), mk(env, 1)]
    out = env.verify(same)
    assert [c.status for c in out] == ["live", "rejected"] and out[1].reason == "duplicate_exact"
    env.questions.add(env.course, env.module, env.lesson, 77)
    existing = next(iter(env.questions.rows.values()))
    copy = mk(env, 2, prompt=existing.prompt, options=list(existing.options))
    assert env.verify([copy])[0].reason == "duplicate_exact"


def test_concept_limit(env):
    for n in (10, 11):
        env.questions.add(env.course, env.module, env.lesson, n, concept="javac output")
    c = env.verify([mk(env, 3, concept="Javac  OUTPUT")])[0]
    assert c.check == "concept"


def test_semantic_duplicates(env):
    env.embedder.same = True
    out = env.verify([mk(env, 1), mk(env, 2)])
    assert [c.status for c in out] == ["live", "rejected"] and out[1].reason == "duplicate_meaning"


def test_embedding_quota_pauses_ai_but_not_import(env):
    env.embedder.error = QuotaExhausted("gemini", "q")
    with pytest.raises(QuotaPause):
        env.verify([mk(env)])
    c = env.verify([mk(env, 5)], mode="import")[0]
    assert c.status == "live" and c.checks["semantic"] == "skipped_no_quota" and c.checks["checker"] == "deferred"


def test_embedding_outage_skips_the_semantic_layer(env):
    env.embedder.error = ProviderFailure("gemini", "down")
    c = env.verify([mk(env)])[0]
    assert c.status == "live" and c.checks["semantic"] == "skipped_unavailable"


@pytest.mark.parametrize(
    "answers,reason",
    [
        (lambda qs: [{**a, "answer_index": (a["answer_index"] + 1) % 4} for a in agree(qs)], "different answer"),
        (lambda qs: [{**a, "single_correct": False} for a in agree(qs)], "more than one"),
        (lambda qs: [{**a, "version_issue": True} for a in agree(qs)], "version"),
        (lambda qs: [{**a, "confidence": 0.79} for a in agree(qs)], "confidence"),
        (lambda qs: [{**a, "answer_index": 17} for a in agree(qs)], "different answer"),
        (lambda qs: [], "no answer"),
        (lambda qs: [{**a, "id": "zzz"} for a in agree(qs)], "no answer"),
    ],
)
def test_checker_verdicts(env, answers, reason):
    env.llm.check_answers = answers
    c = env.verify([mk(env)])[0]
    assert c.status == "rejected" and reason in c.reason


def test_confidence_exactly_at_the_threshold_passes(env):
    env.llm.check_answers = lambda qs: [{**a, "confidence": 0.8} for a in agree(qs)]
    assert env.verify([mk(env)])[0].status == "live"


def test_hard_and_code_questions_get_a_second_shuffled_check(env):
    seen_orders = []

    def answers(qs):
        seen_orders.append([q["options"] for q in qs])
        return agree(qs)

    env.llm.check_answers = answers
    c = env.verify([mk(env, difficulty="hard")])[0]
    assert c.status == "live" and len(seen_orders) == 2
    assert len(c.checks["checker_runs"]) == 2


def test_second_check_disagreeing_rejects(env):
    calls = {"n": 0}

    def answers(qs):
        calls["n"] += 1
        a = agree(qs)
        return a if calls["n"] == 1 else [{**x, "answer_index": (x["answer_index"] + 1) % 4} for x in a]

    env.llm.check_answers = answers
    c = env.verify([mk(env, difficulty="hard")])[0]
    assert c.status == "rejected" and c.check == "checker_2"


def test_no_independent_checker(env):
    env.llm.providers = ["gemini"]
    with pytest.raises(QuotaPause) as raised:
        env.verify([mk(env)])
    assert raised.value.reason == "no_independent_checker"
    c = env.verify([mk(env, 9)], mode="manual", drafter=None)[0]
    assert c.status == "live" and c.checks["checker"] == "groq" or c.checks["checker"] in ("passed", "skipped_no_quota")


def test_failed_check_calls_retry_twice_then_reject(env):
    env.llm.failures = [ProviderFailure("groq", "x")] * 3
    c = env.verify([mk(env)])[0]
    assert c.status == "rejected" and len(env.llm.calls) == 3
    assert sum(1 for e in env.logs.entries if e.purpose == "check" and e.error) == 3


def test_a_quota_refusal_is_never_retried(env):
    env.llm.failures = [QuotaExhausted("groq", "q", retry_after=120)]
    with pytest.raises(QuotaPause) as raised:
        env.verify([mk(env)])
    assert len(env.llm.calls) == 1
    assert (raised.value.until - env.clock.now()).total_seconds() == 120


def test_daily_limit_and_provider_95_percent(env):
    env.logs.extra_calls = 300
    with pytest.raises(QuotaPause) as raised:
        env.verify([mk(env)])
    assert raised.value.reason == "daily_call_limit" and raised.value.until.hour == 0
    env.logs.extra_calls = 0
    env.settings.groq_daily_request_limit = 100
    env.logs.usage = [ProviderUsage("groq", 95, 0, 0, 0, None)]
    with pytest.raises(QuotaPause) as raised:
        env.verify([mk(env, 2)])
    assert raised.value.reason == "groq_quota_95"


def test_code_questions():
    e = Env(slug="python")
    good = draft(1, lesson_ref=e.lesson.key, kind="code_output", code="print(42)", expected_output="42", options=["`42`", "41", "43", "40"])
    assert e.verify([good])[0].status == "live"
    e.runner.output = "41"
    assert e.verify([draft(2, lesson_ref=e.lesson.key, kind="code_output", code="print(41)", expected_output="42", options=["42", "41", "43", "40"])])[0].check == "code"
    e.runner.output = "42"
    key_mismatch = draft(3, lesson_ref=e.lesson.key, kind="code_output", code="print(42)", expected_output="42", options=["40", "42", "43", "41"])
    assert e.verify([key_mismatch])[0].reason == "real output differs from the keyed answer"
    e.runner.ran = False
    assert "did not run" in e.verify([draft(4, lesson_ref=e.lesson.key, kind="code_output", code="x", expected_output="42", options=["42", "1", "2", "3"])])[0].reason
    e.runner.ran = True
    bug = draft(5, lesson_ref=e.lesson.key, kind="spot_the_bug", code="print(1/0)", expected_output=None,
                options=["Divides by zero", "Missing colon", "Wrong indent", "Bad name"])  # fmt: skip
    runs = e.runner.runs
    assert e.verify([bug])[0].status == "live" and e.runner.runs == runs, "spot_the_bug without output isn't run"


def test_code_questions_for_courses_that_cant_run_code_are_rejected(env):
    # java-core with a runner that doesn't support Java (no JDoodle)
    env.runner.supported = False
    c = env.verify([mk(env, kind="code_output", code="class Main{}", expected_output="1", options=["1", "2", "3", "4"])])[0]
    assert c.check == "code" and c.checks["executed"]["reason"] == "not_runnable"
    html = Env(slug="html")
    c = html.verify([draft(1, lesson_ref=html.lesson.key, kind="code_output", code="<p>", expected_output="1", options=["1", "2", "3", "4"])])[0]
    assert c.check == "code"


def test_external_runs_respect_the_daily_limit():
    e = Env(slug="javascript", exam_code_run_daily_limit=1)
    e.runner.external = True
    q = lambda n: draft(n, lesson_ref=e.lesson.key, kind="code_output", code="console.log(42)", expected_output="42", options=["42", "1", "2", "3"])  # noqa: E731
    assert e.verify([q(1)])[0].status == "live"
    assert e.verify([q(2)])[0].reason == "code_run_limit"


def test_import_mode_defers_the_check_and_needs_no_rationale(env):
    c = env.verify([mk(env, why_others_wrong=[], source_fact=None)], mode="import")[0]
    assert c.status == "live" and c.checks["checker"] == "deferred" and c.checks["grounded"] == "skipped"
    assert env.llm.calls == []


# --------------------------------------------------------------------------- #
# Model output parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Sure! Here you go: {"a": 1} hope it helps', {"a": 1}),
        ("[1, 2]", [1, 2]),
        ("not json at all", None),
        ("", None),
        (None, None),
        ("{" * 5000, None),
    ],
)
def test_extract_json_object(text, expected):
    assert extract_json_object(text) == expected


def test_parse_draft_item_is_defensive():
    assert parse_draft_item("x") is None and parse_draft_item(None) is None
    item = parse_draft_item({"options": [1, 2.5, None, True], "correct_index": "B", "difficulty": " HARD ", "why_others_wrong": ["a", 3]})
    assert item.options == ["1", "2.5", "None", "True"] and item.correct_index == 1 and item.difficulty == "hard"
    assert item.why_others_wrong == ["a"] and item.kind == "concept"
    assert parse_draft_item({"correct_index": True}).correct_index == -1
    assert parse_draft_item({"options": "abcd"}).options == []


def test_parse_replies():
    assert parse_draft_reply('{"questions": [{"a": 1}]}') == [{"a": 1}]
    assert parse_draft_reply('{"questions": "x"}') == [] and parse_draft_reply("garbage") == []
    answers = parse_check_reply('{"answers": [{"id": "q1", "answer_index": "2", "confidence": 7}, {"id": ""}, "x", {"id": "q2", "confidence": "high"}]}')
    assert answers["q1"]["answer"] == 2 and answers["q1"]["confidence"] == 1.0
    assert answers["q2"]["confidence"] == 0.0 and answers["q2"]["single_correct"] is False
    assert parse_check_reply("[]") == {}


# --------------------------------------------------------------------------- #
# Refill job
# --------------------------------------------------------------------------- #
def job_env(**overrides):
    e = Env(**overrides)
    e.jobs = f.Jobs()
    e.contexts = f.Contexts()
    for module in e.courses.modules[e.course.id]:
        lessons = [l for l in e.courses.lessons[e.course.id] if l.module_id == module.id]
        e.contexts.rows[module.id] = f.ModuleContextRecord(module.id, e.course.id, {
            "module_title": module.title,
            "lessons": [{"lesson_id": l.key, "title": l.title, "summary": f"{l.key} teaches compilers.",
                         "key_points": [f"The {l.key} compiler turns source into bytecode"]} for l in lessons],
        }, "h")  # fmt: skip
    e.uow = f.Uow()
    e.pool = PoolService(e.courses, e.questions, e.uow, e.clock, e.settings)
    e.generation = GenerationService(e.courses, e.questions, e.contexts, e.jobs, e.pool, e.pipeline, e.llm, e.uow, e.clock, e.settings)
    return e


def drafts_for(e, count, start=0, extra=0):
    def make(message):
        lessons = json.loads(message.split("Lessons (JSON):\n", 1)[1].split("\n", 1)[0])
        out = []
        for i in range(count + extra):
            n = start + len(e.llm.calls) * 100 + i
            words = " ".join(random.Random(n).sample([f"w{k}" for k in range(60)], 8))
            out.append({"prompt": f"Pick {words}", "options": [f"Right {n}", f"Wrong a{n}", f"Wrong b{n}", f"Wrong c{n}"],
                        "correct_index": 0, "explanation": "x", "difficulty": "easy", "kind": "concept",
                        "lesson_id": lessons[0]["lesson_id"], "concept": f"c{n}", "source_fact": lessons[0]["key_points"][0],
                        "why_others_wrong": ["a", "b", "c"]})  # fmt: skip
        return json.dumps({"questions": out})

    return make


class DraftingLlm(Llm):
    def __init__(self, maker):
        super().__init__()
        self.maker = maker
        self.check_answers = agree

    async def complete_json(self, provider, system, user, temperature, max_tokens):
        if "Write exactly" in user:
            self.calls.append((provider, "draft", temperature))
            if self.failures:
                raise self.failures.pop(0)
            return Reply(self.maker(user), provider)
        return await super().complete_json(provider, system, user, temperature, max_tokens)


def run_job(e):
    job, created = run(e.generation.start_job(e.course.id, "admin"))
    assert created
    return run(e.generation.run_job(job))


def test_job_fills_to_target_and_caps_oversized_replies():
    e = job_env()
    e.llm = DraftingLlm(drafts_for(e, 5, extra=15))  # model returns 20 instead of 5
    e.pipeline._llm = e.generation._llm = e.llm
    job = run_job(e)
    assert job.status == "done"
    live = run(e.questions.live_counts(e.course.id))
    targets = run(e.pool.state(e.course.id)).targets
    assert all(live[m] >= t for m, t in targets.items())
    assert all(live[m] <= t + 4 for m, t in targets.items()), "a 20-question reply can't overshoot"
    assert job.progress["modules_done"] == job.progress["modules_total"]


def test_job_without_a_second_provider_pauses_immediately():
    e = job_env()
    e.llm = DraftingLlm(drafts_for(e, 5))
    e.llm.providers = ["groq"]
    e.pipeline._llm = e.generation._llm = e.llm
    job = run_job(e)
    assert job.status == "paused" and e.llm.calls == []


def test_job_survives_garbage_replies_and_failed_calls():
    e = job_env()
    e.llm = DraftingLlm(lambda message: "I cannot help with that")
    e.pipeline._llm = e.generation._llm = e.llm
    job = run_job(e)
    assert job.status == "done" and job.created == 0
    assert len(e.llm.calls) <= 30 * 2, "bounded calls per module"
    e2 = job_env()
    e2.llm = DraftingLlm(drafts_for(e2, 5))
    e2.llm.failures = [ProviderFailure("gemini", "x")] * 3
    e2.pipeline._llm = e2.generation._llm = e2.llm
    job = run_job(e2)
    assert job.status == "done" and any("failed 3 times" in err for err in job.errors)


def test_job_records_failure_without_raising():
    e = job_env()
    e.llm = DraftingLlm(drafts_for(e, 5))
    e.pipeline._llm = e.generation._llm = e.llm

    async def boom(course_id):
        raise RuntimeError("db exploded")

    e.contexts.get_many = boom
    job = run_job(e)
    assert job.status == "failed" and job.finished_at is not None


def test_only_one_running_job_per_course_and_stale_jobs_are_replaced():
    e = job_env()
    job, created = run(e.generation.start_job(e.course.id, "admin"))
    again, created_again = run(e.generation.start_job(e.course.id, "admin"))
    assert created and not created_again and again.id == job.id
    e.clock.advance(minutes=16)
    fresh, created = run(e.generation.start_job(e.course.id, "admin"))
    assert created and fresh.id != job.id and e.jobs.rows[job.id].status == "failed"


def test_missing_context_is_reported_per_module():
    e = job_env()
    e.contexts.rows.clear()
    e.llm = DraftingLlm(drafts_for(e, 5))
    e.pipeline._llm = e.generation._llm = e.llm
    job = run_job(e)
    assert job.status == "done" and sum("no exam context" in err for err in job.errors) == 2


def test_over_served_questions_are_retired_when_configured():
    e = job_env(exam_max_serves=3)
    e.llm = DraftingLlm(drafts_for(e, 5))
    e.pipeline._llm = e.generation._llm = e.llm
    q = e.questions.add(e.course, e.module, e.lesson, 900)
    q.times_served = 3
    run_job(e)
    assert e.questions.rows[q.id].status == "retired"


def test_seed_import_accepts_code_shown_in_the_prompt():
    e = Env(slug="spring-boot")  # a course whose code can't even be executed
    q = draft(1, lesson_ref=e.lesson.key, kind="code_output", why_others_wrong=[], source_fact=None,
              prompt="What does this print?\n\n```java\nint x = 3;\nSystem.out.println(x * 2);\n```",
              options=["6", "5", "9", "32"])  # fmt: skip
    c = e.verify([q], mode="import")[0]
    assert c.status == "live" and c.checks["executed"] == {"ran": False, "reason": "code_in_prompt"}
    assert c.checks["checker"] == "deferred" and e.runner.runs == 0
    # Outside imports the code field is still required.
    assert e.verify([draft(2, lesson_ref=e.lesson.key, kind="code_output", prompt=q.prompt, options=q.options)], mode="manual")[0].check == "shape"
