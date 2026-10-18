"""Question pool generation and verification against a real Postgres database
with fake models (no quota is ever spent): fill to target, the automatic
checks, quota pauses, import, manual questions and the admin pool endpoints.

Needs EXAMS_TEST_DATABASE_URL; skipped otherwise."""

import hashlib
import json
import math
import random
import re

import pytest

from exams import dependencies as exam_dependencies
from exams.dependencies import ExamServices
from exams.errors import QuotaExhausted
from exams.infra.llm_client import LlmReplyData
from exams.services import verification as verification_module
from exams.services.generation_service import GenerationService
from exams.services.verification import VerificationPipeline
from tests.exam_harness import FakeClock, Harness, require_db, reset_process_state, run, make_settings


VOCABULARY = (
    "classes objects interfaces generics streams lambdas threads locks arrays strings maps sets lists queues "
    "exceptions packages modules records enums annotations reflection sockets files buffers iterators loops "
    "switches casts primitives wrappers boxing heaps stacks frames bytecode loaders verifiers compilers"
).split()


class FakeLlm:
    """Drafts grounded questions from the lesson material in the prompt and
    answers checks from what it drafted. Behaviour switches per test."""

    def __init__(self, providers=("gemini", "groq")) -> None:
        self.providers = list(providers)
        self.calls: list[tuple[str, str, int]] = []  # (purpose, provider, requested)
        self.key: dict[str, str] = {}
        self.checker_mode = "agree"
        self.quota_on_draft = False
        self.duplicate_batch = False
        self.ungrounded = False
        self.counter = 0

    def available_providers(self):
        return list(self.providers)

    async def complete_json(self, provider, system_prompt, user_message, temperature, max_tokens):
        if "Write exactly" in user_message:
            self.calls.append(("draft", provider, int(re.search(r"Write exactly (\d+)", user_message).group(1))))
            if self.quota_on_draft:
                raise QuotaExhausted(provider, "quota", retry_after=None)
            return self._reply(provider, self._draft(user_message))
        self.calls.append(("check", provider, 0))
        return self._reply(provider, self._check(user_message))

    def _reply(self, provider, payload):
        return LlmReplyData(json.dumps(payload), provider, f"{provider}-model", 100, 200, None)

    def _draft(self, message):
        lessons = json.loads(message.split("Lessons (JSON):\n", 1)[1].split("\n", 1)[0])
        count = int(re.search(r"Write exactly (\d+)", message).group(1))
        questions = []
        for i in range(count):
            self.counter += 1
            n = 0 if self.duplicate_batch else self.counter
            lesson = lessons[i % len(lessons)]
            words = random.Random(n).sample(VOCABULARY, 7)
            prompt = f"Which statement about {' '.join(words)} is correct"
            right = f"Answer alpha {n}"
            self.key[prompt] = right
            questions.append({
                "prompt": prompt,
                "options": [right, f"Answer bravo {n}", f"Answer charlie {n}", f"Answer delta {n}"],
                "correct_index": 0, "explanation": "Because the lesson says so.",
                "difficulty": ("easy", "medium", "hard", "medium", "easy")[i % 5], "kind": "concept",
                "lesson_id": lesson["lesson_id"], "concept": f"concept {n}",
                "source_fact": "Unicorns write the bytecode" if self.ungrounded else lesson["key_points"][0],
                "why_others_wrong": ["no", "nope", "never"],
            })  # fmt: skip
        return {"questions": questions}

    def _check(self, message):
        questions = json.loads(message.split("Questions (JSON):\n", 1)[1].split("\n\nReturn", 1)[0])
        answers = []
        for q in questions:
            right = self.key.get(q["question"]) or next((o for o in q["options"] if o.startswith("Right ")), None)
            index = q["options"].index(right) if right in q["options"] else 0
            if self.checker_mode == "disagree":
                index = (index + 1) % 4
            answers.append({
                "id": q["id"], "answer_index": index, "single_correct": self.checker_mode != "ambiguous",
                "version_issue": False, "confidence": 0.5 if self.checker_mode == "unsure" else 0.95,
            })  # fmt: skip
        return {"answers": answers}


class FakeEmbedder:
    async def embed(self, texts):
        vectors = []
        for text in texts:
            rng = random.Random(hashlib.sha256(text.encode()).hexdigest())
            vectors.append([rng.gauss(0, 1) for _ in range(768)])
        return vectors, 10


class NoRunner:
    def supports(self, language):
        return False

    def is_external(self, language):
        return False

    async def run(self, language, code):
        raise AssertionError("no code should run in these tests")


@pytest.fixture
def gen(monkeypatch):
    require_db()
    reset_process_state()
    monkeypatch.setattr(verification_module, "CALL_RETRY_DELAYS", (0.0, 0.0))
    clock = FakeClock()
    monkeypatch.setattr(exam_dependencies, "clock", clock)
    h = Harness(make_settings(exam_pool_target=12, exam_pool_min_per_module=3, exam_question_count=10))
    run(h.reset())
    course = run(h.add_course("java-core", "JAVA", [2, 1], enabled=True))
    contexts = {
        "modules": [
            {
                "course_slug": "java-core", "course_title": "Java Core", "module_id": m["key"], "module_title": m["title"],
                "lessons": [
                    {"lesson_id": l["key"], "title": f"Lesson {l['key']}",
                     "summary": f"The {l['key']} lesson explains compilers. The JVM runs bytecode files safely.",
                     "key_points": [f"The javac compiler of {l['key']} turns source into bytecode"],
                     "avoid_prompts": ["What does the public quiz ask about javac?"]}
                    for l in m["lessons"]
                ],
            }
            for m in course["modules"]
        ]
    }  # fmt: skip
    yield h, clock, course, contexts
    reset_process_state()
    run(h.engine.dispose())


def admin(h):
    run(h.add_user("boss", role="admin"))
    return {"X-Test-User": "boss"}


def upload_context(h, contexts):
    headers = admin(h) if "boss" not in h.users else {"X-Test-User": "boss"}
    with h.client() as client:
        response = client.put("/api/admin/learning/courses/java-core/exam-context", json=contexts, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def run_job(h, llm, clock):
    async def go():
        async with h.sessions() as session:
            services = ExamServices(session, h.settings)
            pipeline = VerificationPipeline(
                services.questions, services.logs, llm, FakeEmbedder(), NoRunner(), clock, random.Random(1), h.settings
            )
            generation = GenerationService(
                services.courses, services.questions, services.contexts, services.jobs, services.pool, pipeline,
                llm, services.uow, clock, h.settings,
            )  # fmt: skip
            course = await services.courses.get_course("java-core")
            job, created = await generation.start_job(course.id, "admin")
            assert created
            return await generation.run_job(job)

    return run(go())


def live_counts(h):
    return dict(run(h.rows("select m.key, count(q.id) from course_modules m left join exam_questions q on q.module_id = m.id and q.status = 'live' group by m.key")))


def test_job_fills_each_module_to_its_share_then_stops(gen):
    h, clock, course, contexts = gen
    upload_context(h, contexts)
    llm = FakeLlm()
    job = run_job(h, llm, clock)
    assert job.status == "done", job.errors
    counts = live_counts(h)
    assert counts[course["modules"][0]["key"]] >= 8 and counts[course["modules"][1]["key"]] >= 4
    drafts = [c for c in llm.calls if c[0] == "draft"]
    assert all(requested == 5 for _, _, requested in drafts), "every drafting call asks for 5"
    assert {p for kind, p, _ in llm.calls if kind == "draft"} == {"gemini"}
    assert {p for kind, p, _ in llm.calls if kind == "check"} == {"groq"}, "the drafter never checks itself"
    assert len(drafts) == math.ceil(8 / 5) + math.ceil(4 / 5)
    checks = run(h.scalar("select checks from exam_questions where status = 'live' limit 1"))
    assert checks["grounded"] is True and checks["checker"] == "passed" and "similarity" in checks["duplicate"]
    # The pool is full: the exam becomes visible, and a second run makes no calls.
    with h.client() as client:
        assert client.get("/api/learning/courses/java-core/exam").json()["enabled"] is True
    before = len(llm.calls)
    run_job(h, llm, clock)
    assert len(llm.calls) == before
    # Answer balance: correct answers are not always option A.
    positions = {r[0] for r in run(h.rows("select correct_index from exam_questions where status = 'live'"))}
    assert len(positions) > 1


@pytest.mark.parametrize("mode,reason", [("disagree", "different answer"), ("unsure", "confidence"), ("ambiguous", "more than one")])
def test_checker_problems_reject_and_nothing_goes_live(gen, mode, reason):
    h, clock, _, contexts = gen
    upload_context(h, contexts)
    llm = FakeLlm()
    llm.checker_mode = mode
    job = run_job(h, llm, clock)
    assert job.status == "done" and job.created == 0
    assert run(h.scalar("select count(*) from exam_questions where status = 'live'")) == 0
    reasons = [r[0] for r in run(h.rows("select reject_reason from exam_questions where status = 'rejected'"))]
    assert reasons and all(reason in r for r in reasons), reasons
    # bounded: a module whose drafts keep failing can't burn the day's budget
    assert len([c for c in llm.calls if c[0] == "draft"]) <= 2 * 3 + 1 * 3


def test_ungrounded_and_in_batch_duplicates_are_rejected(gen):
    h, clock, _, contexts = gen
    upload_context(h, contexts)
    llm = FakeLlm()
    llm.ungrounded = True
    run_job(h, llm, clock)
    assert run(h.scalar("select count(*) from exam_questions where status = 'live'")) == 0
    assert run(h.scalar("select count(*) from exam_questions where reject_reason like 'source_fact%'")) > 0

    run(h.execute("delete from exam_questions"))
    llm = FakeLlm()
    llm.duplicate_batch = True
    run_job(h, llm, clock)
    # Every call drafts 5 copies of the same question: at most one goes live.
    assert run(h.scalar("select count(*) from exam_questions where status = 'live'")) == 1
    assert run(h.scalar("select count(*) from exam_questions where reject_reason like 'duplicate%'")) > 0


def test_quota_exhaustion_pauses_without_a_retry_storm(gen):
    h, clock, course, contexts = gen
    upload_context(h, contexts)
    llm = FakeLlm()
    llm.quota_on_draft = True
    job = run_job(h, llm, clock)
    assert job.status == "paused" and job.paused_until is not None and job.paused_until > clock.now()
    assert len(llm.calls) == 1, "a quota refusal is never retried hot"
    # The fill job skips a paused course until paused_until.
    async def due():
        async with h.sessions() as session:
            return await ExamServices(session, h.settings).maintenance_service().courses_needing_fill()

    assert run(due()) == []
    clock.advance(hours=2)
    assert [c.slug for c in run(due())] == ["java-core"]


def test_daily_call_limit_and_missing_checker_pause_before_any_call(gen):
    h, clock, course, contexts = gen
    upload_context(h, contexts)
    h.settings.exam_gen_daily_call_limit = 3
    for _ in range(3):
        run(h.execute("insert into exam_generation_logs (purpose, provider) values ('draft', 'gemini')"))
    llm = FakeLlm()
    job = run_job(h, llm, clock)
    assert job.status == "paused" and llm.calls == [] and "daily_call_limit" in job.errors[-1]

    h.settings.exam_gen_daily_call_limit = 300
    single = FakeLlm(providers=("gemini",))
    job = run_job(h, single, clock)
    assert job.status == "paused" and single.calls == [] and "no_independent_checker" in job.errors[-1]


def test_import_checks_lines_and_defers_the_cross_check(gen):
    h, clock, course, contexts = gen
    upload_context(h, contexts)
    module, other = course["modules"][0], course["modules"][1]
    lesson = module["lessons"][0]

    def line(n, **overrides):
        item = {
            "course_slug": "java-core", "module_id": module["key"], "lesson_id": lesson["key"], "concept": f"seed {n}",
            "kind": "concept", "difficulty": "easy", "prompt": f"Seed question number {n} about compilers and bytecode",
            "options": [f"Right {n}", f"Wrong A{n}", f"Wrong B{n}", f"Wrong C{n}"], "correct_index": 0,
            "explanation": "Because.",
        }  # fmt: skip
        item.update(overrides)
        return json.dumps(item)

    body = "\n".join([
        line(1),
        line(2, module_id=other["key"]),                    # lesson is not in this module
        line(3, course_slug="python"),                      # unknown course
        line(1),                                            # duplicate of line 1
        "{not json",
        line(6, options=["only", "three", "options"]),
        line(7, source_fact="Unicorns write bytecode"),     # ungrounded
        line(8, source_fact=f"The javac compiler of {lesson['key']} turns source into bytecode"),
    ])  # fmt: skip
    with h.client() as client:
        response = client.post("/api/admin/learning/exam-questions/import", content=body.encode(), headers={"X-Test-User": "boss"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["imported"] == 2
    assert {r["line"]: r["reason"] for r in result["rejected"]} == {
        2: "course_mismatch", 3: "unknown_course", 4: "duplicate_exact", 5: "not_an_object",
        6: "shape: there must be exactly 4 text options", 7: "source_fact not found in the lesson material",
    }  # fmt: skip
    rows = run(h.rows("select source, checks->>'checker', status from exam_questions where status = 'live'"))
    assert rows and all(r == ("seed", "deferred", "live") for r in rows)

    # The refill job cross-checks deferred seed questions; failing ones are quarantined.
    llm = FakeLlm()
    llm.checker_mode = "disagree"
    run_job(h, llm, clock)
    assert run(h.scalar("select count(*) from exam_questions where source = 'seed' and status = 'quarantined'")) == 2


def test_manual_question_admin_views_enable_and_context(gen):
    h, clock, course, contexts = gen
    headers = admin(h)
    module = course["modules"][0]
    with h.client() as client:
        bad_ctx = client.put(
            "/api/admin/learning/courses/java-core/exam-context",
            json={"modules": [{"module_id": "mod_unknown", "lessons": []}]}, headers=headers,
        )  # fmt: skip
        assert bad_ctx.status_code == 422 and "mod_unknown" in bad_ctx.json()["detail"]
        assert client.put("/api/admin/learning/courses/java-core/exam-context", json=contexts, headers=headers).json()["updated"] == 2
        assert client.put("/api/admin/learning/courses/java-core/exam-context", json=contexts, headers=headers).json()["unchanged"] == 2

        payload = {
            "course_slug": "java-core", "module_id": module["key"], "lesson_id": module["lessons"][0]["key"],
            "concept": "manual concept", "prompt": "Which tool compiles Java source files", "difficulty": "easy",
            "options": ["javac", "java", "jar", "javadoc"], "correct_index": 0, "explanation": "javac compiles.",
            "source_fact": f"The javac compiler of {module['lessons'][0]['key']} turns source into bytecode",
        }  # fmt: skip
        created = client.post("/api/admin/learning/exam-questions", json=payload, headers=headers)
        assert created.status_code == 201, created.text
        item = created.json()
        # No model keys in the test settings: live, with the check recorded as skipped.
        assert item["status"] == "live" and item["checks"]["checker"] == "skipped_no_quota" and item["checks"]["grounded"] is True
        assert item["module_id"] == module["key"] and item["lesson_id"] == module["lessons"][0]["key"]
        assert client.post("/api/admin/learning/exam-questions", json=payload, headers=headers).json()["status"] == "rejected"
        wrong_lesson = client.post("/api/admin/learning/exam-questions", json={**payload, "lesson_id": "lesson_nope", "prompt": "Other"}, headers=headers)
        assert wrong_lesson.status_code == 422

        listed = client.get("/api/admin/learning/exam-questions?course=java-core&status=live", headers=headers).json()["items"]
        assert [q["id"] for q in listed] == [item["id"]]
        assert listed[0]["options"][listed[0]["correct_index"]] == "javac"  # options were re-shuffled for balance
        assert client.get(f"/api/admin/learning/exam-questions?course=java-core&module={module['key']}", headers=headers).status_code == 200
        summary = client.get("/api/admin/learning/exam-questions/summary?course=java-core", headers=headers).json()
        assert summary["target"] == 12 and summary["live"] == 1 and summary["ready"] is False
        assert summary["modules"][0]["module_id"] == module["key"] and summary["modules"][0]["rejected"] == 1

        # Edit re-runs the checks; restore brings a quarantined question back.
        edited = client.put(f"/api/admin/learning/exam-questions/{item['id']}", json={"prompt": "Which JDK tool compiles .java files"}, headers=headers)
        assert edited.status_code == 200 and edited.json()["prompt"] == "Which JDK tool compiles .java files" and edited.json()["status"] == "live"
        run(h.execute("update exam_questions set status = 'quarantined' where id = :q", q=item["id"]))
        restored = client.put(f"/api/admin/learning/exam-questions/{item['id']}", json={"status": "live"}, headers=headers)
        assert restored.json()["status"] == "live"

        # A rewritten lesson quarantines questions it no longer supports.
        changed = json.loads(json.dumps(contexts))
        for lesson in changed["modules"][0]["lessons"]:
            lesson["key_points"] = ["Completely different material about lambdas"]
            lesson["summary"] = "Lambdas and streams."
        assert client.put("/api/admin/learning/courses/java-core/exam-context", json=changed, headers=headers).json()["quarantined"] == 1

        # Exam switch: code clash, then enabling starts the fill job.
        run(h.add_course("python", "PY", [1]))
        clash = client.put("/api/admin/learning/courses/java-core/exam", json={"enabled": True, "code": "PY"}, headers=headers)
        assert clash.status_code == 409 and clash.json()["code"] == "conflict"
        assert client.put("/api/admin/learning/courses/java-core/exam", json={"enabled": True, "code": "bad code"}, headers=headers).status_code == 422
        enabled = client.put("/api/admin/learning/courses/java-core/exam", json={"enabled": True, "code": "JAVA"}, headers=headers)
        assert enabled.status_code == 200 and enabled.json()["enabled"] is True and enabled.json()["ready"] is False
        job_id = enabled.json()["job_id"]
        assert job_id
        job = client.get(f"/api/admin/learning/exams/jobs/{job_id}", headers=headers).json()
        assert job["status"] in ("running", "paused", "done")
        assert client.get("/api/learning/courses/java-core/exam").json()["enabled"] is False, "hidden until the pool is ready"
        quota = client.get("/api/admin/learning/exams/quota", headers=headers).json()["models"]
        assert {m["provider"] for m in quota} == {"gemini", "groq"}
        assert client.get("/api/admin/learning/exam-questions/summary?course=java-core").status_code == 401
