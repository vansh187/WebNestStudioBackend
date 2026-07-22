import logging

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.engine import Result
from sqlalchemy.sql import Executable

from core.exceptions import ConflictError, DatabaseError

logger = logging.getLogger("webnest.database")


class BasePersistence:
    """Shared DB-error handling for every *_persistence.py class.

    Every commit/query goes through here so a dropped connection, timeout, or
    constraint violation always becomes a clear DomainError instead of a raw
    driver exception leaking up to the router.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _execute(self, statement: Executable) -> Result:
        try:
            return await self._session.execute(statement)
        except SQLAlchemyError as exc:
            logger.error("Database read failed", exc_info=True)
            raise DatabaseError("Could not read data from the database right now. Please try again shortly.") from exc

    async def _commit(self, conflict_message: str = "A record with these details already exists") -> None:
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            logger.warning("Integrity constraint violated on commit: %s", exc)
            raise ConflictError(conflict_message) from exc
        except SQLAlchemyError as exc:
            await self._session.rollback()
            logger.error("Database write failed", exc_info=True)
            raise DatabaseError("Could not save data to the database right now. Please try again shortly.") from exc

    async def _refresh(self, instance) -> None:
        try:
            await self._session.refresh(instance)
        except SQLAlchemyError as exc:
            logger.error("Database refresh failed", exc_info=True)
            raise DatabaseError("Saved, but could not reload the latest data. Please retry your request.") from exc

    async def _delete(self, instance) -> None:
        try:
            await self._session.delete(instance)
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            logger.error("Database delete failed", exc_info=True)
            raise DatabaseError("Could not delete this record right now. Please try again shortly.") from exc
