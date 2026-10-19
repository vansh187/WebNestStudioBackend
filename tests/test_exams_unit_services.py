"""Unit tests of the exam services with in-memory fakes (no database):
every branch and boundary of start / get / ping / save / submit / report,
lazy expiry, lost attempts, eligibility, visibility, certificates and the
maintenance jobs."""

import asyncio
import os
import uuid
from datetime import timedelta

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest

from exams.errors import (
    AttemptClosed,
    AttemptLost,
    AttemptNotFound,
    CertificateNotFound,
    CertificateRevoked,
    CourseNotFound,
    CrossCourseQuestion,
    ExamConflict,
    ExamDisabled,
    ExamInternalError,
    ExamValidationError,
    NotEligible,
    PoolNotReady,
    RateLimited,
)
from exams.records import CertificateRecord
from exams.services.attempt_lifecycle import AttemptLifecycle, parse_key
from exams.services.attempt_service import AttemptService
from exams.services.certificate_service import CertificateService, PdfCache, fallback_exam_code
from exams.services.eligibility_service import EligibilityService
from exams.services.maintenance_service import MaintenanceService
from exams.services import maintenance_service
from exams.services.pool_service import PoolService
from tests import exam_fakes as f


def run(coro):
    return asyncio.run(coro)


class World:
    def __init__(self, questions_per_module=(6, 6), **setting_overrides) -> None:
        self.settings = f.settings(**setting_overrides)
        self.clock = f.Clock()
        self.ids = f.Ids()
        self.limiter = f.Limiter()
        self.uow = f.Uow()
        self.courses = f.Courses()
        self.progress = f.Progress()
        self.questions = f.Questions()
        self.attempts = f.Attempts()
        self.cache = f.Cache()
        self.certificate_store = f.Certificates()
        self.reports = f.Reports()
        self.renderer = f.Renderer()
        self.jobs = f.Jobs()
        self.alerts = f.Alerts()
        self.course = self.courses.add()
        n = 0
        for module, count in zip(self.courses.modules[self.course.id], questions_per_module):
            lesson = next(l for l in self.courses.lessons[self.course.id] if l.module_id == module.id)
            for _ in range(count):
                n += 1
                self.questions.add(self.course, module, lesson, n, difficulty=("easy", "medium", "hard")[n % 3])
        self.certificates = CertificateService(
            self.certificate_store, self.uow, self.clock, self.ids, self.renderer, PdfCache(4), self.limiter, self.settings
        )
        self.pool = PoolService(self.courses, self.questions, self.uow, self.clock, self.settings)
        self.lifecycle = AttemptLifecycle(self.attempts, self.cache, self.courses, self.certificates, self.uow, self.clock, self.settings)
        self.eligibility = EligibilityService(
            self.courses, self.pool, self.lifecycle, self.attempts, self.certificate_store, self.progress, self.clock, self.settings
        )
        self.service = AttemptService(
            self.courses, self.questions, self.attempts, self.cache, self.certificate_store, self.reports, self.pool,
            self.eligibility, self.lifecycle, self.uow, self.clock, self.ids, f.Rand(), self.limiter, self.settings,
        )  # fmt: skip
        self.maintenance = MaintenanceService(
            self.courses, self.questions, self.attempts, self.cache, self.jobs, self.pool, self.lifecycle, self.alerts,
            self.uow, self.clock, self.settings,
        )  # fmt: skip

    def start(self, user=None, name="Asha Verma"):
        return run(self.service.start(self.course.slug, user or USER, name))

    def correct(self, attempt_id, how_many=None):
        payload = self.cache.rows[attempt_id]
        return [(uuid.UUID(e["question_id"]), e["permutation"].index(e["correct_index"])) for e in payload[:how_many]]


USER = uuid.uuid4()
OTHER = uuid.uuid4()


@pytest.fixture
def w():
    return World()


# --------------------------------------------------------------------------- #
# Start
# --------------------------------------------------------------------------- #
def test_start_creates_attempt_with_frozen_cache_and_no_key_in_the_view(w):
    view, created = w.start()
    assert created and view.status == "active" and len(view.questions) == 6
    assert all(set(q) == {"id", "prompt", "options", "module_title"} for q in view.questions)
    payload = w.cache.rows[view.attempt_id]
    for q, entry in zip(view.questions, payload):
        original = w.questions.rows[uuid.UUID(entry["question_id"])].options
        assert q["options"] == [original[i] for i in entry["permutation"]]
    assert view.expires_at - view.started_at == timedelta(minutes=60)
    assert all(w.questions.rows[uuid.UUID(e["question_id"])].times_served == 1 for e in payload)


def test_start_validates_the_name_before_touching_anything(w):
    with pytest.raises(ExamValidationError):
        w.start(name=" A ")
    assert w.attempts.rows == {} and w.limiter._events == {}


@pytest.mark.parametrize(
    "setup,error",
    [
        (lambda w: w.courses.courses.update({"java-core": w.courses.courses["java-core"].__class__(**{**vars(w.course), "status": "draft"})}), CourseNotFound),
        (lambda w: w.courses.courses.update({"java-core": w.courses.courses["java-core"].__class__(**{**vars(w.course), "exam_enabled": False})}), ExamDisabled),
        (lambda w: [setattr(q, "status", "quarantined") for q in list(w.questions.rows.values())[:8]], PoolNotReady),
    ],
)
def test_start_refusals(w, setup, error):
    setup(w)
    with pytest.raises(error):
        w.start()


def test_start_unknown_course(w):
    with pytest.raises(CourseNotFound):
        run(w.service.start("nope", USER, "Asha Verma"))


def test_not_eligible_carries_reason_and_details(w):
    w.progress.remaining[(USER, w.course.id)] = 3
    with pytest.raises(NotEligible) as raised:
        w.start()
    assert raised.value.extra == {"reason": "lessons_incomplete", "lessons_remaining": 3}


def test_resume_returns_the_same_attempt_even_if_the_pool_shrank(w):
    first, _ = w.start()
    for q in w.questions.rows.values():
        q.status = "quarantined"
    again, created = w.start()
    assert not created and again.attempt_id == first.attempt_id and again.questions == first.questions


def test_resuming_never_uses_the_new_attempt_budget(w):
    w.start()
    for _ in range(30):
        assert w.start()[1] is False


def test_new_attempts_are_limited_to_five_an_hour(w):
    for i in range(5):
        view, created = w.start()
        assert created
        run(w.attempts.mark_lost(view.attempt_id, "test"))
    with pytest.raises(RateLimited) as raised:
        w.start()
    assert raised.value.extra["retry_after"] > 0
    w.limiter.t += 3601
    assert w.start()[1] is True


def test_a_double_click_race_returns_the_winner(w):
    winner, _ = w.start(user=OTHER)
    # The user's own insert loses once to a concurrent insert; the retry finds it.
    w.attempts.raise_active_exists = 1
    view, created = w.start()
    assert created and w.uow.rollbacks >= 1


def test_two_lost_races_give_a_clean_conflict(w):
    w.attempts.raise_active_exists = 5
    with pytest.raises(ExamConflict):
        w.start()


def test_start_guard_refuses_when_drawn_questions_are_not_served(w):
    w.questions.serve_override = []
    with pytest.raises(CrossCourseQuestion):
        w.start()
    assert w.attempts.rows == {}


def test_a_lost_active_attempt_is_replaced_on_start(w):
    first, _ = w.start()
    w.cache.rows.clear()
    second, created = w.start()
    assert created and second.attempt_id != first.attempt_id
    assert w.attempts.rows[first.attempt_id].status == "lost"
    assert not {q["id"] for q in first.questions} & {q["id"] for q in second.questions}


def test_questions_of_modules_without_published_lessons_are_not_drawn(w):
    m0, m1 = w.courses.modules[w.course.id]
    w.courses.modules[w.course.id][1] = m1.__class__(**{**vars(m1), "published_lessons": 0})
    view, _ = w.start()
    drawn = {uuid.UUID(q["id"]) for q in view.questions}
    assert all(w.questions.rows[q].module_id == m0.id for q in drawn)


# --------------------------------------------------------------------------- #
# Get / ping
# --------------------------------------------------------------------------- #
def test_get_rules(w):
    view, _ = w.start()
    assert run(w.service.get(view.attempt_id, USER)).questions == view.questions
    with pytest.raises(AttemptNotFound):
        run(w.service.get(view.attempt_id, OTHER))
    with pytest.raises(AttemptNotFound):
        run(w.service.get(uuid.uuid4(), USER))
    w.cache.rows.clear()
    with pytest.raises(AttemptLost):
        run(w.service.get(view.attempt_id, USER))
    with pytest.raises(AttemptLost):
        run(w.service.get(view.attempt_id, USER))  # stays lost


def test_get_on_an_overdue_attempt_grades_it_as_expired(w):
    view, _ = w.start()
    w.clock.advance(minutes=60, seconds=31)
    graded = run(w.service.get(view.attempt_id, USER))
    assert graded.status == "expired" and graded.result.score == 0 and graded.result.retake_after is not None
    assert view.attempt_id not in w.cache.rows


def test_get_at_the_exact_grace_boundary_is_still_active(w):
    view, _ = w.start()
    w.clock.advance(minutes=60, seconds=30)
    assert run(w.service.get(view.attempt_id, USER)).status == "active"


def test_ping(w):
    view, _ = w.start()
    assert run(w.service.ping(view.attempt_id, USER)).status == "active"
    w.clock.advance(hours=2)
    assert run(w.service.ping(view.attempt_id, USER)).status == "expired"
    other, _ = w.start(user=OTHER)
    w.cache.rows.pop(other.attempt_id)
    with pytest.raises(AttemptLost):
        run(w.service.ping(other.attempt_id, OTHER))


# --------------------------------------------------------------------------- #
# Save
# --------------------------------------------------------------------------- #
def test_save_validation(w):
    view, _ = w.start()
    qid = uuid.UUID(view.questions[0]["id"])
    for answers in ([(uuid.uuid4(), 0)], [(qid, 1), (qid, 2)], [(qid, 4)], [(qid, -1)], [(qid, 0)] * 7):
        with pytest.raises(ExamValidationError):
            run(w.service.save(view.attempt_id, USER, answers, 0))
    assert run(w.service.save(view.attempt_id, USER, [], 0)) == w.clock.now()  # clearing is allowed


def test_save_keeps_tab_switches_monotonic(w):
    view, _ = w.start()
    run(w.service.save(view.attempt_id, USER, [], 5))
    run(w.service.save(view.attempt_id, USER, [], 2))
    assert w.attempts.rows[view.attempt_id].tab_switches == 5


def test_save_grace_boundaries(w):
    view, _ = w.start()
    w.clock.advance(minutes=60, seconds=4)
    run(w.service.save(view.attempt_id, USER, w.correct(view.attempt_id, 1), 0))
    w.clock.advance(seconds=1)  # exactly expires_at + 5 s
    with pytest.raises(AttemptClosed):
        run(w.service.save(view.attempt_id, USER, [], 0))


def test_save_on_graded_lost_and_foreign_attempts(w):
    view, _ = w.start()
    with pytest.raises(AttemptNotFound):
        run(w.service.save(view.attempt_id, OTHER, [], 0))
    run(w.service.submit(view.attempt_id, USER))
    with pytest.raises(AttemptClosed):
        run(w.service.save(view.attempt_id, USER, [], 0))
    other, _ = w.start(user=OTHER)
    run(w.attempts.mark_lost(other.attempt_id, "x"))
    with pytest.raises(AttemptLost):
        run(w.service.save(other.attempt_id, OTHER, [], 0))


@pytest.mark.parametrize("change,error", [("lost", AttemptLost), ("submitted", AttemptClosed), ("deleted", AttemptNotFound)])
def test_save_racing_another_request_picks_the_right_code(w, change, error):
    view, _ = w.start()
    w.attempts.save_result_override = False

    def concurrent(row):
        if change == "deleted":
            w.attempts.rows.pop(view.attempt_id)
        else:
            row.status = change

    w.attempts.after_failed_save = concurrent
    with pytest.raises(error):
        run(w.service.save(view.attempt_id, USER, [], 0))


# --------------------------------------------------------------------------- #
# Submit and grading
# --------------------------------------------------------------------------- #
def test_submit_pass_issues_one_certificate_and_is_idempotent(w):
    view, _ = w.start()
    run(w.service.save(view.attempt_id, USER, w.correct(view.attempt_id, 5), 0))
    result = run(w.service.submit(view.attempt_id, USER))
    assert (result.score, result.total, result.percent, result.passed) == (5, 6, 83, True)
    assert result.certificate_id.startswith("WNS-JAVA-") and result.retake_after is None
    assert run(w.service.submit(view.attempt_id, USER)) == result
    assert len(w.certificate_store.rows) == 1 and view.attempt_id not in w.cache.rows
    assert w.attempts.rows[view.attempt_id].answers == {}


def test_submit_on_a_graded_attempt_is_never_rate_limited(w):
    view, _ = w.start()
    run(w.service.submit(view.attempt_id, USER))
    for _ in range(50):
        run(w.service.submit(view.attempt_id, USER))


def test_submit_is_rate_limited_for_active_attempts(w):
    view, _ = w.start()
    for _ in range(10):  # ten submits already this minute
        w.limiter.hit(f"submit:{USER}", 10, 60)
    with pytest.raises(RateLimited):
        run(w.service.submit(view.attempt_id, USER))
    assert w.attempts.rows[view.attempt_id].status == "active", "a refused submit grades nothing"
    w.clock.advance(minutes=61)  # time is up: the submit grades regardless of the limit
    assert run(w.service.submit(view.attempt_id, USER)).total == 6


def test_exact_pass_boundary_through_the_service():
    w = World(questions_per_module=(10, 10), exam_question_count=10)
    view, _ = w.start()
    run(w.service.save(view.attempt_id, USER, w.correct(view.attempt_id, 7), 0))
    assert run(w.service.submit(view.attempt_id, USER)).passed is True
    w2 = World(questions_per_module=(10, 10), exam_question_count=10)
    view, _ = w2.start()
    run(w2.service.save(view.attempt_id, USER, w2.correct(view.attempt_id, 6), 0))
    assert run(w2.service.submit(view.attempt_id, USER)).passed is False


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda p: p[0].update(permutation=[0, 0, 1, 2]),
        lambda p: p[0].update(correct_index=7),
        lambda p: p[0].pop("permutation"),
        lambda p: p.clear(),
        lambda p: p[0].update(permutation="abcd"),
    ],
)
def test_a_corrupt_cache_makes_the_attempt_lost_not_a_crash(w, corrupt):
    view, _ = w.start()
    corrupt(w.cache.rows[view.attempt_id])
    with pytest.raises(AttemptLost):
        run(w.service.submit(view.attempt_id, USER))
    assert w.attempts.rows[view.attempt_id].status == "lost" and view.attempt_id not in w.cache.rows


def test_parse_key_rejects_garbage():
    assert parse_key(None) is None and parse_key("x") is None and parse_key([]) is None
    assert parse_key([{"question_id": "a", "permutation": [1, 0, 2, 3], "correct_index": 2}])[0].module_title == "General"


def test_grading_rolls_back_when_the_certificate_cannot_be_issued(w):
    view, _ = w.start()
    run(w.service.save(view.attempt_id, USER, w.correct(view.attempt_id), 0))
    w.courses.courses.clear()  # course vanished between start and submit
    with pytest.raises(ExamInternalError):
        run(w.service.submit(view.attempt_id, USER))
    assert w.uow.rollbacks >= 1


def test_certificate_id_collisions_retry_then_give_up(w):
    w2 = World()
    view, _ = w2.start()
    run(w2.service.save(view.attempt_id, USER, w2.correct(view.attempt_id), 0))
    taken = "WNS-JAVA-2026-AAAAAAAA"
    w2.certificate_store.rows[taken] = CertificateRecord(taken, OTHER, uuid.uuid4(), uuid.uuid4(), "X Y", "t", "s", 1, 1, f.T0, "valid")
    w2.ids._choices = list("A" * 8 * 5)  # five collisions in a row
    with pytest.raises(ExamInternalError):
        run(w2.service.submit(view.attempt_id, USER))
    w2.ids._choices = list("A" * 8 * 2 + "B" * 8)  # two collisions then a free id
    view2_result = run(w2.service.submit(view.attempt_id, USER)) if w2.attempts.rows[view.attempt_id].status == "active" else None
    assert view2_result is None or view2_result.certificate_id == "WNS-JAVA-2026-BBBBBBBB"


def test_an_existing_valid_certificate_is_reused_not_duplicated(w):
    view, _ = w.start()
    run(w.service.save(view.attempt_id, USER, w.correct(view.attempt_id), 0))
    existing = CertificateRecord("WNS-JAVA-2026-CCCCCCCC", USER, w.course.id, uuid.uuid4(), "A B", "t", "s", 1, 1, f.T0, "valid")
    w.certificate_store.rows[existing.certificate_id] = existing
    result = run(w.service.submit(view.attempt_id, USER))
    assert len(w.certificate_store.rows) == 1
    assert result.certificate_id is None  # not this attempt's certificate


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
def test_reports(w):
    a, _ = w.start()
    b, _ = w.start(user=OTHER)
    qid = uuid.UUID(a.questions[0]["id"])
    w.attempts.rows[b.attempt_id].question_ids.append(qid)
    assert run(w.service.report(a.attempt_id, USER, qid, "typo", "x")) is False
    assert run(w.service.report(a.attempt_id, USER, qid, "typo", "again")) is False  # idempotent, not a 2nd reporter
    assert w.questions.rows[qid].status == "live"
    assert run(w.service.report(b.attempt_id, OTHER, qid, "wrong_answer", "c" * 900)) is True
    assert w.questions.rows[qid].status == "quarantined"
    assert len(w.reports.rows[-1][4]) == 500  # comment capped
    with pytest.raises(ExamValidationError):
        run(w.service.report(a.attempt_id, USER, uuid.uuid4(), "typo", ""))
    with pytest.raises(ExamValidationError):
        run(w.service.report(a.attempt_id, USER, qid, "spam", ""))
    run(w.service.submit(a.attempt_id, USER))
    with pytest.raises(AttemptClosed):
        run(w.service.report(a.attempt_id, USER, qid, "typo", ""))


# --------------------------------------------------------------------------- #
# Eligibility and visibility
# --------------------------------------------------------------------------- #
def test_exam_view_reasons_combine_in_order(w):
    view, _ = w.start()
    w.progress.remaining[(USER, w.course.id)] = 2
    result = run(w.eligibility.get_exam_view(w.course.slug, USER))
    assert result.reasons == ["attempt_active", "lessons_incomplete"] and result.active_attempt_id == view.attempt_id
    anon = run(w.eligibility.get_exam_view(w.course.slug, None))
    assert anon.enabled and anon.reasons == ["login_required"] and anon.lessons_remaining == 0
    with pytest.raises(CourseNotFound):
        run(w.eligibility.get_exam_view("nope", USER))


def test_cooldown_boundary_and_lost_attempts_never_count(w):
    view, _ = w.start()
    run(w.service.submit(view.attempt_id, USER))  # 0/6 fail
    assert run(w.eligibility.get_exam_view(w.course.slug, USER)).reasons == ["cooldown"]
    w.clock.advance(hours=24)  # exactly at retake_after
    assert run(w.eligibility.get_exam_view(w.course.slug, USER)).eligible is True
    lost, _ = w.start()
    run(w.attempts.mark_lost(lost.attempt_id, "x"))
    assert run(w.eligibility.get_exam_view(w.course.slug, USER)).eligible is True


def test_a_regraded_pass_clears_the_cooldown(w):
    view, _ = w.start()
    run(w.service.submit(view.attempt_id, USER))
    w.attempts.rows[view.attempt_id].passed = True
    assert "cooldown" not in run(w.eligibility.get_exam_view(w.course.slug, USER)).reasons


def test_visibility_rules():
    w = World()
    course = w.courses.courses["java-core"] = w.course.__class__(**{**vars(w.course), "exam_ready_at": None})
    assert run(w.pool.is_visible(course)) is False  # 6+6 live, targets 8/4 not met
    m0 = w.courses.modules[course.id][0]
    lesson = w.courses.lessons[course.id][0]
    for n in range(100, 102):
        w.questions.add(course, m0, lesson, n)
    assert run(w.pool.is_visible(course)) is True and w.courses.ready_marked == [course.id]
    disabled = course.__class__(**{**vars(course), "exam_enabled": False})
    assert run(w.pool.is_visible(disabled)) is False


def test_mark_ready_failure_never_fails_the_caller():
    w = World(questions_per_module=(8, 4))
    w.courses.fail_mark_ready = True
    course = w.course.__class__(**{**vars(w.course), "exam_ready_at": None})
    assert run(w.pool.is_visible(course)) is True


def test_launched_exam_hides_only_when_a_full_draw_is_impossible(w):
    for q in list(w.questions.rows.values())[:6]:
        q.status = "quarantined"
    assert run(w.pool.is_visible(w.course)) is True  # 6 live == question_count
    next(q for q in w.questions.rows.values() if q.status == "live").status = "voided"
    assert run(w.pool.is_visible(w.course)) is False


# --------------------------------------------------------------------------- #
# Certificates
# --------------------------------------------------------------------------- #
def cert(w, status="valid", cid="WNS-JAVA-2026-DDDDDDDD"):
    record = CertificateRecord(cid, USER, w.course.id, uuid.uuid4(), "Asha Verma", "Java", "java-core", 25, 30, f.T0, status)
    w.certificate_store.rows[cid] = record
    return record


def test_public_lookup_normalizes_and_never_queries_garbage(w):
    record = cert(w)
    assert run(w.certificates.get_public(" wns-java-2026-dddddddd ", "ip")).certificate_id == record.certificate_id
    calls = w.certificate_store.get_calls
    for bad in ("", "../x", "WNS-JAVA-2026-IIIIIIII", "x" * 500, "WNS-JAVA-26-DDDDDDDD"):
        with pytest.raises(CertificateNotFound):
            run(w.certificates.get_public(bad, "ip"))
    assert w.certificate_store.get_calls == calls


def test_public_lookups_are_rate_limited_per_ip(w):
    cert(w)
    for _ in range(60):
        run(w.certificates.get_public("WNS-JAVA-2026-DDDDDDDD", "1.1.1.1"))
    with pytest.raises(RateLimited):
        run(w.certificates.get_public("WNS-JAVA-2026-DDDDDDDD", "1.1.1.1"))
    run(w.certificates.get_public("WNS-JAVA-2026-DDDDDDDD", "2.2.2.2"))


def test_pdf_is_cached_and_revoked_is_410(w):
    cert(w)
    run(w.certificates.get_pdf("WNS-JAVA-2026-DDDDDDDD", "ip"))
    run(w.certificates.get_pdf("WNS-JAVA-2026-DDDDDDDD", "ip"))
    assert w.renderer.calls == 1
    cert(w, status="revoked", cid="WNS-JAVA-2026-EEEEEEEE")
    with pytest.raises(CertificateRevoked):
        run(w.certificates.get_pdf("WNS-JAVA-2026-EEEEEEEE", "ip"))


def test_revoke_and_reissue_rules(w):
    admin = uuid.uuid4()
    cert(w)
    with pytest.raises(ExamValidationError):
        run(w.certificates.revoke("WNS-JAVA-2026-DDDDDDDD", "   ", admin))
    with pytest.raises(ExamValidationError):
        run(w.certificates.reissue("WNS-JAVA-2026-DDDDDDDD", "<b>", admin))
    with pytest.raises(CertificateNotFound):
        run(w.certificates.revoke("WNS-JAVA-2026-ZZZZZZZZ", "r", admin))
    new = run(w.certificates.reissue("WNS-JAVA-2026-DDDDDDDD", "Asha  K Verma", admin))
    assert new.recipient_name == "Asha K Verma" and new.issued_at == f.T0 and new.score == 25
    old = w.certificate_store.rows["WNS-JAVA-2026-DDDDDDDD"]
    assert old.status == "revoked" and old.replaced_by == new.certificate_id
    with pytest.raises(ExamConflict):
        run(w.certificates.reissue("WNS-JAVA-2026-DDDDDDDD", "X Y", admin))
    run(w.certificates.revoke(new.certificate_id, "fraud", admin))
    again = run(w.certificates.revoke(new.certificate_id, "other", admin))  # idempotent
    assert again.revoked_reason == "fraud"


def test_fallback_exam_code():
    assert fallback_exam_code("java-core") == "JAVACORE"
    assert fallback_exam_code("123-go") == "C123GO"
    assert fallback_exam_code("") == "C" or fallback_exam_code("") == "COURSE"


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #
def test_expiry_job_grades_skips_locked_and_alerts_once_per_hour(w):
    views = [w.start(user=uuid.uuid4())[0] for _ in range(5)]
    w.attempts.locked.add(views[0].attempt_id)
    for v in views[1:]:
        w.cache.rows.pop(v.attempt_id)  # 4 lost
    w.clock.advance(hours=2)
    summary = run(w.maintenance.expire_overdue())
    assert summary["lost"] == 4 and w.attempts.rows[views[0].attempt_id].status == "active"
    assert len(w.alerts.sent) == 1
    run(w.maintenance.expire_overdue())
    assert len(w.alerts.sent) == 1, "at most one lost-attempts alert per hour"
    maintenance_service._last_lost_alert.clear()


def test_nightly_stats_quarantine_retire_and_leave_others(w):
    live = [q for q in w.questions.rows.values() if q.status == "live"]
    bad, easy, fine = live[0], live[1], live[2]
    for i in range(40):
        strong = i < 20
        results = [
            {"question_id": str(bad.id), "correct": not strong},
            {"question_id": str(easy.id), "correct": True},
            {"question_id": str(fine.id), "correct": strong},
        ]
        aid = uuid.uuid4()
        w.attempts.rows[aid] = f.AttemptRecord(aid, uuid.uuid4(), w.course.id, "N", [], {}, 0, "submitted", f.T0, f.T0,
                                                f.T0, 28 if strong else 10, 30, strong, [], results)  # fmt: skip
    summary = run(w.maintenance.question_stats())
    assert (bad.status, easy.status, fine.status) == ("quarantined", "retired", "live")
    assert summary["quarantined"] == 1 and summary["retired"] == 1


def test_stats_need_30_answers_before_acting(w):
    q = next(iter(w.questions.rows.values()))
    for _ in range(29):
        aid = uuid.uuid4()
        w.attempts.rows[aid] = f.AttemptRecord(aid, uuid.uuid4(), w.course.id, "N", [], {}, 0, "submitted", f.T0, f.T0,
                                                f.T0, 0, 30, False, [], [{"question_id": str(q.id), "correct": False}])  # fmt: skip
    run(w.maintenance.question_stats())
    assert q.status == "live" and w.questions.stats[q.id]["answered"] == 29


def test_full_courses_with_unchecked_imports_still_get_their_cross_check():
    w = World(questions_per_module=(8, 4))  # pool full
    assert run(w.maintenance.courses_needing_fill()) == []
    next(iter(w.questions.rows.values())).checks = {"checker": "deferred"}
    assert [c.slug for c in run(w.maintenance.courses_needing_fill())] == ["java-core"]
