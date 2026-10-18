"""Shared harness for the exam integration tests: a real Postgres database
(EXAMS_TEST_DATABASE_URL, never the production one), the real exam and
learning routers over HTTP, a controllable clock, and seed data.

Tests are skipped when EXAMS_TEST_DATABASE_URL is not set or unreachable."""

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from api.learning_router import admin_router as learning_admin_router
from api.learning_router import progress_router as learning_progress_router
from api.learning_router import router as learning_router
from core.config import Settings
from core.dependencies import get_current_user, get_db_session
from core.error_handlers import register_error_handlers
from database import models  # noqa: F401 - registers tables
from database.session import Base
from exams import dependencies as exam_dependencies
from exams import models as exam_models  # noqa: F401
from exams.dependencies import ExamServices, get_exam_services, get_optional_user_strict
from exams.domain.validation import normalize_text, prompt_hash
from exams.router_admin import router as exam_admin_router
from exams.router_learner import router as exam_learner_router
from exams.router_public import router as exam_public_router
from exams.schema_upgrade import upgrade_schema

TEST_DB_URL = os.environ.get("EXAMS_TEST_DATABASE_URL", "")


def async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1) if url.startswith("postgresql://") else url


def make_settings(**overrides) -> Settings:
    values = dict(
        supabase_url=TEST_DB_URL or "postgresql://x@localhost/x",
        jwt_secret_key="test-secret",
        gemini_api_key="",
        groq_api_key="",
        jdoodle_client_id="",
        jdoodle_client_secret="",
        email_enabled=False,
        exam_generation_enabled=False,
        team_notification_email="",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime.now(timezone.utc).replace(microsecond=0)

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta) -> None:
        self.current += timedelta(**delta)


def require_db():
    if not TEST_DB_URL:
        pytest.skip("EXAMS_TEST_DATABASE_URL is not set")


class Harness:
    """Owns the engine, the test app and seed helpers."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or make_settings()
        self.engine = create_async_engine(async_url(TEST_DB_URL), poolclass=NullPool)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.users: dict[str, SimpleNamespace] = {}
        self.app = self._build_app()

    # -- app -------------------------------------------------------------- #
    def _build_app(self) -> FastAPI:
        app = FastAPI()
        for router in (learning_router, learning_admin_router, learning_progress_router, exam_learner_router, exam_public_router, exam_admin_router):
            app.include_router(router)
        register_error_handlers(app)

        async def db_session():
            async with self.sessions() as session:
                yield session

        def user_from_header(x_test_user: str | None = Header(default=None)):
            if not x_test_user:
                raise HTTPException(status_code=401, detail="Missing bearer token")
            return self.users[x_test_user]

        def optional_user(x_test_user: str | None = Header(default=None)):
            if x_test_user == "bad-token":
                raise HTTPException(status_code=401, detail="Invalid or expired access token")
            return self.users.get(x_test_user) if x_test_user else None

        def exam_services(session: AsyncSession = Depends(get_db_session)):
            return ExamServices(session, self.settings)

        app.dependency_overrides[get_db_session] = db_session
        app.dependency_overrides[get_current_user] = user_from_header
        app.dependency_overrides[get_optional_user_strict] = optional_user
        app.dependency_overrides[get_exam_services] = exam_services
        return app

    def client(self) -> TestClient:
        return TestClient(self.app, raise_server_exceptions=False)

    # -- database --------------------------------------------------------- #
    async def reset(self) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await upgrade_schema(self.engine)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def execute(self, sql: str, **params):
        async with self.engine.begin() as conn:
            return await conn.execute(text(sql), params)

    async def scalar(self, sql: str, **params):
        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar()

    async def rows(self, sql: str, **params):
        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).all()

    async def add_user(self, name: str, role: str = "client") -> SimpleNamespace:
        user_id = uuid.uuid4()
        await self.execute(
            "insert into users (id, email, password_hash, role, is_verified, is_active, full_name) "
            "values (:id, :email, 'x', :role, true, true, :name)",
            id=user_id, email=f"{name}@example.com", role=role, name=name,
        )  # fmt: skip
        user = SimpleNamespace(id=user_id, role=role, email=f"{name}@example.com", is_active=True)
        self.users[name] = user
        return user

    async def add_course(
        self, slug: str, code: str, module_lessons: list[int], questions_per_module: list[int] | None = None,
        enabled: bool = True,
    ) -> dict:
        course_id = uuid.uuid4()
        await self.execute(
            "insert into courses (id, slug, title, status, exam_code, exam_enabled) values (:id, :slug, :title, 'published', :code, :enabled)",
            id=course_id, slug=slug, title=slug.replace("-", " ").title(), code=code, enabled=enabled,
        )  # fmt: skip
        modules = []
        for m_index, lesson_count in enumerate(module_lessons):
            module_id = uuid.uuid4()
            module_key = f"mod_{slug}_{m_index}"
            await self.execute(
                "insert into course_modules (id, course_id, key, title, display_order, status) values (:id, :c, :k, :t, :o, 'published')",
                id=module_id, c=course_id, k=module_key, t=f"{slug} module {m_index}", o=m_index,
            )  # fmt: skip
            lessons = []
            for l_index in range(lesson_count):
                lesson_id = uuid.uuid4()
                key = f"lesson_{slug}_{m_index}_{l_index}"
                await self.execute(
                    "insert into lessons (id, module_id, course_id, key, title, status, display_order) "
                    "values (:id, :m, :c, :k, :t, 'published', :o)",
                    id=lesson_id, m=module_id, c=course_id, k=key, t=f"Lesson {m_index}.{l_index}", o=l_index,
                )  # fmt: skip
                lessons.append({"id": lesson_id, "key": key})
            modules.append({"id": module_id, "key": module_key, "lessons": lessons, "title": f"{slug} module {m_index}"})
        course = {"id": course_id, "slug": slug, "modules": modules}
        if questions_per_module:
            for module, count in zip(modules, questions_per_module):
                for q in range(count):
                    await self.add_question(course, module, f"{slug} q{q} of {module['key']}")
        return course

    async def add_question(self, course: dict, module: dict, prompt: str, status: str = "live", concept: str | None = None) -> uuid.UUID:
        question_id = uuid.uuid4()
        lesson = module["lessons"][0]
        options = ["Right answer", "Wrong one", "Wrong two", "Wrong three"]
        norm = normalize_text(prompt)
        await self.execute(
            "insert into exam_questions (id, course_id, module_id, lesson_id, concept, kind, difficulty, prompt, options, "
            "correct_index, explanation, status, source, prompt_norm, prompt_hash) values (:id, :c, :m, :l, :concept, 'concept', "
            ":d, :p, cast(:o as jsonb), 0, 'Because.', :s, 'seed', :n, :h)",
            id=question_id, c=course["id"], m=module["id"], l=lesson["id"], concept=concept or f"concept {prompt}",
            d=("easy", "medium", "hard")[len(prompt) % 3], p=prompt, o=json.dumps(options), s=status,
            n=norm, h=prompt_hash(norm, options[0]),
        )  # fmt: skip
        return question_id

    async def complete_all_lessons(self, user: SimpleNamespace, course: dict) -> None:
        for module in course["modules"]:
            for lesson in module["lessons"]:
                await self.execute(
                    "insert into user_lesson_progress (user_id, lesson_id, status, completed_percent) values (:u, :l, 'completed', 100) "
                    "on conflict (user_id, lesson_id) do update set status='completed'",
                    u=user.id, l=lesson["id"],
                )  # fmt: skip

    async def cache_payload(self, attempt_id) -> list | None:
        return await self.scalar("select payload from exam_attempt_cache where attempt_id = :a", a=attempt_id)


def run(coro):
    return asyncio.run(coro)


def correct_answers(payload: list[dict], how_many: int | None = None) -> list[dict]:
    """Displayed indexes of the right options, from the cache payload."""
    answers = []
    for entry in payload[: how_many if how_many is not None else len(payload)]:
        displayed = entry["permutation"].index(entry["correct_index"])
        answers.append({"question_id": entry["question_id"], "option_index": displayed})
    return answers


FORBIDDEN_KEYS = {"correct_index", "explanation", "permutation", "answer", "correct_answer", "why_others_wrong", "source_fact"}


def assert_no_answer_fields(data) -> None:
    if isinstance(data, dict):
        leaked = FORBIDDEN_KEYS & set(data)
        assert not leaked, f"answer fields leaked: {leaked}"
        for value in data.values():
            assert_no_answer_fields(value)
    elif isinstance(data, list):
        for value in data:
            assert_no_answer_fields(value)


def reset_process_state() -> None:
    exam_dependencies.memory_cache._entries.clear()
    exam_dependencies.rate_limiter._events.clear()
    exam_dependencies.pdf_cache._entries.clear()
