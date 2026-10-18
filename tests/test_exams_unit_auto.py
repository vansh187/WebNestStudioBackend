"""Automatic exams (EXAM_ENABLED=true): lesson content -> exam context,
exam code assignment, switching courses on, and the admin guard."""

import asyncio
import os
import uuid
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest

from exams.domain.auto_context import build_module_context, lesson_text
from exams.domain.validation import context_passages, is_grounded
from exams.errors import ExamConflict
from exams.records import LessonMaterial
from exams.services.auto_exam_service import AutoExamService, pick_exam_code
from exams.services.context_provider import ModuleContextProvider
from tests import exam_fakes as f
from tests.test_exams_unit_admin import AdminWorld

ADMIN = uuid.uuid4()


def run(coro):
    return asyncio.run(coro)


HTML = """
<h2>Variables</h2>
<p>A variable in Java has a type that is fixed when it is declared. The compiler rejects assigning a String to an int.</p>
<script>alert('not lesson text')</script><style>.x{color:red}</style>
<ul><li>Local variables must be initialised before they are read.</li><li>Short</li></ul>
<p>&lt;tags&gt; are escaped &amp; decoded correctly in this lesson sentence here.</p>
"""


def material(content, key="lesson_vars", prompts=()):
    return LessonMaterial(uuid.uuid4(), key, uuid.uuid4(), "Variables", content, list(prompts))


# --------------------------------------------------------------------------- #
# Lesson content -> context
# --------------------------------------------------------------------------- #
def test_html_lessons_become_grounded_context():
    text, items = lesson_text({"format": "html", "body": HTML})
    assert "alert" not in text and "color:red" not in text
    assert "<tags> are escaped & decoded" in text
    assert items == ["Local variables must be initialised before they are read.", "Short"]
    ctx = build_module_context("java-core", "Java", "mod_basics", "Basics", [material({"format": "html", "body": HTML}, prompts=["What is a variable?"])])
    lesson = ctx["lessons"][0]
    assert lesson["lesson_id"] == "lesson_vars" and lesson["avoid_prompts"] == ["What is a variable?"]
    assert lesson["key_points"][0] == "Local variables must be initialised before they are read."
    assert "Short" not in lesson["key_points"], "fragments under 5 words aren't key points"
    # A fact quoted from the lesson grounds; an invented one doesn't.
    passages = context_passages(ctx)
    assert is_grounded("The compiler rejects assigning a String to an int", passages)
    assert not is_grounded("Python lists are implemented as linked lists", passages)


def test_markdown_lessons():
    body = "# Loops\n\nA `for` loop repeats a block a known number of times.\n\n- The `while` loop checks its condition first.\n- [Docs](http://x) explain the do-while loop in detail.\n\n```java\nfor(;;){}\n```"
    text, items = lesson_text({"format": "markdown", "body": body})
    assert "for(;;)" not in text and "http://x" not in text
    assert items[0] == "The while loop checks its condition first."
    assert "Docs explain the do-while loop in detail." in items[1]


@pytest.mark.parametrize("content", [None, {}, {"body": ""}, {"body": "   "}, {"body": 42}, {"format": "html", "body": "<p>Too short.</p>"}, "not a dict"])
def test_lessons_without_usable_text_give_no_context(content):
    assert build_module_context("s", "t", "m", "M", [material(content)]) is None


def test_malformed_html_never_crashes():
    text, _ = lesson_text({"format": "html", "body": "<p>Unclosed <b>tags <div> everywhere and <<<>>> odd brackets in this long sentence"})
    assert "Unclosed" in text


def test_lesson_without_key_uses_its_uuid():
    m = material({"format": "html", "body": "<p>" + "A long enough sentence about generics in Java code. " * 3 + "</p>"}, key=None)
    assert build_module_context("s", "t", "m", "M", [m])["lessons"][0]["lesson_id"] == str(m.lesson_id)


# --------------------------------------------------------------------------- #
# Exam codes and switching courses on
# --------------------------------------------------------------------------- #
def test_pick_exam_code():
    assert pick_exam_code("java-core", None, set()) == "JAVA"
    assert pick_exam_code("java-core", "MYJAVA", set()) == "MYJAVA", "a valid existing code is kept"
    assert pick_exam_code("java-core", None, {"JAVA"}) == "JAVA2"
    assert pick_exam_code("java-core", None, {"JAVA", "JAVA2"}) == "JAVA3"
    assert pick_exam_code("kubernetes-advanced", None, set()) == "KUBERNETESAD"
    long_taken = {"KUBERNETESAD"}
    code = pick_exam_code("kubernetes-advanced", None, long_taken)
    assert code == "KUBERNETESA2" and len(code) <= 12
    assert pick_exam_code("java-core", "bad code", set()) == "JAVA"


def make_auto(**overrides):
    courses = f.Courses()
    settings = f.settings(exam_enabled=True, **overrides)
    return courses, AutoExamService(courses, f.Uow(), settings)


def test_enable_all_switches_on_every_published_course():
    courses, auto = make_auto(exam_auto_exclude=" Python , ")
    java = courses.add(slug="java-core", code=None, enabled=False)
    courses.add(slug="python", code=None, enabled=False)
    courses.add(slug="react", code="REACT", enabled=True)
    courses.add(slug="draft-course", code=None, enabled=False, status="draft")
    courses.add(slug="java-advanced-x", code="JAVA", enabled=False)  # already holds JAVA
    switched = run(auto.enable_all())
    assert sorted(c.slug for c in switched) == ["java-advanced-x", "java-core"]
    assert courses.courses["java-core"].exam_enabled and courses.courses["java-core"].exam_code == "JAVA2"
    assert courses.courses["java-advanced-x"].exam_code == "JAVA", "keeps its own code"
    assert not courses.courses["python"].exam_enabled, "excluded"
    assert not courses.courses["draft-course"].exam_enabled, "unpublished courses stay off"
    assert run(auto.enable_all()) == [], "idempotent"
    assert java.id in {c.id for c in run(courses.list_enabled_courses())}


def test_enable_all_does_nothing_when_off():
    courses = f.Courses()
    courses.add(code=None, enabled=False)
    assert run(AutoExamService(courses, f.Uow(), f.settings(exam_enabled=False)).enable_all()) == []


def test_one_failing_course_doesnt_stop_the_others():
    courses, auto = make_auto()
    courses.add(slug="java-core", code=None, enabled=False)
    courses.add(slug="python", code=None, enabled=False)
    original = courses.set_exam_settings

    async def flaky(course_id, enabled, code):
        if code == "JAVA":
            raise ExamConflict("taken concurrently")
        await original(course_id, enabled, code)

    courses.set_exam_settings = flaky
    switched = run(auto.enable_all())
    assert [c.slug for c in switched] == ["python"]


# --------------------------------------------------------------------------- #
# Refill job uses lesson content; admin guard
# --------------------------------------------------------------------------- #
def lesson_world(exam_enabled=True):
    courses = f.Courses()
    course = courses.add(module_lessons=(1, 1))
    m0, m1 = courses.modules[course.id]
    l0, l1 = courses.lessons[course.id][0], courses.lessons[course.id][1]
    body = {"format": "html", "body": "<p>" + "The JVM loads classes lazily when they are first used. " * 2 + "</p>"}
    courses.materials[course.id] = [
        LessonMaterial(l0.id, l0.key, m0.id, l0.title, body, []),
        LessonMaterial(l1.id, l1.key, m1.id, l1.title, body, []),
    ]
    contexts, questions = f.Contexts(), f.Questions()
    settings = f.settings(exam_enabled=exam_enabled)
    provider = ModuleContextProvider(courses, contexts, questions, f.Clock(), settings)
    return SimpleNamespace(courses=courses, course=course, m0=m0, m1=m1, l0=l0, contexts=contexts, questions=questions,
                           settings=settings, provider=provider)  # fmt: skip


def test_lesson_built_context_only_with_exam_enabled_and_uploads_win():
    w = lesson_world()
    w.contexts.rows[w.m1.id] = f.ModuleContextRecord(w.m1.id, w.course.id, {"lessons": [], "source": "upload"}, "h")
    built = run(w.provider.contexts(w.course))
    assert built[w.m0.id]["source"] == "auto" and built[w.m0.id]["lessons"][0]["lesson_id"] == w.l0.key
    assert built[w.m1.id]["source"] == "upload", "an uploaded context always wins"
    off = lesson_world(exam_enabled=False)
    assert run(off.provider.contexts(off.course)) == {}, "EXAM_ENABLED=false: uploaded contexts only, as before"
    assert run(off.provider.refresh(off.course)) == 0 and off.contexts.rows == {}


def test_refresh_stores_auto_context_and_regrounds_when_lessons_change():
    w = lesson_world()
    assert run(w.provider.refresh(w.course)) == 0
    assert w.contexts.rows[w.m0.id].context["source"] == "auto"
    q = w.questions.add(w.course, w.m0, w.l0, 1)
    q.source_fact = "The JVM loads classes lazily when they are first used"
    assert run(w.provider.refresh(w.course)) == 0, "unchanged lessons: nothing re-checked"
    w.courses.materials[w.course.id][0] = LessonMaterial(
        w.l0.id, w.l0.key, w.m0.id, w.l0.title,
        {"format": "html", "body": "<p>" + "Records are immutable data carriers with generated accessors. " * 2 + "</p>"}, [],
    )  # fmt: skip
    assert run(w.provider.refresh(w.course)) == 1
    assert q.status == "quarantined", "the rewritten lesson no longer supports it"


def test_admin_paths_ground_against_the_same_material_as_generation():
    a = AdminWorld()
    a.settings.exam_enabled = True
    a.courses.materials[a.course.id] = [LessonMaterial(
        a.lesson.id, a.lesson.key, a.module.id, a.lesson.title,
        {"format": "html", "body": "<p>" + "The javac compiler turns source files into bytecode for the JVM. " * 2 + "</p>"}, [],
    )]  # fmt: skip
    ok = run(a.service.create_manual(a.payload(1, source_fact="The javac compiler turns source files into bytecode"), ADMIN))
    assert ok["status"] == "live" and ok["checks"]["grounded"] is True
    bad = run(a.service.create_manual(a.payload(2, source_fact="Python lists are implemented as linked lists"), ADMIN))
    assert bad["status"] == "rejected" and "source_fact" in bad["reject_reason"]


def test_admin_switch_off_is_remembered_and_automation_respects_it():
    a = AdminWorld()
    a.settings.exam_enabled = True
    course, _ = run(a.service.set_exam("java-core", False, None, ADMIN))
    assert course.exam_enabled is False and course.exam_admin_disabled is True
    auto = AutoExamService(a.courses, f.Uow(), a.settings)
    assert run(auto.enable_all()) == [], "an admin's 'off' is never overridden"
    course, _ = run(a.service.set_exam("java-core", True, None, ADMIN))
    assert course.exam_enabled and not course.exam_admin_disabled
    run(a.courses.set_exam_settings(course.id, False, course.exam_code))  # off, but not by an admin
    assert [c.slug for c in run(auto.enable_all())] == ["java-core"]


def test_draft_courses_can_be_switched_off_or_recoded_freely():
    a = AdminWorld()
    a.settings.exam_enabled = True
    a.courses.add(slug="draft-course", code=None, enabled=False, status="draft")
    course, _ = run(a.service.set_exam("draft-course", False, "NEWCODE", ADMIN))
    assert course.exam_code == "NEWCODE" and not course.exam_enabled


def test_exclusion_is_case_insensitive():
    courses, auto = make_auto(exam_auto_exclude="Database-SQL")
    courses.add(slug="Database-SQL", code=None, enabled=False)
    assert run(auto.enable_all()) == []


def test_a_course_whose_code_cant_be_picked_doesnt_stop_the_rest(monkeypatch):
    from exams.services import auto_exam_service

    courses, auto = make_auto()
    courses.add(slug="java-core", code=None, enabled=False)
    courses.add(slug="python", code=None, enabled=False)
    real = auto_exam_service.pick_exam_code

    def picky(slug, current, used):
        if slug == "java-core":
            raise RuntimeError("no free exam code")
        return real(slug, current, used)

    monkeypatch.setattr(auto_exam_service, "pick_exam_code", picky)
    assert [c.slug for c in run(auto.enable_all())] == ["python"]


def test_listing_failure_returns_cleanly():
    courses, auto = make_auto()

    async def down():
        raise RuntimeError("db down")

    courses.list_published_courses = down
    assert run(auto.enable_all()) == []


# --------------------------------------------------------------------------- #
# Content extraction edge cases (#6, #8)
# --------------------------------------------------------------------------- #
def test_nested_list_items_keep_the_outer_text():
    _, items = lesson_text({"format": "html", "body": "<ul><li>The JVM verifies bytecode before running <ul><li>stack maps are checked</li></ul> it at load time</li></ul>"})
    assert "stack maps are checked" in items
    outer = next(i for i in items if i.startswith("The JVM"))
    assert outer.endswith("it at load time") and len(outer.split()) >= 5


def test_unclosed_list_items_are_kept():
    _, items = lesson_text({"format": "html", "body": "<ul><li>An item that is never closed in the markup"})
    assert items == ["An item that is never closed in the markup"]


def test_long_lessons_are_covered_end_to_end():
    sentences = [f"Fact number {i} explains a distinct idea about generics." for i in range(400)]
    ctx = build_module_context("s", "t", "m", "M", [material({"format": "html", "body": "<p>" + " ".join(sentences) + "</p>"})])
    lesson = ctx["lessons"][0]
    assert len(lesson["summary"]) <= 3000 and lesson["summary"].endswith("generics."), "cut on a sentence"
    assert len(lesson["key_points"]) == 40
    assert lesson["key_points"][-1].startswith("Fact number 39"), "key points spread over the whole lesson"
    assert is_grounded("Fact number 399 explains a distinct idea about generics", context_passages(ctx)), "late facts ground"
