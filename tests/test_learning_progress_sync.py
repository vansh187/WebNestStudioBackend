"""GET /api/learning/progress (LESSON_PROGRESS_SYNC_BACKEND.md).

Offline: every error path and status code through the real router with a
fake service, row shaping, and the code rules for the new functions.
Integration (real Postgres via EXAMS_TEST_DATABASE_URL, a throwaway database,
never production): every rule of the contract, end to end."""

import ast
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from api.learning_router import progress_router
from core.dependencies import get_current_user, get_learning_service
from core.error_handlers import register_error_handlers
from core.exceptions import DatabaseError, NotFoundError
from services.learning_service import LearningService
from tests.exam_harness import Harness, require_db, reset_process_state, run

ROOT = Path(__file__).resolve().parent.parent
USER = SimpleNamespace(id=uuid.uuid4(), role="client")
NOW = datetime(2026, 10, 26, 9, 15, tzinfo=timezone.utc)


# ---- offline: the router with a fake service ---------------------------------------------


class FakeLearning:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def list_my_progress(self, user, course_slug=None, since=None):
        self.calls.append((user.id, course_slug, since))
        if self.error:
            raise self.error
        return self.result or {"lessons": [], "server_time": NOW, "truncated": False}


def offline_client(service, user=USER):
    app = FastAPI()
    app.include_router(progress_router)
    register_error_handlers(app)

    def current_user(x_test_user: str | None = Header(default=None)):
        if x_test_user != "ok":
            raise HTTPException(status_code=401, detail="Missing bearer token")
        return user

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_learning_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


OK = {"X-Test-User": "ok"}


def test_no_login_is_401_with_a_message():
    response = offline_client(FakeLearning()).get("/api/learning/progress")
    assert response.status_code == 401 and response.json() == {"detail": "Missing bearer token"}


def test_success_shape_and_no_store():
    response = offline_client(FakeLearning()).get("/api/learning/progress", headers=OK)
    assert response.status_code == 200
    assert response.json() == {"lessons": [], "server_time": "2026-10-26T09:15:00Z", "truncated": False}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("error,status,detail", [
    (NotFoundError("Course not found"), 404, "Course not found"),
    (DatabaseError("Could not load your progress right now. Please try again shortly."), 503,
     "Could not load your progress right now. Please try again shortly."),
    (RuntimeError("driver exploded with secrets"), 503,
     "Could not load your progress right now. Please try again shortly."),
])
def test_errors_have_a_status_code_and_a_clear_message(error, status, detail):
    response = offline_client(FakeLearning(error=error)).get("/api/learning/progress", headers=OK)
    assert response.status_code == status and response.json()["detail"] == detail
    assert "secrets" not in response.text


@pytest.mark.parametrize("query", ["since=yesterday", "since=2026-13-01T00:00:00Z", "since=", "course_slug=",
                                   "course_slug=" + "x" * 201])
def test_invalid_query_parameters_are_422_with_a_message(query):
    service = FakeLearning()
    response = offline_client(service).get(f"/api/learning/progress?{query}", headers=OK)
    assert response.status_code == 422 and isinstance(response.json()["detail"], str) and response.json()["detail"]
    assert service.calls == []   # refused before the service runs


def test_query_parameters_reach_the_service():
    service = FakeLearning()
    offline_client(service).get("/api/learning/progress?course_slug=java-core&since=2026-10-25T10:00:00Z", headers=OK)
    assert service.calls == [(USER.id, "java-core", datetime(2026, 10, 25, 10, tzinfo=timezone.utc))]


# ---- offline: the service with a fake persistence -------------------------------------------


class FakePersistence:
    def __init__(self, rows=None, course_id=None, error=None, truncated=False):
        self.rows = rows or []
        self.course_id = course_id
        self.error = error
        self.truncated = truncated
        self.calls = []

    async def published_course_id(self, slug):
        self.calls.append(("course", slug))
        return self.course_id

    async def list_user_progress(self, user_id, course_id=None, since=None):
        self.calls.append(("rows", user_id, course_id, since))
        if self.error:
            raise self.error
        return list(self.rows), self.truncated


def service_with(persistence):
    service = LearningService.__new__(LearningService)
    service._learning = persistence
    return service


def test_service_shapes_rows_and_skips_ones_that_break_the_contract():
    good = uuid.uuid4()
    rows = [
        ("lesson_a", good, "java-core", "completed", 100, NOW),
        ("lesson_b", uuid.uuid4(), "python", "in_progress", 140, NOW),              # clamped to 100
        ("lesson_c", uuid.uuid4(), "python", "in_progress", 100, NOW),              # stored value reported as-is
        ("", uuid.uuid4(), "python", "in_progress", -5, datetime(2026, 10, 26)),    # empty key -> null, naive -> UTC
        ("lesson_un", uuid.uuid4(), "python", "not_started", 0, NOW),               # an un-mark (since responses)
        ("lesson_bad", "not-a-uuid", "python", "completed", 100, NOW),              # skipped
        ("lesson_odd", uuid.uuid4(), "python", "weird", 100, NOW),                  # skipped
        ("lesson_none", uuid.uuid4(), "python", "completed", 100, None),            # skipped
        ("short_row", uuid.uuid4(), "python", "completed", 100),                    # wrong shape: skipped, not a 503
        None,                                                                       # skipped
    ]
    result = asyncio.run(service_with(FakePersistence(rows)).list_my_progress(USER))
    assert [(i.key, i.status, i.completed_percent) for i in result.lessons] == [
        ("lesson_a", "completed", 100), ("lesson_b", "in_progress", 100), ("lesson_c", "in_progress", 100),
        (None, "in_progress", 0), ("lesson_un", "not_started", 0)]
    assert result.lessons[3].updated_at.tzinfo is not None and result.server_time.tzinfo is not None
    assert result.truncated is False


def test_service_reports_truncation():
    result = asyncio.run(service_with(FakePersistence(truncated=True)).list_my_progress(USER))
    assert result.truncated is True


def test_service_course_and_since_handling():
    persistence = FakePersistence(course_id=uuid.uuid4())
    asyncio.run(service_with(persistence).list_my_progress(USER, " java-core ", datetime(2026, 10, 25, 10)))
    assert persistence.calls[0] == ("course", "java-core")
    # Naive read as UTC, then moved back by the 10-minute overlap.
    assert persistence.calls[1][3] == datetime(2026, 10, 25, 9, 50, tzinfo=timezone.utc)
    for slug in ("unknown", "   "):
        with pytest.raises(NotFoundError) as caught:
            asyncio.run(service_with(FakePersistence(course_id=None)).list_my_progress(USER, slug))
        assert caught.value.message == "Course not found"


def test_since_too_early_to_move_back_means_a_full_fetch():
    persistence = FakePersistence()
    asyncio.run(service_with(persistence).list_my_progress(USER, since=datetime.min.replace(tzinfo=timezone.utc)))
    assert persistence.calls[-1][3] is None


def test_one_failure_is_logged_once(caplog):
    import logging

    with caplog.at_level(logging.ERROR, logger="webnest.learning"):
        response = offline_client(service_with(FakePersistence(error=KeyError("boom")))).get(
            "/api/learning/progress", headers=OK)
    assert response.status_code == 503
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR and r.name == "webnest.learning"]) == 1


def test_service_maps_unexpected_failures_to_a_clear_database_error():
    with pytest.raises(DatabaseError) as passed_through:
        asyncio.run(service_with(FakePersistence(error=DatabaseError("db down"))).list_my_progress(USER))
    assert passed_through.value.message == "db down"
    with pytest.raises(DatabaseError) as mapped:
        asyncio.run(service_with(FakePersistence(error=KeyError("boom"))).list_my_progress(USER))
    assert mapped.value.message == "Could not load your progress right now. Please try again shortly."


# ---- code rules for the new code ----------------------------------------------------------------


NEW_FUNCTIONS = {
    ROOT / "database" / "learning_persistence.py": {"published_course_id", "list_user_progress", "_course_lookup"},
    ROOT / "services" / "learning_service.py": {"list_my_progress", "_progress_items", "_overlapped"},
    ROOT / "api" / "learning_router.py": {"my_progress"},
}


def test_new_functions_are_wrapped_in_try_and_no_static_methods():
    for path, names in NEW_FUNCTIONS.items():
        source = path.read_text(encoding="utf-8")
        assert "@staticmethod" not in source, path
        found = {}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    body = body[1:]
                found[node.name] = len(body) == 1 and isinstance(body[0], ast.Try)
        assert found == {name: True for name in names}, (path.name, found)


# ---- integration: real Postgres ---------------------------------------------------------------


@pytest.fixture
def h():
    from core.dependencies import get_optional_current_user

    require_db()
    reset_process_state()
    harness = Harness()
    run(harness.reset())
    run(harness.add_user("learner"))
    run(harness.add_user("other"))
    # The course outline uses the optional login; the harness only maps the required one.
    def optional_user(x_test_user: str | None = Header(default=None)):
        return harness.users.get(x_test_user) if x_test_user else None

    harness.app.dependency_overrides[get_optional_current_user] = optional_user
    yield harness
    run(harness.engine.dispose())


ME = {"X-Test-User": "learner"}
OTHER = {"X-Test-User": "other"}


def lesson(course, m=0, i=0):
    return course["modules"][m]["lessons"][i]


def progress(client, headers=ME, **params):
    response = client.get("/api/learning/progress", headers=headers, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_empty_then_completed_through_the_real_api(h):
    java = run(h.add_course("java-core", "JAVA", [2, 1]))
    with h.client() as client:
        empty = progress(client)
        assert empty["lessons"] == [] and empty["server_time"].endswith("Z")
        target = lesson(java)
        put = client.put(f"/api/learning/lessons/{target['key']}/progress", headers=ME,
                          json={"status": "completed", "completed_percent": 100})
        assert put.status_code == 200, put.text
        body = progress(client)
        assert body["lessons"] == [{"key": target["key"], "lesson_id": str(target["id"]), "course_slug": "java-core",
                                    "status": "completed", "completed_percent": 100,
                                    "updated_at": body["lessons"][0]["updated_at"]}]
        assert body["lessons"][0]["updated_at"].endswith("Z")


def test_no_login_is_401(h):
    with h.client() as client:
        response = client.get("/api/learning/progress")
        assert response.status_code == 401 and response.json()["detail"] == "Missing bearer token"


def test_in_progress_listed_and_unmarked_not(h):
    java = run(h.add_course("java-core", "JAVA", [3]))
    with h.client() as client:
        first, second = lesson(java, 0, 0), lesson(java, 0, 1)
        client.put(f"/api/learning/lessons/{first['key']}/progress", headers=ME,
                   json={"status": "in_progress", "completed_percent": 40})
        client.put(f"/api/learning/lessons/{second['key']}/progress", headers=ME,
                   json={"status": "completed", "completed_percent": 100})
        client.put(f"/api/learning/lessons/{second['key']}/progress", headers=ME,
                   json={"status": "not_started", "completed_percent": 0})   # explicit un-mark
        lessons = progress(client)["lessons"]
        assert [(l["key"], l["status"], l["completed_percent"]) for l in lessons] == [(first["key"], "in_progress", 40)]


def test_unpublished_lessons_and_courses_are_hidden(h):
    java = run(h.add_course("java-core", "JAVA", [3]))
    draft_course = run(h.add_course("draft-course", "DRAFT", [1]))
    for target in (lesson(java, 0, 0), lesson(java, 0, 1), lesson(java, 0, 2), lesson(draft_course)):
        run(h.execute("insert into user_lesson_progress (user_id, lesson_id, status, completed_percent) "
                      "values (:u, :l, 'completed', 100)", u=h.users["learner"].id, l=target["id"]))
    run(h.execute("update lessons set status = 'draft' where id = :l", l=lesson(java, 0, 1)["id"]))
    run(h.execute("update lessons set status = 'archived' where id = :l", l=lesson(java, 0, 2)["id"]))
    run(h.execute("update courses set status = 'draft' where id = :c", c=draft_course["id"]))
    with h.client() as client:
        assert [l["key"] for l in progress(client)["lessons"]] == [lesson(java)["key"]]


def test_alias_key_appears_once_per_course(h):
    java = run(h.add_course("java-core", "JAVA", [1]))
    spring = run(h.add_course("spring-boot", "SPRING", [1]))
    shared = lesson(java)["key"]
    run(h.execute("update lessons set key = :k where id = :l", k=shared, l=lesson(spring)["id"]))
    with h.client() as client:
        bulk = client.post("/api/learning/progress/bulk", json={"lessons": [{"lesson_id": shared}]}, headers=ME)
        assert bulk.status_code == 200 and bulk.json() == {"accepted": 1, "unknown": []}
        lessons = progress(client)["lessons"]
        assert sorted((l["key"], l["course_slug"], l["status"]) for l in lessons) == [
            (shared, "java-core", "completed"), (shared, "spring-boot", "completed")]


def test_course_filter_and_unknown_course(h):
    java = run(h.add_course("java-core", "JAVA", [1]))
    python = run(h.add_course("python", "PY", [1]))
    with h.client() as client:
        for course in (java, python):
            client.post("/api/learning/progress/bulk", json={"lessons": [{"lesson_id": lesson(course)["key"]}]},
                        headers=ME)
        only_python = progress(client, course_slug="python")["lessons"]
        assert [l["course_slug"] for l in only_python] == ["python"]
        missing = client.get("/api/learning/progress", headers=ME, params={"course_slug": "nope"})
        assert missing.status_code == 404 and missing.json() == {"detail": "Course not found"}
        draft = run(h.add_course("hidden", "HID", [1]))
        run(h.execute("update courses set status = 'draft' where id = :c", c=draft["id"]))
        assert client.get("/api/learning/progress", headers=ME, params={"course_slug": "hidden"}).status_code == 404


def test_since_returns_only_later_changes(h):
    java = run(h.add_course("java-core", "JAVA", [2]))
    old, new = lesson(java, 0, 0), lesson(java, 0, 1)
    for target, when in ((old, datetime(2026, 10, 1, tzinfo=timezone.utc)), (new, datetime(2026, 10, 20, tzinfo=timezone.utc))):
        run(h.execute("insert into user_lesson_progress (user_id, lesson_id, status, completed_percent, updated_at) "
                      "values (:u, :l, 'completed', 100, :t)", u=h.users["learner"].id, l=target["id"], t=when))
    with h.client() as client:
        assert [l["key"] for l in progress(client, since="2026-10-10T00:00:00Z")["lessons"]] == [new["key"]]
        assert [l["key"] for l in progress(client, since="2026-10-10T00:00:00")["lessons"]] == [new["key"]]  # naive = UTC
        assert [l["key"] for l in progress(client, since="2026-10-20T05:30:00+05:30")["lessons"]] == [new["key"]]
        assert progress(client, since="2099-01-01T00:00:00Z")["lessons"] == []
        assert len(progress(client)["lessons"]) == 2
        # Round trip: the next refresh with the previous server_time sees nothing new.
        first = progress(client)
        assert progress(client, since=first["server_time"])["lessons"] == []
        assert first["truncated"] is False
        bad = client.get("/api/learning/progress", headers=ME, params={"since": "not-a-date"})
        assert bad.status_code == 422 and isinstance(bad.json()["detail"], str)


def test_other_users_progress_is_never_returned(h):
    java = run(h.add_course("java-core", "JAVA", [2]))
    with h.client() as client:
        client.post("/api/learning/progress/bulk", json={"lessons": [{"lesson_id": lesson(java)["key"]}]}, headers=OTHER)
        assert progress(client)["lessons"] == []
        assert len(progress(client, headers=OTHER)["lessons"]) == 1


def test_lesson_without_a_key_and_null_updated_at(h):
    java = run(h.add_course("java-core", "JAVA", [2]))
    keyless, undated = lesson(java, 0, 0), lesson(java, 0, 1)
    run(h.execute("update lessons set key = null where id = :l", l=keyless["id"]))
    for target in (keyless, undated):
        run(h.execute("insert into user_lesson_progress (user_id, lesson_id, status, completed_percent) "
                      "values (:u, :l, 'completed', 100)", u=h.users["learner"].id, l=target["id"]))
    # Production's column allows NULL (it predates the default); reproduce that here.
    run(h.execute("alter table user_lesson_progress alter column updated_at drop not null"))
    run(h.execute("update user_lesson_progress set updated_at = null where lesson_id = :l", l=undated["id"]))
    with h.client() as client:
        lessons = {l["lesson_id"]: l for l in progress(client)["lessons"]}
        assert lessons[str(keyless["id"])]["key"] is None
        # An unknown change time is "long ago": in every full fetch, never re-sent by a refresh.
        assert lessons[str(undated["id"])]["updated_at"] == "1970-01-01T00:00:00Z"
        refresh = {l["lesson_id"] for l in progress(client, since="2026-01-01T00:00:00Z")["lessons"]}
        assert str(undated["id"]) not in refresh and str(keyless["id"]) in refresh


def test_bulk_round_trip_and_course_outline_carries_keys(h):
    java = run(h.add_course("java-core", "JAVA", [2, 2]))
    keys = [l["key"] for m in java["modules"] for l in m["lessons"]]
    with h.client() as client:
        bulk = client.post("/api/learning/progress/bulk", json={"lessons": [{"lesson_id": k} for k in keys]},
                           headers=ME)
        assert bulk.json() == {"accepted": 4, "unknown": []}
        assert sorted(l["key"] for l in progress(client)["lessons"]) == sorted(keys)
        outline = client.get("/api/learning/courses/java-core", headers=ME).json()
        refs = [l for m in outline["modules"] for l in m["lessons"]]
        assert sorted(r["key"] for r in refs) == sorted(keys) and {r["status"] for r in refs} == {"completed"}


# ---- marking lessons: every corner case, checked through GET /progress ---------------------------


def mark(client, ref, status="completed", percent=100, headers=ME, course_slug=None, time_spent=0):
    params = {"course_slug": course_slug} if course_slug else None
    return client.put(f"/api/learning/lessons/{ref}/progress", headers=headers, params=params,
                      json={"status": status, "completed_percent": percent, "time_spent_seconds": time_spent})


def states(client, headers=ME):
    return {(l["key"], l["course_slug"]): (l["status"], l["completed_percent"]) for l in progress(client, headers)["lessons"]}


def test_marking_by_key_or_by_uuid_is_the_same_lesson(h):
    java = run(h.add_course("java-core", "JAVA", [2]))
    first, second = lesson(java, 0, 0), lesson(java, 0, 1)
    with h.client() as client:
        assert mark(client, first["key"]).status_code == 200
        assert mark(client, str(second["id"])).status_code == 200
        assert states(client) == {(first["key"], "java-core"): ("completed", 100),
                                  (second["key"], "java-core"): ("completed", 100)}


def test_marking_complete_twice_is_idempotent_and_only_the_first_is_new(h):
    java = run(h.add_course("java-core", "JAVA", [1]))
    key = lesson(java)["key"]
    with h.client() as client:
        first, again = mark(client, key), mark(client, key)
        assert first.json()["newly_completed"] is True and again.json()["newly_completed"] is False
        assert len(progress(client)["lessons"]) == 1


def test_completion_is_sticky_when_the_lesson_is_reopened(h):
    java = run(h.add_course("java-core", "JAVA", [1]))
    key = lesson(java)["key"]
    with h.client() as client:
        mark(client, key)
        reopened = mark(client, key, status="in_progress", percent=10)
        assert reopened.status_code == 200 and reopened.json()["status"] == "completed"
        assert states(client) == {(key, "java-core"): ("completed", 100)}


def test_unmark_then_mark_again(h):
    java = run(h.add_course("java-core", "JAVA", [1]))
    key = lesson(java)["key"]
    with h.client() as client:
        mark(client, key)
        assert mark(client, key, status="not_started", percent=0).json()["status"] == "not_started"
        assert states(client) == {}
        again = mark(client, key)
        assert again.json()["newly_completed"] is True and states(client) == {(key, "java-core"): ("completed", 100)}


def test_reading_progress_only_moves_forward_and_never_reaches_100_without_completing(h):
    java = run(h.add_course("java-core", "JAVA", [1]))
    key = lesson(java)["key"]
    with h.client() as client:
        mark(client, key, status="in_progress", percent=60)
        mark(client, key, status="in_progress", percent=20)    # going back never lowers it
        assert states(client) == {(key, "java-core"): ("in_progress", 60)}
        mark(client, key, status="in_progress", percent=100)   # 100 without completing caps at 99
        assert states(client) == {(key, "java-core"): ("in_progress", 99)}


@pytest.mark.parametrize("body", [
    {"status": "done", "completed_percent": 100}, {"status": "completed", "completed_percent": 101},
    {"status": "completed", "completed_percent": -1}, {"status": "completed"}, {},
    {"status": "completed", "completed_percent": 100, "time_spent_seconds": -5},
    {"status": "completed", "completed_percent": 100, "time_spent_seconds": 86_401},
    {"status": "completed", "completed_percent": "lots"},
])
def test_invalid_mark_requests_are_refused_and_change_nothing(h, body):
    java = run(h.add_course("java-core", "JAVA", [1]))
    with h.client() as client:
        response = client.put(f"/api/learning/lessons/{lesson(java)['key']}/progress", headers=ME, json=body)
        assert response.status_code == 422 and response.json()["detail"]
        assert progress(client)["lessons"] == []


def test_marking_unknown_or_unpublished_lessons_is_404(h):
    java = run(h.add_course("java-core", "JAVA", [2]))
    run(h.execute("update lessons set status = 'draft' where id = :l", l=lesson(java, 0, 1)["id"]))
    with h.client() as client:
        for ref in ("lesson_does_not_exist", str(uuid.uuid4()), lesson(java, 0, 1)["key"]):
            response = mark(client, ref)
            assert response.status_code == 404 and response.json() == {"detail": "Lesson not found"}, ref
        assert progress(client)["lessons"] == []


def test_marking_without_login_is_401_and_saves_nothing(h):
    java = run(h.add_course("java-core", "JAVA", [1]))
    with h.client() as client:
        assert mark(client, lesson(java)["key"], headers={}).status_code == 401
        assert progress(client)["lessons"] == []


def test_marking_an_alias_completes_it_in_every_course(h):
    java = run(h.add_course("java-core", "JAVA", [1]))
    spring = run(h.add_course("spring-boot", "SPRING", [1]))
    shared = lesson(java)["key"]
    run(h.execute("update lessons set key = :k where id = :l", k=shared, l=lesson(spring)["id"]))
    with h.client() as client:
        assert mark(client, shared, course_slug="spring-boot").json()["course_slug"] == "spring-boot"
        assert states(client) == {(shared, "java-core"): ("completed", 100), (shared, "spring-boot"): ("completed", 100)}


def test_bulk_never_downgrades_and_reports_unknown_lessons(h):
    java = run(h.add_course("java-core", "JAVA", [2]))
    done, reading = lesson(java, 0, 0)["key"], lesson(java, 0, 1)["key"]
    with h.client() as client:
        mark(client, done)
        bulk = client.post("/api/learning/progress/bulk", headers=ME, json={"lessons": [
            {"lesson_id": done, "status": "in_progress", "completed_percent": 5},
            {"lesson_id": reading, "status": "in_progress", "completed_percent": 30},
            {"lesson_id": "lesson_from_an_old_outline"},
        ]})
        assert bulk.status_code == 200 and bulk.json() == {"accepted": 2, "unknown": ["lesson_from_an_old_outline"]}
        assert states(client) == {(done, "java-core"): ("completed", 100), (reading, "java-core"): ("in_progress", 30)}
        too_many = client.post("/api/learning/progress/bulk", headers=ME, json={"lessons": [{"lesson_id": done}] * 501})
        assert too_many.status_code == 422


def test_marking_in_two_tabs_at_once_keeps_one_completed_row(h):
    java = run(h.add_course("java-core", "JAVA", [1]))
    key = lesson(java)["key"]
    from concurrent.futures import ThreadPoolExecutor

    with h.client() as client:
        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = list(pool.map(lambda _: mark(client, key).status_code, range(4)))
        # The save is one atomic upsert, so simultaneous marks never collide.
        assert codes == [200, 200, 200, 200]
        assert states(client) == {(key, "java-core"): ("completed", 100)}
        assert run(h.scalar("select count(*) from user_lesson_progress where lesson_id = :l", l=lesson(java)["id"])) == 1


def test_marked_lessons_survive_a_new_session(h):
    """The reported bug: completions must come back on a fresh login (a new
    client with nothing cached), from the server alone."""
    java = run(h.add_course("java-core", "JAVA", [3]))
    keys = [l["key"] for l in java["modules"][0]["lessons"]]
    with h.client() as first_browser:
        for key in keys[:2]:
            assert mark(first_browser, key).status_code == 200
    with h.client() as second_browser:
        assert sorted(l["key"] for l in progress(second_browser)["lessons"]) == sorted(keys[:2])


def test_cap_keeps_the_newest_rows(h):
    from database.learning_persistence import LearningPersistence

    java = run(h.add_course("java-core", "JAVA", [3]))
    for index, target in enumerate(java["modules"][0]["lessons"]):
        run(h.execute("insert into user_lesson_progress (user_id, lesson_id, status, completed_percent, updated_at) "
                      "values (:u, :l, 'completed', 100, now() - make_interval(days => :d))",
                      u=h.users["learner"].id, l=target["id"], d=index))

    async def capped():
        async with h.sessions() as session:
            return await LearningPersistence(session).list_user_progress(h.users["learner"].id, limit=2)

    rows, truncated = run(capped())
    assert truncated is True and [row[0] for row in rows] == [lesson(java, 0, 0)["key"], lesson(java, 0, 1)["key"]]

    async def exact():
        async with h.sessions() as session:
            return await LearningPersistence(session).list_user_progress(h.users["learner"].id, limit=3)

    rows, truncated = run(exact())
    assert truncated is False and len(rows) == 3


def test_a_change_that_commits_late_is_still_picked_up(h):
    """A writer stamps updated_at, then commits after a reader's query. The
    reader's next refresh (since = its server_time) must still see it."""
    java = run(h.add_course("java-core", "JAVA", [1]))
    with h.client() as client:
        reader = progress(client)
        server_time = datetime.fromisoformat(reader["server_time"].replace("Z", "+00:00"))
        run(h.execute("insert into user_lesson_progress (user_id, lesson_id, status, completed_percent, updated_at) "
                      "values (:u, :l, 'completed', 100, :t)", u=h.users["learner"].id, l=lesson(java)["id"],
                      t=server_time - timedelta(minutes=5)))   # stamped before, committed after
        later = progress(client, since=reader["server_time"])["lessons"]
        assert [l["key"] for l in later] == [lesson(java)["key"]]


def test_an_unmark_reaches_a_refreshing_client(h):
    java = run(h.add_course("java-core", "JAVA", [2]))
    kept, undone = lesson(java, 0, 0)["key"], lesson(java, 0, 1)["key"]
    with h.client() as client:
        mark(client, kept)
        mark(client, undone)
        synced = progress(client)
        mark(client, undone, status="not_started", percent=0)
        refresh = {l["key"]: l["status"] for l in progress(client, since=synced["server_time"])["lessons"]}
        assert refresh[undone] == "not_started"
        assert {l["key"] for l in progress(client)["lessons"]} == {kept}   # a full fetch lists only started ones


def test_reported_percent_matches_the_lesson_page(h):
    java = run(h.add_course("java-core", "JAVA", [1]))
    target = lesson(java)
    with h.client() as client:
        client.post("/api/learning/progress/bulk", headers=ME,
                    json={"lessons": [{"lesson_id": target["key"], "status": "in_progress", "completed_percent": 100}]})
        listed = progress(client)["lessons"][0]["completed_percent"]
        lesson_page = client.get(f"/api/learning/lessons/{target['key']}", headers=ME).json()["progress"]
        assert listed == lesson_page["completed_percent"]


def test_course_lookup_rule_is_shared(h):
    from database.learning_persistence import LearningPersistence

    java = run(h.add_course("java-core", "JAVA", [1]))
    draft = run(h.add_course("draft-course", "DRAFT", [1]))
    run(h.execute("update courses set status = 'draft' where id = :c", c=draft["id"]))

    async def lookups():
        async with h.sessions() as session:
            store = LearningPersistence(session)
            return (await store.published_course_id("java-core"), await store.published_course_id(java["id"]),
                    await store.published_course_id("draft-course"), await store.published_course_id("missing"),
                    (await store.get_course("draft-course")), (await store.get_course("java-core")).id)

    by_slug, by_id, hidden, missing, draft_course, java_course = run(lookups())
    assert by_slug == by_id == java_course == java["id"]
    assert hidden is None and missing is None and draft_course is None
