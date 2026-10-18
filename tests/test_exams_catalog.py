"""Phase 1: catalog upsert by key (with adoption of pre-key rows, so learner
progress survives the first keyed sync) and the exam view's strict optional auth."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from core.exceptions import UnauthorizedError
from exams.dependencies import get_optional_user_strict
from tests.exam_harness import Harness, require_db, reset_process_state, run


@pytest.fixture
def h():
    require_db()
    reset_process_state()
    harness = Harness()
    run(harness.reset())
    run(harness.add_user("boss", role="admin"))
    yield harness
    run(harness.engine.dispose())


ADMIN = {"X-Test-User": "boss"}


def catalog(modules, exam_code=None):
    body = {"slug": "java-core", "title": "Java - Core", "status": "published", "modules": modules}
    if exam_code:
        body["exam_code"] = exam_code
    return body


def test_first_keyed_sync_adopts_legacy_rows_and_keeps_progress(h):
    with h.client() as client:
        legacy = client.put("/api/admin/learning/courses", json=catalog([
            {"title": "Java Basics", "lessons": [{"title": "Variables", "status": "published"}, {"title": "Loops", "status": "published"}]},
        ]), headers=ADMIN)  # fmt: skip
        assert legacy.status_code == 200, legacy.text
        old_lesson = legacy.json()["modules"][0]["lessons"][0]
        run(h.add_user("learner"))
        run(h.execute(
            "insert into user_lesson_progress (user_id, lesson_id, status, completed_percent) values (:u, :l, 'completed', 100)",
            u=h.users["learner"].id, l=old_lesson["id"],
        ))  # fmt: skip

        keyed = client.put("/api/admin/learning/courses", json=catalog([
            {"key": "mod_basics", "title": "Java Basics", "lessons": [
                {"key": "lesson_variables", "title": "Variables", "status": "published"},
                {"key": "lesson_loops", "title": "Loops", "status": "published"},
            ]},
        ], exam_code="java"), headers=ADMIN)  # fmt: skip
        assert keyed.status_code == 200, keyed.text
        body = keyed.json()
        assert body["exam_code"] == "JAVA"
        lessons = body["modules"][0]["lessons"]
        assert lessons[0]["id"] == old_lesson["id"] and lessons[0]["key"] == "lesson_variables"
        assert run(h.scalar("select count(*) from lessons")) == 2
        progress = client.put("/api/learning/lessons/lesson_variables/progress",
                              json={"status": "in_progress", "completed_percent": 10}, headers={"X-Test-User": "learner"})  # fmt: skip
        assert progress.json()["status"] == "completed", "existing progress survived the keyed sync"


def test_keys_match_rows_lessons_move_and_dropped_keys_are_archived(h):
    with h.client() as client:
        client.put("/api/admin/learning/courses", json=catalog([
            {"key": "mod_a", "title": "A", "lessons": [
                {"key": "l1", "title": "One", "status": "published"},
                {"key": "l2", "title": "Two", "status": "published"},
            ]},
            {"key": "mod_b", "title": "B", "lessons": [{"key": "l3", "title": "Three", "status": "published"}]},
        ]), headers=ADMIN)  # fmt: skip
        ids = dict(run(h.rows("select key, id from lessons")))

        second = client.put("/api/admin/learning/courses", json=catalog([
            {"key": "mod_a", "title": "A renamed", "lessons": [{"key": "l1", "title": "One v2", "status": "published"}]},
            {"key": "mod_c", "title": "C", "lessons": [{"key": "l2", "title": "Two", "status": "published"}]},
        ]), headers=ADMIN)  # fmt: skip
        assert second.status_code == 200, second.text
        after = {row[0]: row[1:] for row in run(h.rows(
            "select l.key, l.id, l.status, m.key, l.title from lessons l join course_modules m on m.id = l.module_id"))}  # fmt: skip
        assert after["l1"][0] == ids["l1"] and after["l1"][3] == "One v2"
        assert after["l2"][0] == ids["l2"] and after["l2"][2] == "mod_c", "lesson moved modules, same row"
        assert after["l3"][1] == "archived" and after["l3"][0] == ids["l3"]
        assert run(h.scalar("select status from course_modules where key = 'mod_b'")) == "archived"
        detail = client.get("/api/learning/courses/java-core").json()
        assert [m["title"] for m in detail["modules"]] == ["A renamed", "C"]

        dup = client.put("/api/admin/learning/courses", json=catalog([
            {"key": "m", "title": "M", "lessons": [{"key": "x", "title": "X"}, {"key": "x", "title": "Y"}]},
        ]), headers=ADMIN)  # fmt: skip
        assert dup.status_code == 422
        bad_code = client.put("/api/admin/learning/courses", json=catalog([], exam_code="no spaces"), headers=ADMIN)
        assert bad_code.status_code == 422


def test_a_lesson_key_in_two_courses_counts_in_both(h):
    java = run(h.add_course("java-core", "JAVA", [1], enabled=False))
    adv = run(h.add_course("advanced-java", "ADVJAVA", [1], enabled=False))
    shared_key = java["modules"][0]["lessons"][0]["key"]
    run(h.execute("update lessons set key = :k where id = :i", k=shared_key, i=adv["modules"][0]["lessons"][0]["id"]))
    run(h.add_user("alias"))
    with h.client() as client:
        response = client.post("/api/learning/progress/bulk", json={"lessons": [{"lesson_id": shared_key}]}, headers={"X-Test-User": "alias"})
        assert response.json() == {"accepted": 1, "unknown": []}
    assert run(h.scalar("select count(*) from user_lesson_progress where status = 'completed'")) == 2


class _Auth:
    def __init__(self, result):
        self.result = result

    async def get_current_user(self, token):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_strict_optional_auth():
    user = SimpleNamespace(id=uuid.uuid4())
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="tok")
    assert asyncio.run(get_optional_user_strict(None, _Auth(user))) is None
    assert asyncio.run(get_optional_user_strict(creds, _Auth(user))) is user
    for error in (UnauthorizedError("expired"), ValueError("bad")):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(get_optional_user_strict(creds, _Auth(error)))
        assert raised.value.status_code == 401
