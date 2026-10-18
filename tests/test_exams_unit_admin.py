"""Unit tests of the admin pool service with fakes: exam switch, context
upload, edit / restore / void rules, import limits and regrading."""

import asyncio
import os
import random
import uuid

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest

from exams.errors import CourseNotFound, ExamConflict, ExamValidationError, JobNotFound, QuestionNotFound
from exams.services.certificate_service import CertificateService, PdfCache
from exams.services.pool_service import PoolService
from exams.services.question_admin_service import QuestionAdminService
from exams.services.verification import VerificationPipeline
from tests import exam_fakes as f
from tests.test_exams_unit_verification import Embedder, Llm, Runner, agree

ADMIN = uuid.uuid4()


def run(coro):
    return asyncio.run(coro)


class AdminWorld:
    def __init__(self):
        self.settings = f.settings()
        self.clock = f.Clock()
        self.courses = f.Courses()
        self.course = self.courses.add()
        self.module = self.courses.modules[self.course.id][0]
        self.lesson = next(l for l in self.courses.lessons[self.course.id] if l.module_id == self.module.id)
        self.questions = f.Questions()
        self.attempts = f.Attempts()
        self.reports = f.Reports()
        self.contexts = f.Contexts()
        self.jobs = f.Jobs()
        self.logs = f.Logs()
        self.uow = f.Uow()
        self.certs = f.Certificates()
        self.llm = Llm(providers=())
        self.llm.check_answers = agree
        self.pool = PoolService(self.courses, self.questions, self.uow, self.clock, self.settings)
        pipeline = VerificationPipeline(self.questions, self.logs, self.llm, Embedder(), Runner(), self.clock, random.Random(1), self.settings)
        certificates = CertificateService(self.certs, self.uow, self.clock, f.Ids(), f.Renderer(), PdfCache(), f.Limiter(), self.settings)
        self.service = QuestionAdminService(
            self.courses, self.questions, self.attempts, self.reports, self.contexts, self.jobs, self.logs, self.pool,
            pipeline, certificates, self.uow, self.clock, self.settings,
        )  # fmt: skip

    def payload(self, n=1, **overrides):
        values = {
            "course_slug": "java-core", "module_id": self.module.key, "lesson_id": self.lesson.key, "concept": f"c{n}",
            "prompt": f"Manual question {n} about " + " ".join(f"z{n}{i}" for i in range(6)), "difficulty": "easy",
            "options": [f"Right {n}", f"No a{n}", f"No b{n}", f"No c{n}"], "correct_index": 0, "explanation": "x",
        }  # fmt: skip
        values.update(overrides)
        return values


@pytest.fixture
def a():
    return AdminWorld()


def test_set_exam_rules(a):
    with pytest.raises(CourseNotFound):
        run(a.service.set_exam("nope", True, "X1", ADMIN))
    a.courses.add(slug="python", code=None, enabled=False)
    with pytest.raises(ExamValidationError):
        run(a.service.set_exam("python", True, None, ADMIN))
    with pytest.raises(ExamValidationError):
        run(a.service.set_exam("python", True, "1BAD", ADMIN))
    course, needs_fill = run(a.service.set_exam("python", True, "py", ADMIN))
    assert course.exam_code == "PY" and course.exam_enabled and needs_fill
    course, needs_fill = run(a.service.set_exam("python", False, None, ADMIN))
    assert course.exam_code == "PY" and not course.exam_enabled and not needs_fill, "turning off keeps the code"


def test_context_upload_rules(a):
    with pytest.raises(ExamValidationError) as raised:
        run(a.service.upload_context("java-core", [{"module_id": "ghost"}], ADMIN))
    assert "ghost" in raised.value.message
    ctx = {"module_id": a.module.key, "lessons": [{"lesson_id": a.lesson.key, "summary": "javac makes bytecode for the JVM", "key_points": []}]}
    assert run(a.service.upload_context("java-core", [ctx], ADMIN)) == {"updated": 1, "unchanged": 0, "quarantined": 0}
    assert run(a.service.upload_context("java-core", [dict(ctx)], ADMIN))["unchanged"] == 1
    by_uuid = {**ctx, "module_id": str(a.module.id), "lessons": []}
    assert run(a.service.upload_context("java-core", [by_uuid], ADMIN))["updated"] == 1, "module uuid also accepted"


def test_manual_create_edit_restore_and_void_rules(a):
    item = run(a.service.create_manual(a.payload(), ADMIN))
    assert item["status"] == "live" and item["checks"]["checker"] == "skipped_no_quota"
    with pytest.raises(ExamValidationError):
        run(a.service.create_manual(a.payload(2, lesson_id="nope"), ADMIN))
    with pytest.raises(ExamValidationError):
        run(a.service.create_manual(a.payload(3, module_id="ghost"), ADMIN))
    qid = item["id"]
    with pytest.raises(ExamValidationError):
        run(a.service.edit(qid, {"status": "voided"}, ADMIN))
    with pytest.raises(QuestionNotFound):
        run(a.service.edit(uuid.uuid4(), {"prompt": "x"}, ADMIN))

    # An edit that fails the checks takes a live question out of draws.
    bad = run(a.service.edit(qid, {"options": ["same", "same", "x", "y"]}, ADMIN))
    assert bad["status"] == "quarantined" and bad["reject_reason"].startswith("shape")
    fixed = run(a.service.edit(qid, {"options": ["Right 1", "No a1", "No b1", "No c1"]}, ADMIN))
    assert fixed["status"] == "live"

    a.questions.rows[qid].status = "rejected"
    with pytest.raises(ExamConflict):
        run(a.service.edit(qid, {"status": "live"}, ADMIN))
    a.questions.rows[qid].status = "quarantined"
    assert run(a.service.edit(qid, {"status": "live"}, ADMIN))["status"] == "live"

    assert run(a.service.void(qid, ADMIN))["status"] == "voided"
    assert run(a.service.void(qid, ADMIN))["regraded_attempts"] == 0, "voiding twice is harmless"
    with pytest.raises(ExamConflict):
        run(a.service.edit(qid, {"prompt": "changed"}, ADMIN))
    with pytest.raises(QuestionNotFound):
        run(a.service.void(uuid.uuid4(), ADMIN))


def test_edit_cannot_copy_another_question(a):
    first = run(a.service.create_manual(a.payload(1), ADMIN))
    second = run(a.service.create_manual(a.payload(2), ADMIN))
    copy = run(a.service.edit(second["id"], {"prompt": first["prompt"], "options": first["options"], "correct_index": first["correct_index"]}, ADMIN))
    assert copy["status"] == "quarantined" and "duplicate" in copy["reject_reason"]


def test_import_limits_and_line_reporting(a):
    with pytest.raises(ExamValidationError):
        run(a.service.import_questions([{}] * 501, ADMIN))
    result = run(a.service.import_questions([a.payload(1), None, "x", a.payload(2, course_slug="ghost"), a.payload(3, module_id="ghost")], ADMIN))
    assert result.imported == 1
    assert result.rejected == [
        {"line": 2, "reason": "not_an_object"}, {"line": 3, "reason": "not_an_object"},
        {"line": 4, "reason": "unknown_course"}, {"line": 5, "reason": "course_mismatch"},
    ]  # fmt: skip
    assert run(a.service.import_questions([], ADMIN)).imported == 0


def test_void_regrade_issues_new_certificates_and_never_revokes(a):
    q = [a.questions.add(a.course, a.module, a.lesson, n) for n in range(10)]
    user = uuid.uuid4()
    results = [{"question_id": str(x.id), "module_title": "M", "answered": True, "correct": i < 6} for i, x in enumerate(q)]
    aid = uuid.uuid4()
    a.attempts.rows[aid] = f.AttemptRecord(aid, user, a.course.id, "Asha Verma", [x.id for x in q], {}, 0, "submitted",
                                            f.T0, f.T0, f.T0, 6, 10, False, [], results)  # fmt: skip
    out = run(a.service.void(q[9].id, ADMIN))  # 6/9 = 66.7%: still a fail
    assert out["new_certificates"] == 0 and a.attempts.rows[aid].total == 9
    out = run(a.service.void(q[8].id, ADMIN))  # 6/8 = 75%: pass
    assert out["new_certificates"] == 1 and a.attempts.rows[aid].passed is True
    run(a.service.void(q[0].id, ADMIN))  # voiding a right answer: 5/7 = 71%
    run(a.service.void(q[1].id, ADMIN))  # 4/6 = 67% - still passed, certificate kept
    assert a.attempts.rows[aid].passed is True
    assert [c.status for c in a.certs.rows.values()] == ["valid"]


def test_regrade_with_everything_voided_never_divides_by_zero(a):
    q = a.questions.add(a.course, a.module, a.lesson, 1)
    aid = uuid.uuid4()
    a.attempts.rows[aid] = f.AttemptRecord(aid, uuid.uuid4(), a.course.id, "N", [q.id], {}, 0, "expired", f.T0, f.T0, f.T0,
                                            0, 1, False, [], [{"question_id": str(q.id), "module_title": "M", "answered": False, "correct": False}])  # fmt: skip
    run(a.service.void(q.id, ADMIN))
    assert (a.attempts.rows[aid].score, a.attempts.rows[aid].total, a.attempts.rows[aid].passed) == (0, 0, False)


def test_summary_list_job_and_quota(a):
    run(a.service.create_manual(a.payload(), ADMIN))
    summary = run(a.service.summary("java-core"))
    assert summary["live"] == 1 and summary["modules"][0]["module_id"] == a.module.key and summary["ready"] is False
    assert run(a.service.list_questions("java-core", "live", "ghost-module")) == []
    assert len(run(a.service.list_questions("java-core", None, a.module.key))) == 1
    with pytest.raises(JobNotFound):
        run(a.service.get_job(uuid.uuid4()))
    models = run(a.service.quota())
    assert {m["provider"] for m in models} == {"gemini", "groq"}
    assert all(m["remaining"] == {"requests": None, "tokens": None} for m in models), "unknown limits stay null"
