"""In-memory fakes of every exams/protocols.py interface, mirroring the SQL
stores' rules (unique constraints raise the same signals, conditional
updates behave the same), so services can be unit tested without a database."""

import contextlib
import random
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from exams.errors import (
    ActiveAttemptExists,
    CertificateIdTaken,
    DuplicateQuestion,
    JobAlreadyRunning,
    ValidCertificateExists,
)
from exams.infra.system import SlidingWindowRateLimiter
from exams.records import (
    AttemptRecord,
    CandidateRow,
    CertificateRecord,
    CourseRecord,
    DedupeRow,
    JobRecord,
    LessonRecord,
    ModuleContextRecord,
    ModuleRecord,
    NewQuestion,
    ProviderUsage,
    QuestionRecord,
    ServeRow,
)
from types import SimpleNamespace

T0 = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)


def settings(**overrides):
    values = dict(
        exam_question_count=6, exam_duration_minutes=60, exam_pass_percent=70, exam_retake_hours=24,
        exam_submit_grace_seconds=30, exam_save_grace_seconds=5, exam_pool_target=12, exam_pool_min_per_module=3,
        exam_gen_batch_size=5, exam_gen_daily_call_limit=300, exam_checker_min_confidence=0.8,
        exam_duplicate_jaccard=0.8, exam_duplicate_cosine=0.9, exam_duplicate_cosine_same_lesson=0.85,
        exam_code_run_daily_limit=150, exam_max_per_concept=2, exam_max_serves=0, exam_reports_to_quarantine=2,
        gemini_daily_request_limit=0, gemini_daily_token_limit=0, groq_daily_request_limit=0, groq_daily_token_limit=0,
        gemini_model="gemini-x", groq_model="groq-x", exam_enabled=False, exam_auto_exclude="",
        certificate_verify_base_url="https://www.webneststudio.co.in/certificates",
    )  # fmt: skip
    values.update(overrides)
    return SimpleNamespace(**values)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta) -> None:
        self.current += timedelta(**delta)


class Ids:
    def __init__(self, choices: str | None = None) -> None:
        self._choices = list(choices) if choices else None
        self._rng = random.Random(7)

    def new_uuid(self) -> uuid.UUID:
        return uuid.UUID(int=self._rng.getrandbits(128), version=4)

    def choice(self, alphabet: str) -> str:
        if self._choices:
            return self._choices.pop(0)
        return self._rng.choice(alphabet)


class Rand:
    def __init__(self, seed: int = 1) -> None:
        self.seed = seed

    def draw_random(self) -> random.Random:
        self.seed += 1
        return random.Random(self.seed)


class Limiter(SlidingWindowRateLimiter):
    def __init__(self) -> None:
        self.t = 0.0
        super().__init__(monotonic=lambda: self.t)


class Uow:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.fail_commit = False

    async def commit(self) -> None:
        if self.fail_commit:
            from exams.errors import ExamStoreUnavailable

            raise ExamStoreUnavailable()
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    def savepoint(self):
        return _AsyncNull()


class _AsyncNull(contextlib.AbstractAsyncContextManager):
    async def __aexit__(self, *exc):
        return False


# --------------------------------------------------------------------------- #
# Courses, progress
# --------------------------------------------------------------------------- #
class Courses:
    def __init__(self) -> None:
        self.courses: dict[str, CourseRecord] = {}
        self.modules: dict[uuid.UUID, list[ModuleRecord]] = {}
        self.lessons: dict[uuid.UUID, list[LessonRecord]] = {}
        self.ready_marked: list[uuid.UUID] = []
        self.materials: dict = {}
        self.fail_mark_ready = False

    def add(self, slug="java-core", code="JAVA", enabled=True, ready=True, status="published", module_lessons=(2, 1)):
        course = CourseRecord(
            id=uuid.uuid4(), slug=slug, title=slug.title(), status=status, exam_code=code, exam_enabled=enabled,
            exam_ready_at=T0 if ready else None,
        )  # fmt: skip
        self.courses[slug] = course
        modules, lessons = [], []
        for i, count in enumerate(module_lessons):
            module = ModuleRecord(id=uuid.uuid4(), key=f"{slug}-m{i}", title=f"Module {i}", display_order=i, published_lessons=count)
            modules.append(module)
            for j in range(count):
                lessons.append(LessonRecord(id=uuid.uuid4(), key=f"{slug}-l{i}-{j}", module_id=module.id, course_id=course.id, title=f"L{i}.{j}", status="published"))
        self.modules[course.id] = modules
        self.lessons[course.id] = lessons
        return course

    async def get_published_course(self, slug):
        course = self.courses.get(slug)
        return course if course and course.status == "published" else None

    async def get_course(self, slug):
        return self.courses.get(slug)

    async def get_course_by_id(self, course_id):
        return next((c for c in self.courses.values() if c.id == course_id), None)

    async def list_exam_modules(self, course_id):
        return list(self.modules.get(course_id, []))

    async def list_lessons(self, course_id):
        return list(self.lessons.get(course_id, []))

    async def set_exam_settings(self, course_id, enabled, exam_code, admin_disabled=None):
        for slug, c in self.courses.items():
            if c.id == course_id:
                flag = c.exam_admin_disabled if admin_disabled is None else admin_disabled
                self.courses[slug] = replace(c, exam_enabled=enabled, exam_code=exam_code, exam_admin_disabled=flag)

    async def mark_ready(self, course_id, now):
        if self.fail_mark_ready:
            raise RuntimeError("db down")
        self.ready_marked.append(course_id)
        for slug, c in self.courses.items():
            if c.id == course_id and c.exam_ready_at is None:
                self.courses[slug] = replace(c, exam_ready_at=now)

    async def list_enabled_courses(self):
        return [c for c in self.courses.values() if c.exam_enabled]

    async def list_published_courses(self):
        return [c for c in self.courses.values() if c.status == "published"]

    async def used_exam_codes(self):
        return {c.exam_code for c in self.courses.values() if c.exam_code}

    async def lesson_materials(self, course_id):
        return list(self.materials.get(course_id, []))


class Progress:
    def __init__(self) -> None:
        self.remaining: dict[tuple, int] = {}

    async def remaining_lessons(self, user_id, course_id):
        return self.remaining.get((user_id, course_id), 0)


# --------------------------------------------------------------------------- #
# Questions
# --------------------------------------------------------------------------- #
class Questions:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, QuestionRecord] = {}
        self.reports: dict[uuid.UUID, int] = {}
        self.stats: dict[uuid.UUID, dict] = {}
        self.serve_override: list[ServeRow] | None = None

    def add(self, course, module, lesson, n, status="live", concept=None, difficulty="easy", source="ai", checks=None):
        from exams.domain.validation import normalize_text, prompt_hash

        prompt = f"Question number {n} for {module.key} " + " ".join(f"w{n}x{i}" for i in range(6))
        options = [f"Right {n}", f"Wrong a{n}", f"Wrong b{n}", f"Wrong c{n}"]
        norm = normalize_text(prompt)
        q = QuestionRecord(
            id=uuid.uuid4(), course_id=course.id, module_id=module.id, lesson_id=lesson.id,
            concept=concept or f"concept {n}", kind="concept", difficulty=difficulty, prompt=prompt, options=options,
            correct_index=0, explanation="e", why_others_wrong=None, source_fact=None, code=None, code_language=None,
            expected_output=None, status=status, source=source, reject_reason=None, prompt_norm=norm,
            prompt_hash=prompt_hash(norm, options[0]), embedding=None, checks=checks, generator_model=None,
            generation_job_id=None, times_served=0, created_at=T0, updated_at=T0,
        )  # fmt: skip
        self.rows[q.id] = q
        return q

    async def live_candidates(self, course_id):
        return [CandidateRow(q.id, q.course_id, q.module_id, q.lesson_id, q.difficulty, q.concept)
                for q in self.rows.values() if q.course_id == course_id and q.status == "live"]  # fmt: skip

    async def load_for_serving(self, course_id, ids):
        if self.serve_override is not None:
            return self.serve_override
        return [ServeRow(q.id, q.course_id, q.module_id, q.prompt, list(q.options), q.correct_index)
                for q in self.rows.values() if q.id in set(ids) and q.course_id == course_id and q.status == "live"]  # fmt: skip

    async def increment_served(self, ids):
        for qid in ids:
            self.rows[qid].times_served += 1

    async def status_counts(self, course_id):
        out: dict = {}
        for q in self.rows.values():
            if q.course_id == course_id:
                out.setdefault(q.module_id, {}).setdefault(q.status, 0)
                out[q.module_id][q.status] += 1
        return out

    async def live_counts(self, course_id):
        out: dict = {}
        for q in self.rows.values():
            if q.course_id == course_id and q.status == "live":
                out[q.module_id] = out.get(q.module_id, 0) + 1
        return out

    async def list_questions(self, course_id, status, module_id, limit):
        return [q for q in self.rows.values() if q.course_id == course_id and (not status or q.status == status)
                and (not module_id or q.module_id == module_id)][:limit]  # fmt: skip

    async def get(self, qid):
        q = self.rows.get(qid)
        return replace(q) if q else None

    get_for_update = get

    async def insert(self, nq: NewQuestion):
        if nq.status != "rejected" and any(
            q.course_id == nq.course_id and q.prompt_hash == nq.prompt_hash and q.status != "rejected" for q in self.rows.values()
        ):
            raise DuplicateQuestion()
        qid = uuid.uuid4()
        self.rows[qid] = QuestionRecord(id=qid, times_served=0, created_at=T0, updated_at=T0, **vars(nq))
        return qid

    async def update(self, qid, fields):
        q = self.rows[qid]
        for k, v in fields.items():
            setattr(q, k, v)

    async def set_status(self, ids, status, from_statuses):
        n = 0
        for qid in ids:
            q = self.rows.get(qid)
            if q and q.status in from_statuses:
                q.status = status
                n += 1
        return n

    async def dedupe_rows(self, course_id, exclude_id=None):
        return [DedupeRow(q.id, q.lesson_id, q.concept, q.prompt_norm, q.prompt_hash, q.embedding)
                for q in self.rows.values() if q.course_id == course_id and q.status != "rejected" and q.id != exclude_id]  # fmt: skip

    async def ids_with_status(self, course_id, status):
        return {q.id for q in self.rows.values() if q.course_id == course_id and q.status == status}

    async def report_counts(self, ids):
        return {i: self.reports[i] for i in ids if i in self.reports}

    async def stats_for(self, ids):
        return {i: self.stats[i] for i in ids if i in self.stats}

    async def live_by_module(self, module_id):
        return [replace(q) for q in self.rows.values() if q.module_id == module_id and q.status == "live"]

    async def unchecked_seed_questions(self, course_id, limit):
        return [replace(q) for q in self.rows.values() if q.course_id == course_id and q.status == "live"
                and (q.checks or {}).get("checker") in ("skipped_no_quota", "deferred")][:limit]  # fmt: skip

    async def over_served(self, course_id, max_serves):
        return [q.id for q in self.rows.values() if q.course_id == course_id and q.status == "live" and q.times_served >= max_serves]

    async def upsert_stats(self, rows, now):
        for row in rows:
            self.stats[row["question_id"]] = {k: row[k] for k in ("answered", "correct", "discrimination")}


# --------------------------------------------------------------------------- #
# Attempts, cache, certificates, reports
# --------------------------------------------------------------------------- #
class Attempts:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, AttemptRecord] = {}
        self.locked: set[uuid.UUID] = set()
        self.raise_active_exists = 0  # how many inserts to refuse
        self.save_result_override: bool | None = None
        self.after_failed_save = None

    async def get(self, aid):
        a = self.rows.get(aid)
        return replace(a, answers=dict(a.answers)) if a else None

    async def get_for_update(self, aid, skip_locked=False):
        if skip_locked and aid in self.locked:
            return None
        return await self.get(aid)

    async def active_for(self, user_id, course_id):
        return next((replace(a) for a in self.rows.values() if a.user_id == user_id and a.course_id == course_id and a.status == "active"), None)

    async def latest_graded(self, user_id, course_id):
        graded = [a for a in self.rows.values() if a.user_id == user_id and a.course_id == course_id and a.graded]
        return replace(max(graded, key=lambda a: a.submitted_at)) if graded else None

    async def seen_questions(self, user_id, course_id):
        seen: dict = {}
        for a in self.rows.values():
            if a.user_id == user_id and a.course_id == course_id:
                for q in a.question_ids:
                    seen[q] = max(seen.get(q, a.started_at), a.started_at)
        return seen

    async def insert(self, attempt):
        if self.raise_active_exists > 0:
            self.raise_active_exists -= 1
            raise ActiveAttemptExists()
        if any(a.user_id == attempt.user_id and a.course_id == attempt.course_id and a.status == "active" for a in self.rows.values()):
            raise ActiveAttemptExists()
        self.rows[attempt.id] = replace(attempt)

    async def save_answers(self, aid, user_id, answers, tab_switches, now):
        if self.save_result_override is not None:
            if self.after_failed_save:
                self.after_failed_save(self.rows.get(aid))
            return self.save_result_override
        a = self.rows.get(aid)
        if not a or a.user_id != user_id or a.status != "active" or not a.expires_at > now:
            return False
        a.answers = dict(answers)
        a.tab_switches = max(a.tab_switches, tab_switches)
        return True

    async def mark_graded(self, attempt):
        a = self.rows[attempt.id]
        if a.status == "active":
            self.rows[attempt.id] = replace(attempt, answers={})

    async def mark_lost(self, aid, reason, now=None):
        a = self.rows.get(aid)
        if a and a.status == "active":
            a.status, a.lost_reason, a.submitted_at = "lost", reason, now or a.started_at
            return True
        return False

    async def update_grade(self, aid, score, total, passed, breakdown):
        a = self.rows[aid]
        a.score, a.total, a.passed, a.module_breakdown = score, total, passed, breakdown

    async def overdue_ids(self, cutoff, limit):
        return [a.id for a in self.rows.values() if a.status == "active" and a.expires_at < cutoff][:limit]

    async def graded_containing(self, qid):
        return [replace(a) for a in self.rows.values() if a.graded and qid in a.question_ids]

    async def graded_results(self, course_id):
        return [(a.score or 0, a.total, list(a.results or [])) for a in self.rows.values() if a.course_id == course_id and a.graded and a.results]

    async def count_lost_since(self, since):
        return sum(1 for a in self.rows.values() if a.status == "lost" and a.submitted_at and a.submitted_at >= since)


class Cache:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, list] = {}

    async def get(self, aid, user_id):
        return self.rows.get(aid)

    async def exists(self, aid, user_id):
        return aid in self.rows

    async def put(self, aid, user_id, payload, expires_at):
        self.rows[aid] = payload

    async def delete(self, aid, user_id):
        self.rows.pop(aid, None)

    async def sweep(self, now):
        return 0


class Certificates:
    def __init__(self) -> None:
        self.rows: dict[str, CertificateRecord] = {}
        self.get_calls = 0

    async def get(self, cid):
        self.get_calls += 1
        return self.rows.get(cid)

    get_for_update = get

    async def valid_for(self, user_id, course_id):
        return next((c for c in self.rows.values() if c.user_id == user_id and c.course_id == course_id and c.status == "valid"), None)

    async def valid_for_attempt(self, aid):
        return next((c for c in self.rows.values() if c.attempt_id == aid and c.status == "valid"), None)

    async def insert(self, cert):
        if cert.certificate_id in self.rows:
            raise CertificateIdTaken()
        if cert.status == "valid" and (
            await self.valid_for(cert.user_id, cert.course_id) or await self.valid_for_attempt(cert.attempt_id)
        ):
            raise ValidCertificateExists()
        self.rows[cert.certificate_id] = cert

    async def list_for_user(self, user_id):
        return sorted((c for c in self.rows.values() if c.user_id == user_id), key=lambda c: c.issued_at, reverse=True)

    async def search(self, query, limit):
        q = (query or "").lower()
        return [replace(c, user_email="x@example.com") for c in self.rows.values()
                if not q or q in c.certificate_id.lower() or q in c.recipient_name.lower()][:limit]  # fmt: skip

    async def revoke(self, cid, reason, now, replaced_by):
        self.rows[cid] = replace(self.rows[cid], status="revoked", revoked_reason=reason, revoked_at=now, replaced_by=replaced_by)


class Reports:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    async def add(self, qid, aid, user_id, reason, comment):
        if any(r[0] == qid and r[1] == aid for r in self.rows):
            return False
        self.rows.append((qid, aid, user_id, reason, comment, None))
        return True

    async def distinct_open_reporters(self, qid):
        return len({r[2] for r in self.rows if r[0] == qid and r[5] is None})

    async def resolve_for_question(self, qid, admin_id, now):
        self.rows = [(r[0], r[1], r[2], r[3], r[4], now if r[0] == qid else r[5]) for r in self.rows]


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
class Logs:
    def __init__(self) -> None:
        self.entries = []
        self.extra_calls = 0
        self.usage: list[ProviderUsage] = []

    async def add(self, entry):
        self.entries.append(entry)

    async def calls_since(self, since, purposes):
        return self.extra_calls + sum(1 for e in self.entries if e.purpose in purposes)

    async def usage_since(self, since, hour_ago):
        return list(self.usage)


class Contexts:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, ModuleContextRecord] = {}

    async def get_many(self, course_id):
        return {m: c for m, c in self.rows.items() if c.course_id == course_id}

    async def upsert(self, module_id, course_id, context, context_hash, now):
        self.rows[module_id] = ModuleContextRecord(module_id, course_id, context, context_hash)


class Jobs:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, JobRecord] = {}

    async def create_running(self, course_id, trigger, now):
        if any(j.course_id == course_id and j.status == "running" for j in self.rows.values()):
            raise JobAlreadyRunning()
        job = JobRecord(id=uuid.uuid4(), course_id=course_id, trigger=trigger, status="running", started_at=now, heartbeat_at=now, created_at=now)
        self.rows[job.id] = job
        return replace(job)

    async def get(self, job_id):
        j = self.rows.get(job_id)
        return replace(j) if j else None

    async def running_for(self, course_id):
        return next((replace(j) for j in self.rows.values() if j.course_id == course_id and j.status == "running"), None)

    async def latest_for(self, course_id):
        jobs = [j for j in self.rows.values() if j.course_id == course_id]
        return replace(max(jobs, key=lambda j: j.created_at)) if jobs else None

    async def save(self, job):
        self.rows[job.id] = replace(job)

    async def fail_stale(self, before, now):
        n = 0
        for j in self.rows.values():
            if j.status == "running" and j.heartbeat_at and j.heartbeat_at < before:
                j.status, j.finished_at = "failed", now
                n += 1
        return n


class Alerts:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, subject, body):
        self.sent.append((subject, body))


class Renderer:
    def __init__(self) -> None:
        self.calls = 0

    def render(self, certificate, verify_url):
        self.calls += 1
        return b"%PDF-fake " + certificate.certificate_id.encode()
