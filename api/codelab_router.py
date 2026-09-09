from fastapi import APIRouter, Depends, Query, status

from core.dependencies import get_codelab_service, get_current_user, get_optional_current_user, require_admin
from database.models import User
from schemas.codelab_schemas import (
    AdminProblemResponse,
    AdminProblemUpsertRequest,
    CodelabDashboardResponse,
    ProblemDetailResponse,
    ProblemListResponse,
    SubmissionCreateRequest,
    SubmissionCreateResponse,
    SubmissionHistoryResponse,
    TrackListResponse,
)
from services.codelab_service import CodelabService

router = APIRouter(prefix="/api/codelab", tags=["codelab"])
admin_router = APIRouter(prefix="/api/admin/codelab", tags=["codelab-admin"], dependencies=[Depends(require_admin)])


@router.get("/tracks", response_model=TrackListResponse)
async def list_tracks(
    codelab: CodelabService = Depends(get_codelab_service),
) -> TrackListResponse:
    return await codelab.list_tracks()


@router.get("/problems", response_model=ProblemListResponse)
async def list_problems(
    track: str | None = Query(default=None, max_length=60),
    language: str | None = Query(default=None, max_length=40),
    topic: str | None = Query(default=None, max_length=60),
    difficulty: str | None = Query(default=None, pattern="^(easy|medium|hard)$"),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    current_user: User | None = Depends(get_optional_current_user),
    codelab: CodelabService = Depends(get_codelab_service),
) -> ProblemListResponse:
    return await codelab.list_problems(
        current_user,
        track=track,
        language=language,
        topic=topic,
        difficulty=difficulty,
        limit=limit,
        cursor=cursor,
    )


@router.get("/problems/{identifier}", response_model=ProblemDetailResponse)
async def get_problem(
    identifier: str,
    current_user: User | None = Depends(get_optional_current_user),
    codelab: CodelabService = Depends(get_codelab_service),
) -> ProblemDetailResponse:
    return await codelab.get_problem(current_user, identifier)


@router.get("/submissions", response_model=SubmissionHistoryResponse)
async def list_submissions(
    problem_id: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    codelab: CodelabService = Depends(get_codelab_service),
) -> SubmissionHistoryResponse:
    return await codelab.list_submissions(
        current_user, problem_id=problem_id, limit=limit, cursor=cursor
    )


@router.post(
    "/submissions", response_model=SubmissionCreateResponse, status_code=status.HTTP_201_CREATED
)
async def create_submission(
    payload: SubmissionCreateRequest,
    current_user: User = Depends(get_current_user),
    codelab: CodelabService = Depends(get_codelab_service),
) -> SubmissionCreateResponse:
    return await codelab.create_submission(current_user, payload)


@router.get("/dashboard", response_model=CodelabDashboardResponse)
async def get_dashboard(
    current_user: User = Depends(get_current_user),
    codelab: CodelabService = Depends(get_codelab_service),
) -> CodelabDashboardResponse:
    return await codelab.get_dashboard(current_user)


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
@admin_router.put("/problems", response_model=AdminProblemResponse)
async def upsert_problem(
    payload: AdminProblemUpsertRequest,
    codelab: CodelabService = Depends(get_codelab_service),
) -> AdminProblemResponse:
    """Create or replace a problem, keyed by slug. Replaces its full test-case set."""
    return await codelab.upsert_problem(payload)
