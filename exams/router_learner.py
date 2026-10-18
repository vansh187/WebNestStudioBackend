"""Learner exam endpoints (contract sections 5 and 10, endpoints #4-#11)."""

import uuid

from fastapi import APIRouter, Depends, Response, status

from core.dependencies import get_current_user
from database.models import User
from exams.routing import ExamContractRoute
from exams.dependencies import get_attempt_service, get_certificate_service, get_eligibility_service, get_optional_user_strict
from exams.schemas import (
    ActiveAttemptResponse,
    ExamViewResponse,
    GradedAttemptResponse,
    MyCertificate,
    MyCertificatesResponse,
    PingResponse,
    ReportRequest,
    ReportResponse,
    ResultResponse,
    SaveAnswersRequest,
    SaveAnswersResponse,
    StartAttemptRequest,
)
from exams.services.attempt_service import ActiveAttemptView, AttemptService
from exams.services.certificate_service import CertificateService
from exams.services.eligibility_service import EligibilityService

router = APIRouter(tags=["exams"], route_class=ExamContractRoute)

NO_STORE = "no-store"


def _active(view: ActiveAttemptView) -> ActiveAttemptResponse:
    return ActiveAttemptResponse(
        attempt_id=view.attempt_id,
        status=view.status,
        started_at=view.started_at,
        expires_at=view.expires_at,
        server_now=view.server_now,
        questions=view.questions,
        answers=view.answers,
    )


@router.get("/api/learning/courses/{slug}/exam", response_model=ExamViewResponse)
async def get_exam_view(
    slug: str,
    response: Response,
    user: User | None = Depends(get_optional_user_strict),
    eligibility: EligibilityService = Depends(get_eligibility_service),
) -> ExamViewResponse:
    response.headers["Cache-Control"] = NO_STORE
    view = await eligibility.get_exam_view(slug[:200], user.id if user else None)
    return ExamViewResponse(**view.__dict__)


@router.post(
    "/api/learning/courses/{slug}/exam/attempts",
    response_model=ActiveAttemptResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_attempt(
    slug: str,
    payload: StartAttemptRequest,
    response: Response,
    user: User = Depends(get_current_user),
    attempts: AttemptService = Depends(get_attempt_service),
) -> ActiveAttemptResponse:
    view, created = await attempts.start(slug[:200], user.id, payload.recipient_name)
    response.headers["Cache-Control"] = NO_STORE
    if not created:
        response.status_code = status.HTTP_200_OK
    return _active(view)


@router.get(
    "/api/learning/exam-attempts/{attempt_id}",
    response_model=ActiveAttemptResponse | GradedAttemptResponse,
)
async def get_attempt(
    attempt_id: uuid.UUID,
    response: Response,
    user: User = Depends(get_current_user),
    attempts: AttemptService = Depends(get_attempt_service),
):
    response.headers["Cache-Control"] = NO_STORE
    view = await attempts.get(attempt_id, user.id)
    if isinstance(view, ActiveAttemptView):
        return _active(view)
    return GradedAttemptResponse(attempt_id=view.attempt_id, status=view.status, result=ResultResponse(**view.result.__dict__))


@router.put("/api/learning/exam-attempts/{attempt_id}/answers", response_model=SaveAnswersResponse)
async def save_answers(
    attempt_id: uuid.UUID,
    payload: SaveAnswersRequest,
    user: User = Depends(get_current_user),
    attempts: AttemptService = Depends(get_attempt_service),
) -> SaveAnswersResponse:
    saved_at = await attempts.save(
        attempt_id, user.id, [(a.question_id, a.option_index) for a in payload.answers], payload.tab_switches
    )
    return SaveAnswersResponse(saved_at=saved_at)


@router.post("/api/learning/exam-attempts/{attempt_id}/submit", response_model=ResultResponse)
async def submit_attempt(
    attempt_id: uuid.UUID,
    response: Response,
    user: User = Depends(get_current_user),
    attempts: AttemptService = Depends(get_attempt_service),
) -> ResultResponse:
    response.headers["Cache-Control"] = NO_STORE
    result = await attempts.submit(attempt_id, user.id)
    return ResultResponse(**result.__dict__)


@router.get("/api/learning/exam-attempts/{attempt_id}/ping", response_model=PingResponse)
async def ping_attempt(
    attempt_id: uuid.UUID,
    response: Response,
    user: User = Depends(get_current_user),
    attempts: AttemptService = Depends(get_attempt_service),
) -> PingResponse:
    response.headers["Cache-Control"] = NO_STORE
    view = await attempts.ping(attempt_id, user.id)
    return PingResponse(status=view.status, expires_at=view.expires_at, server_now=view.server_now)


@router.post(
    "/api/learning/exam-attempts/{attempt_id}/reports",
    response_model=ReportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def report_question(
    attempt_id: uuid.UUID,
    payload: ReportRequest,
    user: User = Depends(get_current_user),
    attempts: AttemptService = Depends(get_attempt_service),
) -> ReportResponse:
    await attempts.report(attempt_id, user.id, payload.question_id, payload.reason, payload.comment)
    return ReportResponse()


@router.get("/api/me/certificates", response_model=MyCertificatesResponse)
async def my_certificates(
    user: User = Depends(get_current_user),
    certificates: CertificateService = Depends(get_certificate_service),
) -> MyCertificatesResponse:
    records = await certificates.list_mine(user.id)
    return MyCertificatesResponse(
        items=[
            MyCertificate(
                certificate_id=r.certificate_id,
                course_slug=r.course_slug,
                course_title=r.course_title,
                issued_at=r.issued_at,
                status=r.status,
            )
            for r in records
        ]
    )
