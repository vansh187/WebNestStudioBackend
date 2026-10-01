from fastapi import APIRouter, BackgroundTasks, Depends, Query, status

from core.dependencies import (
    container,
    get_codelab_generation_service,
    get_codelab_service,
    get_current_user,
    get_optional_current_user,
    require_admin,
)
from database.models import User
from schemas.codelab_schemas import (
    AdminProblemResponse,
    AdminProblemUpsertRequest,
    CodelabDashboardResponse,
    CodelabGenerationLogResponse,
    CodelabGenerationTriggerRequest,
    CodelabGenerationTriggerResponse,
    ProblemDetailResponse,
    ProblemListResponse,
    SubmissionCreateRequest,
    SubmissionCreateResponse,
    SubmissionHistoryResponse,
    TrackListResponse,
)
from services.codelab_generation_service import CodelabGenerationService
from services.codelab_scheduler import run_manual_generation
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


@admin_router.post("/generate", response_model=CodelabGenerationTriggerResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_problem_generation(
    background_tasks: BackgroundTasks,
    payload: CodelabGenerationTriggerRequest = CodelabGenerationTriggerRequest(),
) -> CodelabGenerationTriggerResponse:
    """Starts automated problem generation now (one problem per requested
    difficulty, all three by default), bypassing the schedule. Generating and
    verifying a set takes a minute or more, so it runs in the background -
    poll GET /generation-logs for the outcome of each difficulty."""
    background_tasks.add_task(run_manual_generation, container.database, container.settings, payload.difficulties)
    return CodelabGenerationTriggerResponse(
        started=True, detail="Generation started. Check /api/admin/codelab/generation-logs for the result."
    )


@admin_router.get("/generation-logs", response_model=list[CodelabGenerationLogResponse])
async def list_generation_logs(
    limit: int = Query(default=30, ge=1, le=200),
    generation: CodelabGenerationService = Depends(get_codelab_generation_service),
) -> list:
    """Recent generation attempts. A successful row's problem_slug identifies
    an auto-generated problem; a failed row's error_message says why the
    draft was rejected."""
    return await generation.list_recent_logs(limit=limit)
