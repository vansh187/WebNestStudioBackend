"""Reports members make about AI replies. Each method opens its own short session with a time
limit, so no connection is held between requests."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import DatabaseError
from database.models import AssistantCallLog, AssistantReport, User

DB_TIMEOUT_SECONDS = 10.0
_ERROR = "Could not reach the reports right now. Please try again shortly."


@dataclass(frozen=True)
class NewAssistantReport:
    user_id: uuid.UUID
    reason: str
    details: str | None
    reply_text: str
    prompt_text: str | None
    client_message_id: str | None
    provider_name: str | None
    model: str | None


@dataclass(frozen=True)
class SavedAssistantReport:
    id: uuid.UUID
    user_id: uuid.UUID | None
    reporter_email: str | None
    reason: str
    details: str | None
    reply_text: str
    prompt_text: str | None
    client_message_id: str | None
    provider_name: str | None
    model: str | None
    status: str
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: uuid.UUID | None


@dataclass(frozen=True)
class AnsweredBy:
    provider: str
    model: str


class AssistantReportStore(Protocol):
    async def latest_answer(self, user_id: uuid.UUID) -> AnsweredBy | None: ...

    async def create(self, report: NewAssistantReport) -> uuid.UUID: ...

    async def list_by_status(self, status: str, limit: int) -> list[SavedAssistantReport]: ...

    async def resolve(self, report_id: uuid.UUID, admin_id: uuid.UUID) -> SavedAssistantReport | None: ...


def _with_reporter() -> Select[tuple[AssistantReport, str | None]]:
    return select(AssistantReport, User.email).outerjoin(User, User.id == AssistantReport.user_id)


def _saved(report: AssistantReport, email: str | None) -> SavedAssistantReport:
    return SavedAssistantReport(
        id=report.id,
        user_id=report.user_id,
        reporter_email=email,
        reason=report.reason,
        details=report.details,
        reply_text=report.reply_text,
        prompt_text=report.prompt_text,
        client_message_id=report.client_message_id,
        provider_name=report.provider_name,
        model=report.model,
        status=report.status,
        created_at=report.created_at,
        resolved_at=report.resolved_at,
        resolved_by=report.resolved_by,
    )


class SqlAssistantReportStore:
    def __init__(self, session_factory: Callable[[], AsyncSession], timeout_seconds: float = DB_TIMEOUT_SECONDS) -> None:
        self._session_factory = session_factory
        self._timeout_seconds = timeout_seconds

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        """A session that is always closed, which rolls back anything not committed."""
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session_factory() as session:
                    yield session
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            raise DatabaseError(_ERROR) from exc

    async def latest_answer(self, user_id: uuid.UUID) -> AnsweredBy | None:
        """The provider and model of this member's latest finished reply, which is almost always
        the one being reported. It can differ from the configured one after a fallback. Uses the
        call log's (user_id, created_at) index."""
        statement = (
            select(AssistantCallLog.provider, AssistantCallLog.model)
            .where(AssistantCallLog.user_id == user_id, AssistantCallLog.outcome == "completed")
            .order_by(AssistantCallLog.created_at.desc())
            .limit(1)
        )
        async with self._session() as session:
            row = (await session.execute(statement)).one_or_none()
            return None if row is None else AnsweredBy(row.provider, row.model)

    async def create(self, report: NewAssistantReport) -> uuid.UUID:
        row = AssistantReport(
            user_id=report.user_id,
            reason=report.reason,
            details=report.details,
            reply_text=report.reply_text,
            prompt_text=report.prompt_text,
            client_message_id=report.client_message_id,
            provider_name=report.provider_name,
            model=report.model,
        )
        async with self._session() as session:
            session.add(row)
            await session.commit()
            return row.id

    async def list_by_status(self, status: str, limit: int) -> list[SavedAssistantReport]:
        statement = (
            _with_reporter().where(AssistantReport.status == status).order_by(AssistantReport.created_at.desc()).limit(limit)
        )
        async with self._session() as session:
            return [_saved(report, email) for report, email in (await session.execute(statement)).all()]

    async def resolve(self, report_id: uuid.UUID, admin_id: uuid.UUID) -> SavedAssistantReport | None:
        """Resolving twice keeps the first resolution, so who resolved it and when stay true."""
        mark = (
            update(AssistantReport)
            .where(AssistantReport.id == report_id, AssistantReport.status == "open")
            .values(status="resolved", resolved_at=func.now(), resolved_by=admin_id)
        )
        async with self._session() as session:
            await session.execute(mark)
            await session.commit()
            row = (await session.execute(_with_reporter().where(AssistantReport.id == report_id))).one_or_none()
            return None if row is None else _saved(row[0], row[1])
