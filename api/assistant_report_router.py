import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError as PydanticValidationError

from api.assistant_router import get_assistant_user_id
from core.dependencies import get_assistant_report_service, require_admin
from core.request_limits import read_limited_body
from database.models import User
from schemas.assistant_schemas import AssistantReportCreate, AssistantReportCreated, AssistantReportList, AssistantReportOut
from services.assistant_report_service import AssistantReportService

router = APIRouter(prefix="/api/assistant", tags=["ai-assistant-reports"])
admin_router = APIRouter(prefix="/api/admin/assistant-reports", tags=["admin"], dependencies=[Depends(require_admin)])

# Long fields are cut, not refused, so this only stops abuse: a whole long reply, even with
# every character JSON-escaped, fits. What is kept is far smaller.
MAX_REPORT_BODY_BYTES = 1024 * 1024


async def _read_report(request: Request) -> AssistantReportCreate:
    """Errors never echo the reported text back."""
    raw = await read_limited_body(request, MAX_REPORT_BODY_BYTES, "This report is too large.")
    try:
        return AssistantReportCreate.model_validate_json(raw)
    except PydanticValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False)
        raise RequestValidationError([{**error, "loc": ("body", *error["loc"])} for error in errors]) from exc


@router.post(
    "/reports",
    status_code=status.HTTP_201_CREATED,
    response_model=AssistantReportCreated,
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": AssistantReportCreate.model_json_schema()}}}},
)
async def report_reply(
    request: Request,
    user_id: uuid.UUID = Depends(get_assistant_user_id),
    reports: AssistantReportService = Depends(get_assistant_report_service),
) -> AssistantReportCreated:
    return await reports.report(user_id, await _read_report(request))


@admin_router.get("", response_model=AssistantReportList)
async def list_reports(
    status_filter: str = Query(default="open", alias="status", pattern="^(open|resolved)$"),
    reports: AssistantReportService = Depends(get_assistant_report_service),
) -> AssistantReportList:
    return AssistantReportList(reports=await reports.list_reports(status_filter))


@admin_router.post("/{report_id}/resolve", response_model=AssistantReportOut)
async def resolve_report(
    report_id: uuid.UUID,
    admin: User = Depends(require_admin),
    reports: AssistantReportService = Depends(get_assistant_report_service),
) -> AssistantReportOut:
    return await reports.resolve(report_id, admin.id)
