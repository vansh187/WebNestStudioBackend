"""The attempt cache (plan section 6.6): a per-process dictionary in front of
the exam_attempt_cache table. Reads try the dictionary first and refill it
from the table on a miss (restart, deploy, another worker); writes go to
both; deleting at grading removes both. Services always read the attempt row
first and consult the cache only for an active attempt, so a stale entry in
another worker's dictionary is never served."""

import uuid
from collections import OrderedDict
from datetime import datetime

from sqlalchemy import delete, exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from exams.infra.sql_base import SqlStore
from exams.models import ExamAttempt, ExamAttemptCache


class MemoryAttemptCache:
    """Bounded: at most `max_entries` payloads (~30 KB each), oldest evicted."""

    def __init__(self, max_entries: int = 500) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[uuid.UUID, uuid.UUID], tuple[list[dict], datetime]] = OrderedDict()

    def get(self, attempt_id: uuid.UUID, user_id: uuid.UUID, now: datetime) -> list[dict] | None:
        key = (user_id, attempt_id)
        entry = self._entries.get(key)
        if entry is None:
            return None
        payload, expires_at = entry
        if expires_at <= now:
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return payload

    def put(self, attempt_id: uuid.UUID, user_id: uuid.UUID, payload: list[dict], expires_at: datetime) -> None:
        key = (user_id, attempt_id)
        self._entries[key] = (payload, expires_at)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def forget(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> None:
        self._entries.pop((user_id, attempt_id), None)

    def sweep(self, now: datetime) -> int:
        stale = [key for key, (_, expires_at) in self._entries.items() if expires_at <= now]
        for key in stale:
            self._entries.pop(key, None)
        return len(stale)

    def __len__(self) -> int:
        return len(self._entries)


class SqlAttemptCache(SqlStore):
    async def get(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> tuple[list[dict], datetime] | None:
        result = await self._execute(
            select(ExamAttemptCache.payload, ExamAttemptCache.expires_at).where(
                ExamAttemptCache.attempt_id == attempt_id, ExamAttemptCache.user_id == user_id
            ),
            "read attempt cache",
        )
        row = result.first()
        if row is None or not isinstance(row[0], list):
            return None
        return row[0], row[1]

    async def exists(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        result = await self._execute(
            select(
                exists().where(ExamAttemptCache.attempt_id == attempt_id, ExamAttemptCache.user_id == user_id)
            ),
            "check attempt cache",
        )
        return bool(result.scalar())

    async def put(self, attempt_id: uuid.UUID, user_id: uuid.UUID, payload: list[dict], expires_at: datetime) -> None:
        statement = pg_insert(ExamAttemptCache).values(
            attempt_id=attempt_id, user_id=user_id, payload=payload, expires_at=expires_at
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ExamAttemptCache.attempt_id],
            set_={"payload": statement.excluded.payload, "expires_at": statement.excluded.expires_at},
        )
        await self._execute(statement, "write attempt cache")

    async def delete(self, attempt_id: uuid.UUID) -> None:
        await self._execute(
            delete(ExamAttemptCache).where(ExamAttemptCache.attempt_id == attempt_id), "delete attempt cache"
        )

    async def sweep(self, now: datetime) -> int:
        """Deletes rows past their expiry whose attempt is no longer active. A
        cache row of an overdue but still active attempt is kept, so lazy
        grading after a long sleep can still grade it."""
        still_active = exists().where(
            ExamAttempt.id == ExamAttemptCache.attempt_id, ExamAttempt.status == "active"
        )
        result = await self._execute(
            delete(ExamAttemptCache)
            .where(ExamAttemptCache.expires_at < now, ~still_active)
            .returning(ExamAttemptCache.attempt_id),
            "sweep attempt cache",
        )
        return len(result.all())


class LayeredAttemptCache:
    def __init__(self, memory: MemoryAttemptCache, table: SqlAttemptCache, clock) -> None:
        self._memory = memory
        self._table = table
        self._clock = clock

    async def get(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> list[dict] | None:
        payload = self._memory.get(attempt_id, user_id, self._clock.now())
        if payload is not None:
            return payload
        row = await self._table.get(attempt_id, user_id)
        if row is None:
            return None
        payload, expires_at = row
        self._memory.put(attempt_id, user_id, payload, expires_at)
        return payload

    async def exists(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        if self._memory.get(attempt_id, user_id, self._clock.now()) is not None:
            return True
        return await self._table.exists(attempt_id, user_id)

    async def put(self, attempt_id: uuid.UUID, user_id: uuid.UUID, payload: list[dict], expires_at: datetime) -> None:
        await self._table.put(attempt_id, user_id, payload, expires_at)
        self._memory.put(attempt_id, user_id, payload, expires_at)

    async def delete(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> None:
        # Memory first: if the transaction then fails, the next read simply
        # refills from the table, which still has the row.
        self._memory.forget(attempt_id, user_id)
        await self._table.delete(attempt_id)

    async def sweep(self, now: datetime) -> int:
        return self._memory.sweep(now) + await self._table.sweep(now)
