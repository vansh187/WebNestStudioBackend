"""End-to-end exam flows over HTTP against a real Postgres database: the
contract's learner, certificate, crash-recovery and admin rules, plus the
concurrency guarantees (one active attempt, one certificate, SKIP LOCKED).

Needs EXAMS_TEST_DATABASE_URL (a throwaway database); skipped otherwise."""

import asyncio
import re
import uuid
from datetime import datetime, timedelta

import pytest

from exams import dependencies as exam_dependencies
from exams.dependencies import ExamServices
from tests.exam_harness import (
    FakeClock,
    Harness,
    assert_no_answer_fields,
    correct_answers,
    require_db,
    reset_process_state,
    run,
)

CERT_ID = re.compile(r"^WNS-JAVA-\d{4}-[0-9A-HJKMNP-TV-Z]{8}$")


@pytest.fixture
def world(monkeypatch):
    require_db()
    reset_process_state()
    clock = FakeClock()
    monkeypatch.setattr(exam_dependencies, "clock", clock)
    h = Harness()
    run(h.reset())
    java = run(h.add_course("java-core", "JAVA", [4, 3, 2], [40, 30, 20]))
    python = run(h.add_course("python", "PY", [3, 3], [45, 45]))
    yield h, clock, java, python
    reset_process_state()
    run(h.engine.dispose())


def user(h, name):
    run(h.add_user(name))
    return {"X-Test-User": name}


def ready_learner(h, course, name):
    headers = user(h, name)
    run(h.complete_all_lessons(h.users[name], course))
    return headers


def start(client, headers, slug="java-core", name="Asha Verma"):
    return client.post(f"/api/learning/courses/{slug}/exam/attempts", json={"recipient_name": name}, headers=headers)


# --------------------------------------------------------------------------- #
# Exam view and eligibility
# --------------------------------------------------------------------------- #
def test_exam_view_anonymous_disabled_unknown_and_bad_token(world):
    h, _, _, _ = world
    with h.client() as client:
        anon = client.get("/api/learning/courses/java-core/exam")
        assert anon.status_code == 200
        body = anon.json()
        assert body["enabled"] is True and body["eligible"] is False and body["reasons"] == ["login_required"]
        assert body["question_count"] == 30 and body["duration_minutes"] == 60 and body["pass_percent"] == 70
        assert set(body) == {
            "enabled", "eligible", "reasons", "lessons_remaining", "cooldown_until", "active_attempt_id",
            "passed", "certificate_id", "question_count", "duration_minutes", "pass_percent",
        }  # fmt: skip
        assert anon.headers["cache-control"] == "no-store"
        assert client.get("/api/learning/courses/nope/exam").json()["code"] == "course_not_found"
        assert client.get("/api/learning/courses/java-core/exam", headers={"X-Test-User": "bad-token"}).status_code == 401
        run(h.execute("update courses set exam_enabled = false where slug = 'python'"))
        disabled = client.get("/api/learning/courses/python/exam").json()
        assert disabled["enabled"] is False and disabled["reasons"] == ["exam_disabled"]


def test_exam_stays_hidden_until_every_module_has_its_share(world):
    h, _, java, _ = world
    # Readiness uses EXAM_READY_TARGET (45): module 2 (2 of 9 lessons) needs 10
    # of its 20 live questions, so leave it 9.
    run(h.execute(
        "update exam_questions set status = 'quarantined' where id in "
        "(select id from exam_questions where module_id = :m order by prompt limit 11)",
        m=java["modules"][2]["id"],
    ))  # fmt: skip
    run(h.execute("update courses set exam_ready_at = null"))
    with h.client() as client:
        assert client.get("/api/learning/courses/java-core/exam").json()["enabled"] is False
        run(h.execute("update exam_questions set status = 'live'"))
        assert client.get("/api/learning/courses/java-core/exam").json()["enabled"] is True
        # Once launched, one quarantined question doesn't hide it again.
        run(h.execute("update exam_questions set status = 'quarantined' where module_id = :m and prompt like '%q0 %'", m=java["modules"][2]["id"]))
        assert client.get("/api/learning/courses/java-core/exam").json()["enabled"] is True


def test_lessons_incomplete_then_bulk_progress_by_key_makes_learner_eligible(world):
    h, _, java, _ = world
    headers = user(h, "asha")
    with h.client() as client:
        view = client.get("/api/learning/courses/java-core/exam", headers=headers).json()
        assert view["reasons"] == ["lessons_incomplete"] and view["lessons_remaining"] == 9
        refused = start(client, headers)
        assert refused.status_code == 409
        assert refused.json()["code"] == "not_eligible" and refused.json()["reason"] == "lessons_incomplete"
        assert refused.json()["lessons_remaining"] == 9

        keys = [l["key"] for m in java["modules"] for l in m["lessons"]]
        bulk = client.post(
            "/api/learning/progress/bulk",
            json={"lessons": [{"lesson_id": k, "status": "completed", "completed_percent": 100} for k in keys]
                  + [{"lesson_id": "lesson_unknown"}, {"lesson_id": keys[0], "status": "in_progress", "completed_percent": 10}]},
            headers=headers,
        )  # fmt: skip
        assert bulk.status_code == 200, bulk.text
        assert bulk.json() == {"accepted": 10, "unknown": ["lesson_unknown"]}
        # never downgraded by the later in_progress entry for the same lesson
        assert run(h.scalar("select status from user_lesson_progress where lesson_id = :l", l=java["modules"][0]["lessons"][0]["id"])) == "completed"
        view = client.get("/api/learning/courses/java-core/exam", headers=headers).json()
        assert view["eligible"] is True and view["reasons"] == [] and view["lessons_remaining"] == 0

        too_many = client.post("/api/learning/progress/bulk", json={"lessons": [{"lesson_id": "x"}] * 501}, headers=headers)
        assert too_many.status_code == 422 and isinstance(too_many.json()["detail"], str)


def test_lesson_routes_accept_keys_and_uuids(world):
    h, _, java, _ = world
    headers = user(h, "keyuser")
    lesson = java["modules"][0]["lessons"][1]
    with h.client() as client:
        by_key = client.put(f"/api/learning/lessons/{lesson['key']}/progress", json={"status": "completed", "completed_percent": 100}, headers=headers)
        assert by_key.status_code == 200, by_key.text
        assert by_key.json()["lesson_id"] == str(lesson["id"])
        assert client.get(f"/api/learning/lessons/{lesson['id']}").status_code == 200
        assert client.get(f"/api/learning/lessons/{lesson['key']}").json()["key"] == lesson["key"]
        assert client.get("/api/learning/lessons/lesson_missing").status_code == 404


# --------------------------------------------------------------------------- #
# The main learner flow
# --------------------------------------------------------------------------- #
def test_start_resume_save_restart_submit_and_certificate(world):
    h, clock, _, _ = world
    headers = ready_learner(h, world[2], "asha")
    with h.client() as client:
        first = start(client, headers)
        assert first.status_code == 201, first.text
        body = first.json()
        assert first.headers["cache-control"] == "no-store"
        assert_no_answer_fields(body)
        assert body["status"] == "active" and len(body["questions"]) == 30 and body["answers"] == []
        assert len({q["id"] for q in body["questions"]}) == 30
        for q in body["questions"]:
            assert set(q) == {"id", "prompt", "options", "module_title"} and len(q["options"]) == 4
        assert body["started_at"].endswith(("Z", "+00:00")) and body["server_now"].endswith(("Z", "+00:00"))
        started = datetime.fromisoformat(body["started_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
        assert expires - started == timedelta(minutes=60)
        attempt_id = body["attempt_id"]

        again = start(client, headers)
        assert again.status_code == 200 and again.json()["attempt_id"] == attempt_id
        assert again.json()["questions"] == body["questions"]

        payload = run(h.cache_payload(attempt_id))
        answers = correct_answers(payload, 24)
        saved = client.put(f"/api/learning/exam-attempts/{attempt_id}/answers", json={"answers": answers, "tab_switches": 2}, headers=headers)
        assert saved.status_code == 200 and "saved_at" in saved.json()

        # Simulated restart / another worker: the in-process cache is empty.
        reset_process_state()
        resumed = client.get(f"/api/learning/exam-attempts/{attempt_id}", headers=headers).json()
        assert resumed["questions"] == body["questions"]
        assert {a["question_id"] for a in resumed["answers"]} == {a["question_id"] for a in answers}
        assert_no_answer_fields(resumed)

        ping = client.get(f"/api/learning/exam-attempts/{attempt_id}/ping", headers=headers).json()
        assert ping["status"] == "active" and set(ping) == {"status", "expires_at", "server_now"}

        other = user(h, "intruder")
        assert client.get(f"/api/learning/exam-attempts/{attempt_id}", headers=other).json()["code"] == "attempt_not_found"
        assert client.post(f"/api/learning/exam-attempts/{attempt_id}/submit", headers=other).status_code == 404

        clock.advance(minutes=30)
        result = client.post(f"/api/learning/exam-attempts/{attempt_id}/submit", headers=headers)
        assert result.status_code == 200, result.text
        r = result.json()
        assert_no_answer_fields(r)
        assert (r["score"], r["total"], r["percent"], r["passed"], r["retake_after"]) == (24, 30, 80, True, None)
        assert CERT_ID.match(r["certificate_id"])
        assert sum(m["total"] for m in r["module_breakdown"]) == 30
        assert run(h.cache_payload(attempt_id)) is None, "cache must be deleted at grading"
        assert run(h.scalar("select answers::text from exam_attempts where id = :a", a=attempt_id)) == "{}"

        assert client.post(f"/api/learning/exam-attempts/{attempt_id}/submit", headers=headers).json() == r
        graded = client.get(f"/api/learning/exam-attempts/{attempt_id}", headers=headers).json()
        assert graded == {"attempt_id": attempt_id, "status": "submitted", "result": r}
        late_save = client.put(f"/api/learning/exam-attempts/{attempt_id}/answers", json={"answers": []}, headers=headers)
        assert late_save.status_code == 409 and late_save.json()["code"] == "attempt_closed"

        view = client.get("/api/learning/courses/java-core/exam", headers=headers).json()
        assert view["passed"] is True and view["certificate_id"] == r["certificate_id"] and "already_passed" in view["reasons"]
        refused = start(client, headers)
        assert refused.status_code == 409 and refused.json()["reason"] == "already_passed"
        assert run(h.scalar("select count(*) from certificates")) == 1

        mine = client.get("/api/me/certificates", headers=headers).json()["items"]
        assert [c["certificate_id"] for c in mine] == [r["certificate_id"]] and mine[0]["course_slug"] == "java-core"


def test_public_certificate_pdf_revoke_and_reissue(world):
    h, _, java, _ = world
    headers = ready_learner(h, java, "asha")
    admin = user(h, "boss")
    h.users["boss"].role = "admin"
    with h.client() as client:
        attempt = start(client, headers, name="  Asha   Verma ").json()
        answers = correct_answers(run(h.cache_payload(attempt["attempt_id"])))
        client.put(f"/api/learning/exam-attempts/{attempt['attempt_id']}/answers", json={"answers": answers}, headers=headers)
        cert_id = client.post(f"/api/learning/exam-attempts/{attempt['attempt_id']}/submit", headers=headers).json()["certificate_id"]

        public = client.get(f"/api/certificates/{cert_id.lower()}")
        assert public.status_code == 200
        assert public.json() == {
            "certificate_id": cert_id, "recipient_name": "Asha Verma", "course_title": "Java Core",
            "course_slug": "java-core", "issued_at": public.json()["issued_at"], "status": "valid",
        }  # fmt: skip
        assert client.get("/api/certificates/WNS-JAVA-2026-00000000").json()["code"] == "certificate_not_found"
        assert client.get("/api/certificates/not-an-id").status_code == 404

        pdf = client.get(f"/api/certificates/{cert_id}/pdf")
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.headers["content-disposition"] == f'attachment; filename="{cert_id}.pdf"'

        assert client.get("/api/admin/certificates?q=asha", headers=headers).status_code == 403
        found = client.get("/api/admin/certificates?q=asha@exam", headers=admin).json()["items"]
        assert found == [] or found[0]["certificate_id"] == cert_id
        found = client.get("/api/admin/certificates?q=Verma", headers=admin).json()["items"]
        assert found[0]["user_email"] == "asha@example.com" and found[0]["score"] == 30

        reissued = client.post(f"/api/admin/certificates/{cert_id}/reissue", json={"recipient_name": "Asha K. Verma"}, headers=admin)
        assert reissued.status_code == 200, reissued.text
        new_id = reissued.json()["certificate_id"]
        assert new_id != cert_id and reissued.json()["recipient_name"] == "Asha K. Verma"
        old = client.get(f"/api/certificates/{cert_id}").json()
        assert old["status"] == "revoked"
        assert run(h.scalar("select replaced_by from certificates where certificate_id = :c", c=cert_id)) == new_id
        assert client.get(f"/api/certificates/{cert_id}/pdf").json()["code"] == "certificate_revoked"
        assert client.get(f"/api/certificates/{cert_id}/pdf").status_code == 410
        again = client.post(f"/api/admin/certificates/{cert_id}/reissue", json={"recipient_name": "X Y"}, headers=admin)
        assert again.status_code == 409

        revoked = client.post(f"/api/admin/certificates/{new_id}/revoke", json={"reason": "Plagiarism"}, headers=admin)
        assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
        assert client.get(f"/api/certificates/{new_id}").json()["status"] == "revoked"
        view = client.get("/api/learning/courses/java-core/exam", headers=headers).json()
        assert view["passed"] is False and view["certificate_id"] is None
        assert [c["status"] for c in client.get("/api/me/certificates", headers=headers).json()["items"]] == ["revoked", "revoked"]
        bad_name = client.post(f"/api/admin/certificates/{new_id}/reissue", json={"recipient_name": "<script>"}, headers=admin)
        assert bad_name.status_code == 422


def test_fail_cooldown_and_a_new_paper_after_24_hours(world):
    h, clock, java, _ = world
    headers = ready_learner(h, java, "ravi")
    with h.client() as client:
        first = start(client, headers).json()
        result = client.post(f"/api/learning/exam-attempts/{first['attempt_id']}/submit", headers=headers).json()
        assert result["passed"] is False and result["certificate_id"] is None and result["score"] == 0
        assert result["retake_after"] is not None

        refused = start(client, headers)
        assert refused.status_code == 409
        assert refused.json()["reason"] == "cooldown" and refused.json()["cooldown_until"] == result["retake_after"]
        view = client.get("/api/learning/courses/java-core/exam", headers=headers).json()
        assert view["reasons"] == ["cooldown"] and view["cooldown_until"] == result["retake_after"]

        clock.advance(hours=24, minutes=1)
        second = start(client, headers)
        assert second.status_code == 201
        first_ids = {q["id"] for q in first["questions"]}
        assert not first_ids & {q["id"] for q in second.json()["questions"]}, "a retake must be a new paper"


def test_validation_and_unknown_question_ids_are_422_not_409(world):
    h, _, java, _ = world
    headers = ready_learner(h, java, "val")
    with h.client() as client:
        assert start(client, headers, name="A").status_code == 422
        assert start(client, headers, name="Bobby Tables'); --").json()["code"] == "validation_error"
        attempt = start(client, headers).json()
        url = f"/api/learning/exam-attempts/{attempt['attempt_id']}/answers"
        qid = attempt["questions"][0]["id"]
        unknown = client.put(url, json={"answers": [{"question_id": str(uuid.uuid4()), "option_index": 0}]}, headers=headers)
        assert unknown.status_code == 422 and isinstance(unknown.json()["detail"], str)
        assert client.put(url, json={"answers": [{"question_id": qid, "option_index": 4}]}, headers=headers).status_code == 422
        dup = client.put(url, json={"answers": [{"question_id": qid, "option_index": 1}] * 2}, headers=headers)
        assert dup.status_code == 422
        assert client.put(url, json={"answers": "nope"}, headers=headers).json()["errors"][0]["msg"]
        assert client.get("/api/learning/exam-attempts/not-a-uuid", headers=headers).status_code == 422
        assert client.get(f"/api/learning/exam-attempts/{uuid.uuid4()}", headers=headers).status_code == 404


# --------------------------------------------------------------------------- #
# Timer, expiry and grace
# --------------------------------------------------------------------------- #
def test_save_closes_at_expiry_and_lazy_expiry_grades_on_read(world):
    h, clock, java, _ = world
    headers = ready_learner(h, java, "late")
    with h.client() as client:
        attempt = start(client, headers).json()
        aid = attempt["attempt_id"]
        answers = correct_answers(run(h.cache_payload(aid)), 22)
        client.put(f"/api/learning/exam-attempts/{aid}/answers", json={"answers": answers}, headers=headers)
        clock.advance(minutes=60, seconds=2)
        # A last autosave arriving just after the timer is kept (5 s latency allowance)...
        late = client.put(f"/api/learning/exam-attempts/{aid}/answers", json={"answers": answers}, headers=headers)
        assert late.status_code == 200
        clock.advance(seconds=4)
        # ...but answering can't go on after that.
        closed = client.put(f"/api/learning/exam-attempts/{aid}/answers", json={"answers": []}, headers=headers)
        assert closed.status_code == 409 and closed.json()["code"] == "attempt_closed"
        clock.advance(seconds=25)
        graded = client.get(f"/api/learning/exam-attempts/{aid}", headers=headers).json()
        assert graded["status"] == "expired" and graded["result"]["score"] == 22 and graded["result"]["passed"] is True
        assert CERT_ID.match(graded["result"]["certificate_id"]), "a passing expired attempt still gets a certificate"
        assert run(h.cache_payload(aid)) is None
        assert client.post(f"/api/learning/exam-attempts/{aid}/submit", headers=headers).json() == graded["result"]


def test_submit_inside_the_grace_window_counts_as_submitted(world):
    h, clock, java, _ = world
    headers = ready_learner(h, java, "grace")
    with h.client() as client:
        aid = start(client, headers).json()["attempt_id"]
        clock.advance(minutes=60, seconds=20)
        result = client.post(f"/api/learning/exam-attempts/{aid}/submit", headers=headers)
        assert result.status_code == 200
        assert run(h.scalar("select status from exam_attempts where id = :a", a=aid)) == "submitted"


def test_exam_view_grades_an_overdue_attempt_before_answering(world):
    h, clock, java, _ = world
    headers = ready_learner(h, java, "viewer")
    with h.client() as client:
        aid = start(client, headers).json()["attempt_id"]
        assert client.get("/api/learning/courses/java-core/exam", headers=headers).json()["active_attempt_id"] == aid
        clock.advance(hours=2)
        view = client.get("/api/learning/courses/java-core/exam", headers=headers).json()
        assert view["active_attempt_id"] is None and view["reasons"] == ["cooldown"]
        assert run(h.scalar("select status from exam_attempts where id = :a", a=aid)) == "expired"


def test_expiry_job_grades_overdue_attempts_and_sweeps_orphans(world):
    h, clock, java, _ = world
    headers = ready_learner(h, java, "jobber")
    with h.client() as client:
        aid = start(client, headers).json()["attempt_id"]
    clock.advance(hours=3)

    async def job():
        async with h.sessions() as session:
            return await ExamServices(session, h.settings).maintenance_service().expire_overdue()

    summary = run(job())
    assert summary["graded"] == 1
    assert run(h.scalar("select status from exam_attempts where id = :a", a=aid)) == "expired"
    assert run(h.scalar("select count(*) from exam_attempt_cache")) == 0
    assert run(job())["graded"] == 0


# --------------------------------------------------------------------------- #
# Crash recovery
# --------------------------------------------------------------------------- #
def test_lost_attempt_is_not_a_fail_and_restart_gets_new_questions(world):
    h, clock, java, _ = world
    headers = ready_learner(h, java, "unlucky")
    with h.client() as client:
        first = start(client, headers).json()
        aid = first["attempt_id"]
        run(h.execute("delete from exam_attempt_cache where attempt_id = :a", a=aid))
        reset_process_state()
        for response in (
            client.get(f"/api/learning/exam-attempts/{aid}/ping", headers=headers),
            client.get(f"/api/learning/exam-attempts/{aid}", headers=headers),
            client.put(f"/api/learning/exam-attempts/{aid}/answers", json={"answers": []}, headers=headers),
            client.post(f"/api/learning/exam-attempts/{aid}/submit", headers=headers),
        ):
            assert response.status_code == 409 and response.json()["code"] == "attempt_lost", response.text
        view = client.get("/api/learning/courses/java-core/exam", headers=headers).json()
        assert view["eligible"] is True and view["active_attempt_id"] is None and view["cooldown_until"] is None
        clock.advance(minutes=5)
        second = start(client, headers)
        assert second.status_code == 201
        assert second.json()["attempt_id"] != aid
        assert not {q["id"] for q in first["questions"]} & {q["id"] for q in second.json()["questions"]}
        expires = datetime.fromisoformat(second.json()["expires_at"].replace("Z", "+00:00"))
        assert expires == clock.now() + timedelta(minutes=60)


def test_start_marks_a_lost_active_attempt_and_creates_a_new_one(world):
    h, _, java, _ = world
    headers = ready_learner(h, java, "restart")
    with h.client() as client:
        aid = start(client, headers).json()["attempt_id"]
        run(h.execute("delete from exam_attempt_cache"))
        reset_process_state()
        again = start(client, headers)
        assert again.status_code == 201 and again.json()["attempt_id"] != aid
        assert run(h.scalar("select status from exam_attempts where id = :a", a=aid)) == "lost"


# --------------------------------------------------------------------------- #
# Reports, quarantine and void
# --------------------------------------------------------------------------- #
def test_two_reports_quarantine_a_question_which_is_then_never_drawn(world):
    h, _, java, _ = world
    a = ready_learner(h, java, "rep1")
    b = ready_learner(h, java, "rep2")
    with h.client() as client:
        first = start(client, a).json()
        target = first["questions"][0]["id"]
        url_a = f"/api/learning/exam-attempts/{first['attempt_id']}/reports"
        assert client.post(url_a, json={"question_id": target, "reason": "wrong_answer", "comment": "B is right"}, headers=a).status_code == 201
        assert client.post(url_a, json={"question_id": target, "reason": "typo"}, headers=a).status_code == 201  # same attempt: idempotent
        assert run(h.scalar("select status from exam_questions where id = :q", q=target)) == "live"
        assert client.post(url_a, json={"question_id": str(uuid.uuid4()), "reason": "typo"}, headers=a).status_code == 422

        second = start(client, b).json()
        if target not in {q["id"] for q in second["questions"]}:
            # Make sure learner b was served the question, then report it.
            run(h.execute("update exam_attempts set question_ids = array_append(question_ids, cast(:q as uuid)) where id = :a", q=target, a=second["attempt_id"]))
        url_b = f"/api/learning/exam-attempts/{second['attempt_id']}/reports"
        assert client.post(url_b, json={"question_id": target, "reason": "unclear"}, headers=b).status_code == 201
        assert run(h.scalar("select status from exam_questions where id = :q", q=target)) == "quarantined"

        # The reporter's own grade is untouched; quarantined questions are never drawn.
        c = ready_learner(h, java, "rep3")
        for _ in range(3):
            paper = start(client, c).json()
            assert target not in {q["id"] for q in paper["questions"]}
            client.post(f"/api/learning/exam-attempts/{paper['attempt_id']}/submit", headers=c)
            run(h.execute("update exam_attempts set submitted_at = submitted_at - interval '2 days' where user_id = :u", u=h.users["rep3"].id))


def test_void_regrades_new_passes_get_certificates_and_none_are_revoked(world):
    h, _, java, _ = world
    admin = user(h, "boss")
    h.users["boss"].role = "admin"
    headers = ready_learner(h, java, "border")
    with h.client() as client:
        attempt = start(client, headers).json()
        payload = run(h.cache_payload(attempt["attempt_id"]))
        answers = correct_answers(payload, 20)  # 20/30 = fail
        client.put(f"/api/learning/exam-attempts/{attempt['attempt_id']}/answers", json={"answers": answers}, headers=headers)
        result = client.post(f"/api/learning/exam-attempts/{attempt['attempt_id']}/submit", headers=headers).json()
        assert result["passed"] is False
        wrong = [entry["question_id"] for entry in payload[20:]]
        for qid in wrong[:2]:
            voided = client.post(f"/api/admin/learning/exam-questions/{qid}/void", headers=admin)
            assert voided.status_code == 200, voided.text
        assert voided.json()["regraded_attempts"] == 1 and voided.json()["new_certificates"] == 1
        regraded = client.get(f"/api/learning/exam-attempts/{attempt['attempt_id']}", headers=headers).json()["result"]
        assert (regraded["score"], regraded["total"], regraded["passed"]) == (20, 28, True)
        assert CERT_ID.match(regraded["certificate_id"])
        # Voiding a question the learner got right never takes the pass away.
        client.post(f"/api/admin/learning/exam-questions/{payload[0]['question_id']}/void", headers=admin)
        after = client.get(f"/api/learning/exam-attempts/{attempt['attempt_id']}", headers=headers).json()["result"]
        assert after["passed"] is True and after["certificate_id"] == regraded["certificate_id"]
        assert run(h.scalar("select count(*) from certificates where status = 'valid'")) == 1


# --------------------------------------------------------------------------- #
# Isolation and rate limits
# --------------------------------------------------------------------------- #
def test_exams_only_contain_their_own_course(world):
    h, _, java, python = world
    headers = ready_learner(h, java, "iso")
    run(h.complete_all_lessons(h.users["iso"], python))
    java_ids = {str(r[0]) for r in run(h.rows("select id from exam_questions where course_id = :c", c=java["id"]))}
    py_ids = {str(r[0]) for r in run(h.rows("select id from exam_questions where course_id = :c", c=python["id"]))}
    with h.client() as client:
        java_paper = start(client, headers).json()
        py_paper = start(client, headers, slug="python").json()
    assert {q["id"] for q in java_paper["questions"]} <= java_ids
    assert {q["id"] for q in py_paper["questions"]} <= py_ids


def test_database_refuses_a_question_whose_module_belongs_to_another_course(world):
    h, _, java, python = world
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        run(h.execute(
            "insert into exam_questions (course_id, module_id, lesson_id, concept, difficulty, prompt, options, correct_index, "
            "status, prompt_norm, prompt_hash) values (:c, :m, :l, 'x', 'easy', 'p', '[\"a\",\"b\",\"c\",\"d\"]', 0, 'live', 'p', 'h')",
            c=java["id"], m=python["modules"][0]["id"], l=python["modules"][0]["lessons"][0]["id"],
        ))  # fmt: skip


def test_resuming_is_not_rate_limited_but_new_attempts_are(world):
    h, _, java, _ = world
    headers = ready_learner(h, java, "spam")
    with h.client() as client:
        codes = [start(client, headers).status_code for _ in range(10)]
        assert codes == [201] + [200] * 9, "re-opening a running attempt never uses the new-attempt budget"
        for _ in range(4):  # lose the attempt each time, so every start creates a new one
            run(h.execute("delete from exam_attempt_cache"))
            reset_process_state_keep_limits()
            assert start(client, headers).status_code == 201
        run(h.execute("delete from exam_attempt_cache"))
        reset_process_state_keep_limits()
        limited = start(client, headers)
        assert limited.status_code == 429 and limited.json()["code"] == "rate_limited"
        assert int(limited.headers["retry-after"]) > 0 and limited.json()["retry_after"] > 0


def reset_process_state_keep_limits():
    exam_dependencies.memory_cache._entries.clear()


# --------------------------------------------------------------------------- #
# Concurrency (real row locks and partial unique indexes)
# --------------------------------------------------------------------------- #
def test_concurrent_starts_create_one_attempt_and_concurrent_submits_one_certificate(world):
    h, _, java, _ = world
    run(h.add_user("racer"))
    run(h.complete_all_lessons(h.users["racer"], java))
    user_id = h.users["racer"].id

    async def start_once():
        async with h.sessions() as session:
            return await ExamServices(session, h.settings).attempt_service().start("java-core", user_id, "Race Car")

    async def submit_once(attempt_id):
        async with h.sessions() as session:
            return await ExamServices(session, h.settings).attempt_service().submit(attempt_id, user_id)

    async def scenario():
        results = await asyncio.gather(*(start_once() for _ in range(4)))
        ids = {view.attempt_id for view, _ in results}
        assert len(ids) == 1, ids
        assert sum(1 for _, created in results if created) == 1
        attempt_id = ids.pop()
        payload = await h.cache_payload(attempt_id)
        answers = {e["question_id"]: e["permutation"].index(e["correct_index"]) for e in payload}
        await h.execute("update exam_attempts set answers = cast(:a as jsonb) where id = :i", a=__import__("json").dumps(answers), i=attempt_id)
        exam_dependencies.rate_limiter._events.clear()
        outcomes = await asyncio.gather(*(submit_once(attempt_id) for _ in range(4)))
        assert len({o.certificate_id for o in outcomes}) == 1 and outcomes[0].passed
        return attempt_id

    run(scenario())
    assert run(h.scalar("select count(*) from exam_attempts where user_id = :u", u=user_id)) == 1
    assert run(h.scalar("select count(*) from certificates where user_id = :u", u=user_id)) == 1


def test_expiry_job_and_submit_racing_grade_once(world):
    h, clock, java, _ = world
    run(h.add_user("race2"))
    run(h.complete_all_lessons(h.users["race2"], java))
    user_id = h.users["race2"].id

    async def scenario():
        async with h.sessions() as session:
            view, _ = await ExamServices(session, h.settings).attempt_service().start("java-core", user_id, "Race Two")
        clock.advance(minutes=60, seconds=40)

        async def job():
            async with h.sessions() as session:
                return await ExamServices(session, h.settings).maintenance_service().expire_overdue()

        async def submit():
            async with h.sessions() as session:
                return await ExamServices(session, h.settings).attempt_service().submit(view.attempt_id, user_id)

        await asyncio.gather(job(), submit(), job())
        return view.attempt_id

    attempt_id = run(scenario())
    assert run(h.scalar("select status from exam_attempts where id = :a", a=attempt_id)) == "expired"
    assert run(h.scalar("select count(*) from exam_attempt_cache")) == 0
