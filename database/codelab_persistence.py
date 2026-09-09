import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Select, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.base_persistence import BasePersistence
from database.models import (
    CodelabAchievement,
    CodelabProblem,
    CodelabProblemProgress,
    CodelabSubmission,
    CodelabTrack,
    CodelabUserStats,
)

_DEFAULT_TRACKS = (
    ("python", "Python", "pyodide", "Python practice with browser-side execution.", 1),
    ("web", "Web Development", "iframe", "HTML, CSS, and JavaScript challenges.", 2),
)


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


class CodelabPersistence(BasePersistence):
    """All DB access for the CodeLab learning platform (tracks, problems, test
    cases, submissions, per-user progress, stats, achievements). Every call
    goes through BasePersistence so only DomainErrors ever surface."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    # ------------------------------------------------------------------ #
    # Tracks
    # ------------------------------------------------------------------ #
    async def ensure_default_tracks(self) -> None:
        """Seed the two Phase 1 tracks once, idempotently, so the catalog is
        never empty on a fresh deploy."""
        existing = await self._execute(select(func.count(CodelabTrack.id)))
        if (existing.scalar_one() or 0) > 0:
            return
        rows = [
            {"id": tid, "label": label, "runner": runner, "description": desc, "display_order": order}
            for tid, label, runner, desc, order in _DEFAULT_TRACKS
        ]
        await self._execute(
            pg_insert(CodelabTrack).values(rows).on_conflict_do_nothing(index_elements=[CodelabTrack.id])
        )
        await self._commit()

    async def list_tracks(self, include_unpublished: bool = False) -> list[CodelabTrack]:
        stmt = select(CodelabTrack).order_by(CodelabTrack.display_order, CodelabTrack.id)
        if not include_unpublished:
            stmt = stmt.where(CodelabTrack.status == "published")
        result = await self._execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------ #
    # Problems - read
    # ------------------------------------------------------------------ #
    def _problem_filters(
        self,
        stmt: Select,
        track: str | None,
        language: str | None,
        topic: str | None,
        difficulty: str | None,
    ) -> Select:
        if track:
            stmt = stmt.where(CodelabProblem.track == track)
        if language:
            stmt = stmt.where(CodelabProblem.language == language)
        if difficulty:
            stmt = stmt.where(CodelabProblem.difficulty == difficulty)
        if topic:
            stmt = stmt.where(CodelabProblem.topics.any(topic))
        return stmt

    async def list_published_problems(
        self,
        track: str | None,
        language: str | None,
        topic: str | None,
        difficulty: str | None,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_id: uuid.UUID | None,
    ) -> list[CodelabProblem]:
        limit = min(max(limit, 1), 100)
        stmt = select(CodelabProblem).where(CodelabProblem.status == "published")
        stmt = self._problem_filters(stmt, track, language, topic, difficulty)
        if cursor_created_at is not None and cursor_id is not None:
            stmt = stmt.where(
                tuple_(CodelabProblem.created_at, CodelabProblem.id)
                < tuple_(cursor_created_at, cursor_id)
            )
        stmt = stmt.order_by(CodelabProblem.created_at.desc(), CodelabProblem.id.desc()).limit(limit + 1)
        result = await self._execute(stmt)
        return list(result.scalars().all())

    async def get_problem(
        self, identifier: str | uuid.UUID, include_unpublished: bool = False
    ) -> CodelabProblem | None:
        as_uuid = _as_uuid(identifier)
        match = CodelabProblem.slug == str(identifier)
        if as_uuid is not None:
            match = or_(match, CodelabProblem.id == as_uuid)
        stmt = (
            select(CodelabProblem)
            .where(match)
            .options(selectinload(CodelabProblem.test_cases))
        )
        if not include_unpublished:
            stmt = stmt.where(CodelabProblem.status == "published")
        result = await self._execute(stmt.limit(1))
        return result.scalars().first()

    async def count_published_problems(self) -> int:
        result = await self._execute(
            select(func.count(CodelabProblem.id)).where(CodelabProblem.status == "published")
        )
        return int(result.scalar_one() or 0)

    async def count_published_by(self, column) -> dict[str, int]:
        result = await self._execute(
            select(column, func.count(CodelabProblem.id))
            .where(CodelabProblem.status == "published")
            .group_by(column)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    async def solved_counts_for(self, problem_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not problem_ids:
            return {}
        result = await self._execute(
            select(CodelabProblemProgress.problem_id, func.count(CodelabProblemProgress.id))
            .where(
                CodelabProblemProgress.problem_id.in_(problem_ids),
                CodelabProblemProgress.status == "solved",
            )
            .group_by(CodelabProblemProgress.problem_id)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    # ------------------------------------------------------------------ #
    # Problems - admin write
    # ------------------------------------------------------------------ #
    async def create_problem(self, fields: dict, test_cases: list[dict]) -> CodelabProblem:
        problem = CodelabProblem(**fields)
        for index, tc in enumerate(test_cases):
            problem.test_cases.append(
                _new_test_case(tc, index)
            )
        self._session.add(problem)
        await self._commit(conflict_message="A problem with this slug already exists")
        reloaded = await self.get_problem(problem.id, include_unpublished=True)
        return reloaded if reloaded is not None else problem

    async def update_problem(
        self, problem: CodelabProblem, fields: dict, test_cases: list[dict] | None
    ) -> CodelabProblem:
        for key, value in fields.items():
            setattr(problem, key, value)
        if test_cases is not None:
            problem.test_cases.clear()
            for index, tc in enumerate(test_cases):
                problem.test_cases.append(_new_test_case(tc, index))
        await self._commit(conflict_message="A problem with this slug already exists")
        reloaded = await self.get_problem(problem.id, include_unpublished=True)
        return reloaded if reloaded is not None else problem

    # ------------------------------------------------------------------ #
    # Submissions
    # ------------------------------------------------------------------ #
    async def list_user_submissions(
        self,
        user_id: uuid.UUID,
        problem_id: uuid.UUID | None,
        limit: int,
        cursor_submitted_at: datetime | None,
        cursor_id: uuid.UUID | None,
    ) -> list[CodelabSubmission]:
        limit = min(max(limit, 1), 100)
        stmt = (
            select(CodelabSubmission)
            .where(CodelabSubmission.user_id == user_id)
            .options(selectinload(CodelabSubmission.problem))
        )
        if problem_id is not None:
            stmt = stmt.where(CodelabSubmission.problem_id == problem_id)
        if cursor_submitted_at is not None and cursor_id is not None:
            stmt = stmt.where(
                tuple_(CodelabSubmission.submitted_at, CodelabSubmission.id)
                < tuple_(cursor_submitted_at, cursor_id)
            )
        stmt = stmt.order_by(
            CodelabSubmission.submitted_at.desc(), CodelabSubmission.id.desc()
        ).limit(limit + 1)
        result = await self._execute(stmt)
        return list(result.scalars().all())

    async def recent_submissions(self, user_id: uuid.UUID, limit: int) -> list[CodelabSubmission]:
        result = await self._execute(
            select(CodelabSubmission)
            .where(CodelabSubmission.user_id == user_id)
            .options(selectinload(CodelabSubmission.problem))
            .order_by(CodelabSubmission.submitted_at.desc(), CodelabSubmission.id.desc())
            .limit(max(1, min(limit, 50)))
        )
        return list(result.scalars().all())

    async def apply_submission(
        self,
        *,
        user_id: uuid.UUID,
        problem: CodelabProblem,
        language: str,
        files: list[dict],
        result_blob: dict,
        raw_status: str,
        score: int,
        passed_tests: int,
        total_tests: int,
        runtime_ms: int | None,
        is_solve: bool,
        xp_for_solve: int,
        today: date,
    ) -> dict:
        """The whole read-modify-write for one submission in a single
        transaction: store the attempt, upsert per-user progress under a row
        lock (so concurrent submissions can't double-award), roll the streak,
        and award first-solve XP exactly once. Returns a plain dict."""
        submission = CodelabSubmission(
            user_id=user_id,
            problem_id=problem.id,
            language=language,
            files=files,
            result=result_blob,
            status=raw_status,
            score=max(0, min(score, 100)),
            passed_tests=max(0, passed_tests),
            total_tests=max(0, total_tests),
            runtime_ms=runtime_ms if (runtime_ms is None or runtime_ms >= 0) else None,
        )
        self._session.add(submission)

        progress = await self._lock_or_create_progress(user_id, problem.id)
        was_solved = progress.status == "solved"
        newly_solved = is_solve and not was_solved

        progress.attempts = (progress.attempts or 0) + 1
        progress.best_score = max(progress.best_score or 0, submission.score)
        progress.last_activity_at = datetime.now(timezone.utc)
        if is_solve:
            progress.status = "solved"
            if progress.solved_at is None:
                progress.solved_at = datetime.now(timezone.utc)
        elif progress.status != "solved":
            progress.status = "in_progress"

        stats = await self._lock_or_create_stats(user_id)
        self._roll_streak(stats, today)
        xp_awarded = 0
        if newly_solved:
            stats.xp = (stats.xp or 0) + max(0, xp_for_solve)
            stats.solved_count = (stats.solved_count or 0) + 1
            xp_awarded = max(0, xp_for_solve)

        await self._commit(conflict_message="Submission conflicted with a concurrent update")
        await self._refresh(submission)

        first_solve = newly_solved and (stats.solved_count == 1)
        if first_solve:
            await self.award_achievement(user_id, "first_solve", "First Problem Solved")

        return {
            "submission": submission,
            "progress_status": progress.status,
            "best_score": progress.best_score,
            "attempts": progress.attempts,
            "xp_awarded": xp_awarded,
            "newly_solved": newly_solved,
        }

    # ------------------------------------------------------------------ #
    # Progress / stats
    # ------------------------------------------------------------------ #
    async def get_progress(
        self, user_id: uuid.UUID, problem_id: uuid.UUID
    ) -> CodelabProblemProgress | None:
        result = await self._execute(
            select(CodelabProblemProgress).where(
                CodelabProblemProgress.user_id == user_id,
                CodelabProblemProgress.problem_id == problem_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_progress_map(
        self, user_id: uuid.UUID, problem_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, CodelabProblemProgress]:
        if not problem_ids:
            return {}
        result = await self._execute(
            select(CodelabProblemProgress).where(
                CodelabProblemProgress.user_id == user_id,
                CodelabProblemProgress.problem_id.in_(problem_ids),
            )
        )
        return {row.problem_id: row for row in result.scalars().all()}

    async def _lock_or_create_progress(
        self, user_id: uuid.UUID, problem_id: uuid.UUID
    ) -> CodelabProblemProgress:
        # ON CONFLICT DO NOTHING guarantees the row exists without a rollback
        # that would also discard other pending inserts in this transaction;
        # the FOR UPDATE select then locks it for the read-modify-write.
        await self._execute(
            pg_insert(CodelabProblemProgress)
            .values(user_id=user_id, problem_id=problem_id, status="not_started")
            .on_conflict_do_nothing(
                index_elements=[CodelabProblemProgress.user_id, CodelabProblemProgress.problem_id]
            )
        )
        result = await self._execute(
            select(CodelabProblemProgress)
            .where(
                CodelabProblemProgress.user_id == user_id,
                CodelabProblemProgress.problem_id == problem_id,
            )
            .with_for_update()
        )
        return result.scalar_one()

    async def get_stats(self, user_id: uuid.UUID) -> CodelabUserStats | None:
        result = await self._execute(
            select(CodelabUserStats).where(CodelabUserStats.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def _lock_or_create_stats(self, user_id: uuid.UUID) -> CodelabUserStats:
        await self._execute(
            pg_insert(CodelabUserStats)
            .values(user_id=user_id)
            .on_conflict_do_nothing(index_elements=[CodelabUserStats.user_id])
        )
        result = await self._execute(
            select(CodelabUserStats).where(CodelabUserStats.user_id == user_id).with_for_update()
        )
        return result.scalar_one()

    async def touch_streak(self, user_id: uuid.UUID, today: date) -> CodelabUserStats:
        """Roll the daily streak for a non-problem qualifying activity (lesson
        completion, quiz submit). Commits."""
        stats = await self._lock_or_create_stats(user_id)
        self._roll_streak(stats, today)
        await self._commit()
        await self._refresh(stats)
        return stats

    async def add_xp(self, user_id: uuid.UUID, amount: int) -> CodelabUserStats:
        stats = await self._lock_or_create_stats(user_id)
        stats.xp = (stats.xp or 0) + max(0, amount)
        await self._commit()
        await self._refresh(stats)
        return stats

    def _roll_streak(self, stats: CodelabUserStats, today: date) -> None:
        last = stats.last_activity_date
        if last == today:
            return
        if last == today - timedelta(days=1):
            stats.current_streak = (stats.current_streak or 0) + 1
        else:
            stats.current_streak = 1
        stats.best_streak = max(stats.best_streak or 0, stats.current_streak)
        stats.last_activity_date = today

    # ------------------------------------------------------------------ #
    # Achievements
    # ------------------------------------------------------------------ #
    async def award_achievement(self, user_id: uuid.UUID, key: str, label: str) -> bool:
        stmt = (
            pg_insert(CodelabAchievement)
            .values(user_id=user_id, achievement_key=key, label=label)
            .on_conflict_do_nothing(index_elements=[CodelabAchievement.user_id, CodelabAchievement.achievement_key])
            .returning(CodelabAchievement.id)
        )
        result = await self._execute(stmt)
        await self._commit()
        return result.first() is not None

    async def list_achievements(self, user_id: uuid.UUID) -> list[CodelabAchievement]:
        result = await self._execute(
            select(CodelabAchievement)
            .where(CodelabAchievement.user_id == user_id)
            .order_by(CodelabAchievement.earned_at.asc())
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------ #
    # Dashboard aggregates
    # ------------------------------------------------------------------ #
    async def progress_counts(self, user_id: uuid.UUID) -> tuple[int, int]:
        result = await self._execute(
            select(
                func.count(CodelabProblemProgress.id).filter(CodelabProblemProgress.attempts > 0),
                func.count(CodelabProblemProgress.id).filter(CodelabProblemProgress.status == "solved"),
            ).where(CodelabProblemProgress.user_id == user_id)
        )
        row = result.one()
        return int(row[0] or 0), int(row[1] or 0)

    async def solved_by_track(self, user_id: uuid.UUID) -> dict[str, int]:
        result = await self._execute(
            select(CodelabProblem.track, func.count(CodelabProblemProgress.id))
            .join(CodelabProblem, CodelabProblem.id == CodelabProblemProgress.problem_id)
            .where(
                CodelabProblemProgress.user_id == user_id,
                CodelabProblemProgress.status == "solved",
                CodelabProblem.status == "published",
            )
            .group_by(CodelabProblem.track)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    async def solved_by_difficulty(self, user_id: uuid.UUID) -> dict[str, int]:
        result = await self._execute(
            select(CodelabProblem.difficulty, func.count(CodelabProblemProgress.id))
            .join(CodelabProblem, CodelabProblem.id == CodelabProblemProgress.problem_id)
            .where(
                CodelabProblemProgress.user_id == user_id,
                CodelabProblemProgress.status == "solved",
                CodelabProblem.status == "published",
            )
            .group_by(CodelabProblem.difficulty)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    async def pick_continue_problem(self, user_id: uuid.UUID) -> CodelabProblem | None:
        # Most recently touched, not-yet-solved, still-published problem.
        result = await self._execute(
            select(CodelabProblem)
            .join(CodelabProblemProgress, CodelabProblemProgress.problem_id == CodelabProblem.id)
            .where(
                CodelabProblemProgress.user_id == user_id,
                CodelabProblemProgress.status != "solved",
                CodelabProblem.status == "published",
            )
            .order_by(CodelabProblemProgress.last_activity_at.desc())
            .limit(1)
        )
        problem = result.scalars().first()
        if problem is not None:
            return problem
        # Fall back to any published problem the user has never touched.
        result = await self._execute(
            select(CodelabProblem)
            .where(
                CodelabProblem.status == "published",
                ~select(CodelabProblemProgress.id)
                .where(
                    CodelabProblemProgress.problem_id == CodelabProblem.id,
                    CodelabProblemProgress.user_id == user_id,
                )
                .exists(),
            )
            .order_by(CodelabProblem.difficulty.asc(), CodelabProblem.created_at.asc())
            .limit(1)
        )
        return result.scalars().first()


def _new_test_case(tc: dict, index: int):
    from database.models import CodelabTestCase

    return CodelabTestCase(
        input=tc.get("input", "") or "",
        expected_output=tc.get("expected_output", "") or "",
        is_hidden=bool(tc.get("is_hidden", False)),
        weight=int(tc.get("weight", 1) or 1),
        display_order=int(tc.get("display_order", index)),
    )
