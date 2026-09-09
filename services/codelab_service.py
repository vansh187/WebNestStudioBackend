import base64
import binascii
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import BadRequestError, NotFoundError
from database.codelab_persistence import CodelabPersistence
from database.models import CodelabProblem, User
from schemas.codelab_schemas import (
    AchievementItem,
    AdminProblemResponse,
    AdminProblemUpsertRequest,
    CodelabDashboardResponse,
    ContinueLearning,
    DashboardSummary,
    DifficultyProgress,
    ProblemDetailResponse,
    ProblemListItem,
    ProblemListResponse,
    PublicTest,
    RecentActivityItem,
    SubmissionCreateRequest,
    SubmissionCreateResponse,
    SubmissionHistoryItem,
    SubmissionHistoryResponse,
    SubmissionProblemRef,
    TrackItem,
    TrackListResponse,
    TrackProgress,
    UserProblemProgress,
)

# result.status -> the coarse status the client shows next to the attempt.
_SUBMISSION_STATUS = {
    "passed": "solved",
    "failed": "attempted",
    "runtime_error": "attempted",
    "timeout": "attempted",
    "manual_review": "manual_review",
}


def _encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | tuple[None, None]:
    if not cursor:
        return None, None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        iso, row_id = raw.split("|", 1)
        return datetime.fromisoformat(iso), uuid.UUID(row_id)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise BadRequestError("Invalid pagination cursor") from exc


class CodelabService:
    """Business logic for the CodeLab coding-practice platform. Visitor code is
    executed in the browser; submissions carry a client-reported result that is
    persisted for audit and used to advance gamification state."""

    def __init__(self, session: AsyncSession) -> None:
        self._codelab = CodelabPersistence(session)

    # ------------------------------------------------------------------ #
    # Tracks
    # ------------------------------------------------------------------ #
    async def list_tracks(self) -> TrackListResponse:
        await self._codelab.ensure_default_tracks()
        tracks = await self._codelab.list_tracks()
        return TrackListResponse(
            items=[
                TrackItem(id=t.id, label=t.label, runner=t.runner, description=t.description)
                for t in tracks
            ]
        )

    # ------------------------------------------------------------------ #
    # Problems
    # ------------------------------------------------------------------ #
    async def list_problems(
        self,
        user: User | None,
        *,
        track: str | None,
        language: str | None,
        topic: str | None,
        difficulty: str | None,
        limit: int,
        cursor: str | None,
    ) -> ProblemListResponse:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        rows = await self._codelab.list_published_problems(
            track, language, topic, difficulty, limit, cursor_created_at, cursor_id
        )
        has_more = len(rows) > limit
        rows = rows[:limit]

        solved_counts = await self._codelab.solved_counts_for([p.id for p in rows])
        progress_map = (
            await self._codelab.get_progress_map(user.id, [p.id for p in rows]) if user else {}
        )

        items = [
            ProblemListItem(
                id=p.id,
                slug=p.slug,
                title=p.title,
                track=p.track,
                language=p.language,
                difficulty=p.difficulty,
                topics=list(p.topics or []),
                points=p.points,
                estimated_minutes=p.estimated_minutes,
                status=(progress_map[p.id].status if p.id in progress_map else "not_started"),
                solved_count=solved_counts.get(p.id, 0),
            )
            for p in rows
        ]
        next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
        return ProblemListResponse(items=items, next_cursor=next_cursor)

    async def get_problem(self, user: User | None, identifier: str) -> ProblemDetailResponse:
        problem = await self._codelab.get_problem(identifier)
        if problem is None:
            raise NotFoundError("Problem not found")

        public_tests = [
            PublicTest(
                id=str(tc.id),
                input=tc.input,
                expected_output=tc.expected_output,
                weight=tc.weight,
            )
            for tc in problem.test_cases
            if not tc.is_hidden
        ]

        user_progress = None
        if user is not None:
            progress = await self._codelab.get_progress(user.id, problem.id)
            if progress is not None:
                user_progress = UserProblemProgress(
                    status=progress.status,
                    best_score=progress.best_score,
                    attempts=progress.attempts,
                    last_activity_at=progress.last_activity_at,
                )

        return ProblemDetailResponse(
            id=problem.id,
            slug=problem.slug,
            title=problem.title,
            track=problem.track,
            language=problem.language,
            difficulty=problem.difficulty,
            points=problem.points,
            estimated_minutes=problem.estimated_minutes,
            topics=list(problem.topics or []),
            statement=problem.statement,
            constraints=list(problem.constraints or []),
            hints=list(problem.hints or []),
            starter_files=list(problem.starter_files or []),
            examples=list(problem.examples or []),
            public_tests=public_tests,
            user_progress=user_progress,
        )

    # ------------------------------------------------------------------ #
    # Submissions
    # ------------------------------------------------------------------ #
    async def create_submission(
        self, user: User, payload: SubmissionCreateRequest
    ) -> SubmissionCreateResponse:
        problem = await self._codelab.get_problem(payload.problem_id)
        if problem is None:
            raise NotFoundError("Problem not found")

        result = payload.result
        is_solve = result.status == "passed"
        coarse_status = _SUBMISSION_STATUS.get(result.status, "attempted")

        outcome = await self._codelab.apply_submission(
            user_id=user.id,
            problem=problem,
            language=payload.language,
            files=[f.model_dump() for f in payload.files],
            result_blob=result.model_dump(),
            raw_status=result.status,
            score=result.score,
            passed_tests=result.passed_tests,
            total_tests=result.total_tests,
            runtime_ms=result.runtime_ms,
            is_solve=is_solve,
            xp_for_solve=problem.points,
            today=datetime.now(timezone.utc).date(),
        )
        submission = outcome["submission"]
        return SubmissionCreateResponse(
            id=submission.id,
            problem_id=problem.id,
            status=coarse_status,
            score=submission.score,
            passed_tests=submission.passed_tests,
            total_tests=submission.total_tests,
            xp_awarded=outcome["xp_awarded"],
            best_score=outcome["best_score"],
            attempts=outcome["attempts"],
            submitted_at=submission.submitted_at,
        )

    async def list_submissions(
        self, user: User, *, problem_id: str | None, limit: int, cursor: str | None
    ) -> SubmissionHistoryResponse:
        resolved_problem_id: uuid.UUID | None = None
        if problem_id:
            problem = await self._codelab.get_problem(problem_id)
            if problem is None:
                raise NotFoundError("Problem not found")
            resolved_problem_id = problem.id

        cursor_submitted_at, cursor_id = _decode_cursor(cursor)
        rows = await self._codelab.list_user_submissions(
            user.id, resolved_problem_id, limit, cursor_submitted_at, cursor_id
        )
        has_more = len(rows) > limit
        rows = rows[:limit]

        items = [
            SubmissionHistoryItem(
                id=s.id,
                problem=SubmissionProblemRef(id=s.problem.id, slug=s.problem.slug, title=s.problem.title),
                language=s.language,
                status=s.status,
                score=s.score,
                passed_tests=s.passed_tests,
                total_tests=s.total_tests,
                submitted_at=s.submitted_at,
            )
            for s in rows
        ]
        next_cursor = (
            _encode_cursor(rows[-1].submitted_at, rows[-1].id) if has_more and rows else None
        )
        return SubmissionHistoryResponse(items=items, next_cursor=next_cursor)

    # ------------------------------------------------------------------ #
    # Dashboard
    # ------------------------------------------------------------------ #
    async def get_dashboard(self, user: User) -> CodelabDashboardResponse:
        await self._codelab.ensure_default_tracks()
        tracks = await self._codelab.list_tracks()
        total_problems = await self._codelab.count_published_problems()
        totals_by_track = await self._codelab.count_published_by(CodelabProblem.track)
        totals_by_difficulty = await self._codelab.count_published_by(CodelabProblem.difficulty)

        attempted, solved = await self._codelab.progress_counts(user.id)
        solved_by_track = await self._codelab.solved_by_track(user.id)
        solved_by_difficulty = await self._codelab.solved_by_difficulty(user.id)
        stats = await self._codelab.get_stats(user.id)
        achievements = await self._codelab.list_achievements(user.id)
        recent = await self._codelab.recent_submissions(user.id, 10)
        continue_problem = await self._codelab.pick_continue_problem(user.id)

        completion_percent = int(round(solved / total_problems * 100)) if total_problems else 0

        summary = DashboardSummary(
            problems_total=total_problems,
            attempted=attempted,
            solved=solved,
            completion_percent=completion_percent,
            xp=(stats.xp if stats else 0),
            current_streak=(stats.current_streak if stats else 0),
            best_streak=(stats.best_streak if stats else 0),
        )

        track_progress = [
            TrackProgress(
                track=t.id,
                label=t.label,
                solved=solved_by_track.get(t.id, 0),
                total=totals_by_track.get(t.id, 0),
                completion_percent=(
                    int(round(solved_by_track.get(t.id, 0) / totals_by_track[t.id] * 100))
                    if totals_by_track.get(t.id)
                    else 0
                ),
            )
            for t in tracks
        ]

        difficulty_progress = [
            DifficultyProgress(
                difficulty=level,
                solved=solved_by_difficulty.get(level, 0),
                total=totals_by_difficulty.get(level, 0),
            )
            for level in ("easy", "medium", "hard")
        ]

        continue_learning = None
        if continue_problem is not None:
            continue_learning = ContinueLearning(
                slug=continue_problem.slug,
                title=continue_problem.title,
                track=continue_problem.track,
                difficulty=continue_problem.difficulty,
            )

        recent_activity = [
            RecentActivityItem(
                title=s.problem.title,
                track=s.problem.track,
                status=s.status,
                score=s.score,
                xp=(s.problem.points if s.status == "passed" else 0),
                created_at=s.submitted_at,
            )
            for s in recent
        ]

        return CodelabDashboardResponse(
            summary=summary,
            track_progress=track_progress,
            difficulty_progress=difficulty_progress,
            continue_learning=continue_learning,
            recent_activity=recent_activity,
            achievements=[
                AchievementItem(id=a.achievement_key, label=a.label, earned_at=a.earned_at)
                for a in achievements
            ],
        )

    # ------------------------------------------------------------------ #
    # Admin
    # ------------------------------------------------------------------ #
    async def upsert_problem(self, payload: AdminProblemUpsertRequest) -> AdminProblemResponse:
        fields = {
            "slug": payload.slug,
            "title": payload.title,
            "track": payload.track,
            "language": payload.language,
            "difficulty": payload.difficulty,
            "points": payload.points,
            "estimated_minutes": payload.estimated_minutes,
            "status": payload.status,
            "topics": payload.topics,
            "statement": payload.statement,
            "constraints": payload.constraints,
            "hints": payload.hints,
            "starter_files": [f.model_dump() for f in payload.starter_files],
            "examples": [e.model_dump() for e in payload.examples],
        }
        test_cases = [tc.model_dump() for tc in payload.test_cases]

        existing = await self._codelab.get_problem(payload.slug, include_unpublished=True)
        if existing is None:
            problem = await self._codelab.create_problem(fields, test_cases)
        else:
            problem = await self._codelab.update_problem(existing, fields, test_cases)
        return self._to_admin_problem(problem)

    @staticmethod
    def _to_admin_problem(problem: CodelabProblem) -> AdminProblemResponse:
        return AdminProblemResponse(
            id=problem.id,
            slug=problem.slug,
            title=problem.title,
            track=problem.track,
            language=problem.language,
            difficulty=problem.difficulty,
            points=problem.points,
            estimated_minutes=problem.estimated_minutes,
            status=problem.status,
            topics=list(problem.topics or []),
            statement=problem.statement,
            constraints=list(problem.constraints or []),
            hints=list(problem.hints or []),
            starter_files=list(problem.starter_files or []),
            examples=list(problem.examples or []),
            public_test_count=sum(1 for tc in problem.test_cases if not tc.is_hidden),
            hidden_test_count=sum(1 for tc in problem.test_cases if tc.is_hidden),
            created_at=problem.created_at,
            updated_at=problem.updated_at,
        )
