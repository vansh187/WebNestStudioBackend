"""Public certificate verification (contract section 6) - no auth."""

from fastapi import APIRouter, Depends, Request, Response

from core.dependencies import get_client_ip
from exams.routing import ExamContractRoute
from exams.dependencies import get_certificate_service
from exams.schemas import PublicCertificateResponse
from exams.services.certificate_service import CertificateService

router = APIRouter(tags=["certificates"], route_class=ExamContractRoute)


def _client_key(request: Request) -> str:
    return get_client_ip(request) or "unknown"


@router.get("/api/certificates/{certificate_id}", response_model=PublicCertificateResponse)
async def get_certificate(
    certificate_id: str,
    request: Request,
    certificates: CertificateService = Depends(get_certificate_service),
) -> PublicCertificateResponse:
    record = await certificates.get_public(certificate_id, _client_key(request))
    # No email, user id or score - just what the public page shows.
    return PublicCertificateResponse(
        certificate_id=record.certificate_id,
        recipient_name=record.recipient_name,
        course_title=record.course_title,
        course_slug=record.course_slug,
        issued_at=record.issued_at,
        status=record.status,
    )


@router.get("/api/certificates/{certificate_id}/pdf", response_class=Response)
async def get_certificate_pdf(
    certificate_id: str,
    request: Request,
    certificates: CertificateService = Depends(get_certificate_service),
) -> Response:
    normalized_id, pdf = await certificates.get_pdf(certificate_id, _client_key(request))
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{normalized_id}.pdf"',
            "Cache-Control": "public, max-age=3600",
        },
    )
