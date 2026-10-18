"""Admin exam endpoints (contract section 7) - role admin only."""

import json
import uuid

from fastapi import APIRouter, Depends, Query, Request, status

from core.dependencies import container, require_admin
from database.models import User
from exams.routing import ExamContractRoute
from exams.dependencies import ExamServices, get_admin_service, get_certificate_service, get_exam_services
from exams.errors import ExamValidationError
from exams.jobs import start_refill_in_background
from exams.records import CertificateRecord, JobRecord
from exams.schemas import (
    AdminCertificate,
    AdminCertificateList,
    AdminExamSettingsRequest,
    AdminExamSettingsResponse,
    AdminPoolSummary,
    AdminQuestionCreate,
    AdminQuestionItem,
    AdminQuestionList,
    AdminQuestionUpdate,
    ExamContextRequest,
    ExamContextResponse,
    ImportResponse,
    JobResponse,
    QuotaResponse,
    RefillResponse,
    ReissueRequest,
    RevokeRequest,
    VoidResponse,
)
from exams.services.certificate_service import CertificateService
from exams.services.question_admin_service import MAX_IMPORT, QuestionAdminService

router = APIRouter(tags=["exams-admin"], dependencies=[Depends(require_admin)], route_class=ExamContractRoute)

MAX_IMPORT_BYTES = 5 * 1024 * 1024


def _job(job: JobRecord) -> JobResponse:
    progress = job.progress or {}
    return JobResponse(
        job_id=job.id,
        status=job.status,
        trigger=job.trigger,
        modules_done=int(progress.get("modules_done", 0) or 0),
        modules_total=int(progress.get("modules_total", 0) or 0),
        calls=job.calls,
        created=job.created,
        rejected_by_check=job.rejected or {},
        errors=[str(e) for e in (job.errors or [])][-50:],
        paused_until=job.paused_until,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _certificate(record: CertificateRecord) -> AdminCertificate:
    return AdminCertificate(
        certificate_id=record.certificate_id,
        recipient_name=record.recipient_name,
        user_email=record.user_email,
        course_title=record.course_title,
        score=record.score,
        total=record.total,
        issued_at=record.issued_at,
        status=record.status,
        revoked_reason=record.revoked_reason,
        replaced_by=record.replaced_by,
    )


async def _start_refill(services: ExamServices, course_id: uuid.UUID, trigger: str) -> RefillResponse:
    job, created = await services.generation_service().start_job(course_id, trigger)
    if job is None:
        return RefillResponse(job_id=None, status="not_started")
    if created:
        start_refill_in_background(container.database, container.settings, course_id, trigger, job.id)
    return RefillResponse(job_id=job.id, status=job.status)


@router.put("/api/admin/learning/courses/{slug}/exam", response_model=AdminExamSettingsResponse)
async def set_exam(
    slug: str,
    payload: AdminExamSettingsRequest,
    admin: User = Depends(require_admin),
    services: ExamServices = Depends(get_exam_services),
) -> AdminExamSettingsResponse:
    course, needs_fill = await services.admin_service().set_exam(slug, payload.enabled, payload.code, admin.id)
    job_id = None
    if needs_fill:
        job_id = (await _start_refill(services, course.id, "enabled")).job_id
    ready = course.exam_enabled and await services.pool.is_visible(course)
    return AdminExamSettingsResponse(slug=course.slug, enabled=course.exam_enabled, code=course.exam_code, ready=ready, job_id=job_id)


@router.put("/api/admin/learning/courses/{slug}/exam-context", response_model=ExamContextResponse)
async def upload_exam_context(
    slug: str,
    payload: ExamContextRequest,
    admin: User = Depends(require_admin),
    services: ExamServices = Depends(get_exam_services),
) -> ExamContextResponse:
    result = await services.admin_service().upload_context(slug, [m.model_dump() for m in payload.modules], admin.id)
    if result["quarantined"]:
        course = await services.admin_service().course_for(slug)
        if course.exam_enabled:
            await _start_refill(services, course.id, "context_changed")
    return ExamContextResponse(**result)


@router.get("/api/admin/learning/exam-questions/summary", response_model=AdminPoolSummary)
async def pool_summary(
    course: str = Query(min_length=1, max_length=200),
    admin_service: QuestionAdminService = Depends(get_admin_service),
) -> AdminPoolSummary:
    return AdminPoolSummary(**await admin_service.summary(course))


@router.get("/api/admin/learning/exam-questions", response_model=AdminQuestionList)
async def list_questions(
    course: str = Query(min_length=1, max_length=200),
    status_filter: str | None = Query(default=None, alias="status", pattern="^(live|quarantined|rejected|voided|retired)$"),
    module: str | None = Query(default=None, max_length=200),
    admin_service: QuestionAdminService = Depends(get_admin_service),
) -> AdminQuestionList:
    items = await admin_service.list_questions(course, status_filter, module)
    return AdminQuestionList(items=[AdminQuestionItem(**item) for item in items])


@router.post("/api/admin/learning/exam-questions", response_model=AdminQuestionItem, status_code=status.HTTP_201_CREATED)
async def create_question(
    payload: AdminQuestionCreate,
    admin: User = Depends(require_admin),
    admin_service: QuestionAdminService = Depends(get_admin_service),
) -> AdminQuestionItem:
    return AdminQuestionItem(**await admin_service.create_manual(payload.model_dump(), admin.id))


@router.post("/api/admin/learning/exam-questions/import", response_model=ImportResponse)
async def import_questions(
    request: Request,
    admin: User = Depends(require_admin),
    admin_service: QuestionAdminService = Depends(get_admin_service),
) -> ImportResponse:
    """JSON array, {"questions": [...]}, JSON Lines, or a multipart file
    (field "file") holding either."""
    items = await _read_import(request)
    result = await admin_service.import_questions(items, admin.id)
    return ImportResponse(imported=result.imported, rejected=result.rejected)


async def _read_import(request: Request) -> list[object]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise ExamValidationError("multipart import needs a 'file' field")
        raw = await upload.read(MAX_IMPORT_BYTES + 1)
    else:
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_IMPORT_BYTES:
                break
    if len(raw) > MAX_IMPORT_BYTES:
        raise ExamValidationError("import file is larger than 5 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExamValidationError("import must be UTF-8 text") from exc
    stripped = text.strip()
    if not stripped:
        raise ExamValidationError("import is empty")
    if stripped[0] in "[{":
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                data = data.get("questions")
            if isinstance(data, list):
                items = data
            else:
                raise ExamValidationError("expected a JSON array of questions")
        except json.JSONDecodeError:
            items = None
        if items is not None:
            if len(items) > MAX_IMPORT:
                raise ExamValidationError(f"At most {MAX_IMPORT} questions per request")
            return items
    # JSON Lines: one object per non-empty line; an unparsable line becomes a
    # non-object entry so it's reported with its line number.
    items = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            items.append(None)
        if len(items) > MAX_IMPORT:
            raise ExamValidationError(f"At most {MAX_IMPORT} questions per request")
    return items


@router.put("/api/admin/learning/exam-questions/{question_id}", response_model=AdminQuestionItem)
async def edit_question(
    question_id: uuid.UUID,
    payload: AdminQuestionUpdate,
    admin: User = Depends(require_admin),
    admin_service: QuestionAdminService = Depends(get_admin_service),
) -> AdminQuestionItem:
    return AdminQuestionItem(**await admin_service.edit(question_id, payload.model_dump(exclude_unset=True), admin.id))


@router.post("/api/admin/learning/exam-questions/{question_id}/void", response_model=VoidResponse)
async def void_question(
    question_id: uuid.UUID,
    admin: User = Depends(require_admin),
    admin_service: QuestionAdminService = Depends(get_admin_service),
) -> VoidResponse:
    return VoidResponse(**await admin_service.void(question_id, admin.id))


@router.post("/api/admin/learning/exams/{slug}/refill", response_model=RefillResponse)
async def refill(slug: str, services: ExamServices = Depends(get_exam_services)) -> RefillResponse:
    course = await services.admin_service().course_for(slug)
    return await _start_refill(services, course.id, "admin")


@router.get("/api/admin/learning/exams/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: uuid.UUID, admin_service: QuestionAdminService = Depends(get_admin_service)) -> JobResponse:
    return _job(await admin_service.get_job(job_id))


@router.get("/api/admin/learning/exams/quota", response_model=QuotaResponse)
async def quota(admin_service: QuestionAdminService = Depends(get_admin_service)) -> QuotaResponse:
    return QuotaResponse(models=await admin_service.quota())


@router.get("/api/admin/certificates", response_model=AdminCertificateList)
async def search_certificates(
    q: str | None = Query(default=None, max_length=200),
    certificates: CertificateService = Depends(get_certificate_service),
) -> AdminCertificateList:
    return AdminCertificateList(items=[_certificate(r) for r in await certificates.search(q)])


@router.post("/api/admin/certificates/{certificate_id}/revoke", response_model=AdminCertificate)
async def revoke_certificate(
    certificate_id: str,
    payload: RevokeRequest,
    admin: User = Depends(require_admin),
    certificates: CertificateService = Depends(get_certificate_service),
) -> AdminCertificate:
    return _certificate(await certificates.revoke(certificate_id, payload.reason, admin.id))


@router.post("/api/admin/certificates/{certificate_id}/reissue", response_model=AdminCertificate)
async def reissue_certificate(
    certificate_id: str,
    payload: ReissueRequest,
    admin: User = Depends(require_admin),
    certificates: CertificateService = Depends(get_certificate_service),
) -> AdminCertificate:
    return _certificate(await certificates.reissue(certificate_id, payload.recipient_name, admin.id))
