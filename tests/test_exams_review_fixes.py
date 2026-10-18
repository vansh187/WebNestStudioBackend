"""Regression tests for the /code-review findings: catalog upsert conflicts,
shared lesson keys, bulk progress, the save grace and route-scoped error shapes."""

import pytest

from tests.exam_harness import Harness, require_db, reset_process_state, run

ADMIN = {"X-Test-User": "boss"}


@pytest.fixture
def h():
    require_db()
    reset_process_state()
    harness = Harness()
    run(harness.reset())
    run(harness.add_user("boss", role="admin"))
    yield harness
    run(harness.engine.dispose())


def course_body(modules, slug="java-core", exam_code=None):
    body = {"slug": slug, "title": slug, "status": "published", "modules": modules}
    if exam_code:
        body["exam_code"] = exam_code
    return body


def test_upsert_conflicts_are_409_and_key_swaps_work(h):
    with h.client() as client:
        first = client.put("/api/admin/learning/courses", json=course_body([
            {"key": "m", "title": "M", "lessons": [
                {"key": "k1", "title": "One", "status": "published"},
                {"key": "k2", "title": "Two", "status": "published"},
            ]},
        ], exam_code="JAVA"), headers=ADMIN)  # fmt: skip
        assert first.status_code == 200, first.text
        ids = {l["key"]: l["id"] for l in first.json()["modules"][0]["lessons"]}

        # Two lessons swap keys in one payload (matched by id): no unique-index clash.
        swapped = client.put("/api/admin/learning/courses", json=course_body([
            {"key": "m", "title": "M", "lessons": [
                {"id": ids["k1"], "key": "k2", "title": "One", "status": "published"},
                {"id": ids["k2"], "key": "k1", "title": "Two", "status": "published"},
            ]},
        ]), headers=ADMIN)  # fmt: skip
        assert swapped.status_code == 200, swapped.text
        after = {l["id"]: l["key"] for l in swapped.json()["modules"][0]["lessons"]}
        assert after == {ids["k1"]: "k2", ids["k2"]: "k1"}

        # Another course taking the same exam code: 409 with the message, not a 500.
        clash = client.put("/api/admin/learning/courses", json=course_body([], slug="python", exam_code="JAVA"), headers=ADMIN)
        assert clash.status_code == 409, clash.text
        assert "exam code" in clash.json()["detail"]
        # The session recovered: the next request works.
        assert client.put("/api/admin/learning/courses", json=course_body([], slug="python", exam_code="PY"), headers=ADMIN).status_code == 200


def test_shared_key_counts_time_once_and_the_course_hint_picks_the_lesson(h):
    java = run(h.add_course("java-core", "JAVA", [2], enabled=False))
    adv = run(h.add_course("advanced-java", "ADVJAVA", [2], enabled=False))
    key = java["modules"][0]["lessons"][0]["key"]
    adv_lesson = adv["modules"][0]["lessons"][0]
    run(h.execute("update lessons set key = :k where id = :i", k=key, i=adv_lesson["id"]))
    run(h.add_user("learner"))
    me = {"X-Test-User": "learner"}
    with h.client() as client:
        response = client.put(
            f"/api/learning/lessons/{key}/progress?course_slug=advanced-java",
            json={"status": "completed", "completed_percent": 100, "time_spent_seconds": 300},
            headers=me,
        )
        assert response.status_code == 200, response.text
        assert response.json()["lesson_id"] == str(adv_lesson["id"]) and response.json()["course_slug"] == "advanced-java"
        assert run(h.scalar("select sum(time_spent_seconds) from user_lesson_progress")) == 300, "time counted once"
        assert run(h.scalar("select count(*) from user_lesson_progress where status = 'completed'")) == 2, "completed in both courses"
        assert run(h.scalar("select current_streak from codelab_user_stats")) == 1

        detail = client.get(f"/api/learning/lessons/{key}?course_slug=advanced-java", headers=me).json()
        assert detail["course_slug"] == "advanced-java" and detail["id"] == str(adv_lesson["id"])

        # With a hint the note lands on that course's lesson only; without one, on every alias.
        client.put(f"/api/learning/lessons/{key}/note?course_slug=advanced-java", json={"note": "adv only"}, headers=me)
        assert run(h.scalar("select count(*) from user_lesson_notes")) == 1
        client.put(f"/api/learning/lessons/{key}/bookmark", json={"bookmarked": True}, headers=me)
        assert run(h.scalar("select count(*) from user_lesson_bookmarks where bookmarked")) == 2


def test_bulk_not_started_is_stored_as_zero_and_completion_touches_the_streak(h):
    java = run(h.add_course("java-core", "JAVA", [2], enabled=False))
    first, second = java["modules"][0]["lessons"]
    run(h.add_user("bulk"))
    me = {"X-Test-User": "bulk"}
    with h.client() as client:
        client.post("/api/learning/progress/bulk", json={"lessons": [
            {"lesson_id": first["key"], "status": "not_started", "completed_percent": 100},
        ]}, headers=me)  # fmt: skip
        assert run(h.scalar("select completed_percent from user_lesson_progress where lesson_id = :l", l=first["id"])) == 0
        assert run(h.scalar("select count(*) from codelab_user_stats")) == 0
        client.post("/api/learning/progress/bulk", json={"lessons": [{"lesson_id": second["key"]}]}, headers=me)
        assert run(h.scalar("select current_streak from codelab_user_stats")) == 1


def test_exam_error_shape_is_scoped_to_exam_routes_not_paths(h):
    run(h.add_user("v"))
    with h.client() as client:
        # A non-exam learning route under a slug that starts with "exam" keeps the normal shape.
        normal = client.put("/api/learning/lessons/x/progress", json={"status": "nope"}, headers={"X-Test-User": "v"})
        assert normal.status_code == 422 and "code" not in normal.json()
        exam = client.post("/api/learning/courses/exam-prep/exam/attempts", json={}, headers={"X-Test-User": "v"})
        assert exam.status_code == 422 and exam.json()["code"] == "validation_error" and isinstance(exam.json()["detail"], str)
        bulk = client.post("/api/learning/progress/bulk", json={"lessons": "x"}, headers={"X-Test-User": "v"})
        assert bulk.json()["code"] == "validation_error" and "input" not in str(bulk.json())
