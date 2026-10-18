"""Shared SQL plumbing for the exam stores: every driver error becomes an
ExamStoreUnavailable (503) with a log line, never a raw exception."""

import logging

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from exams.errors import ExamStoreUnavailable

logger = logging.getLogger("webnest.exams.store")


def constraint_name(exc: IntegrityError) -> str:
    """Name of the violated constraint/index (asyncpg exposes it on the cause)."""
    orig = getattr(exc, "orig", None)
    for candidate in (getattr(orig, "__cause__", None), orig):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return str(orig or exc)


class SqlStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _execute(self, statement, context: str):
        try:
            return await self._session.execute(statement)
        except SQLAlchemyError as exc:
            logger.error("Exam store operation failed: %s", context, exc_info=True)
            raise ExamStoreUnavailable() from exc


class SqlUnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def commit(self) -> None:
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            logger.error("Exam transaction commit failed", exc_info=True)
            await self.rollback()
            raise ExamStoreUnavailable() from exc

    async def rollback(self) -> None:
        try:
            await self._session.rollback()
        except SQLAlchemyError:
            logger.warning("Exam transaction rollback failed", exc_info=True)

    def savepoint(self):
        return self._session.begin_nested()
