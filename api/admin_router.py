import uuid

from fastapi import APIRouter, Depends, Query, status

from core.dependencies import (
    get_blog_service,
    get_client_service,
    get_email_service,
    get_faq_service,
    get_lead_service,
    get_portfolio_service,
    get_service_catalog_service,
    get_testimonial_service,
    require_admin,
)
from database.models import BlogPost, Faq, Lead, PortfolioItem, ProjectStatus, Service, Testimonial
from schemas.client_schemas import ProjectStatusResponse, ProjectStatusUpsertRequest
from schemas.diagnostics_schemas import EmailTestRequest, EmailTestResponse
from schemas.content_schemas import (
    BlogPostCreateRequest,
    BlogPostResponse,
    BlogPostUpdateRequest,
    FaqCreateRequest,
    FaqResponse,
    FaqUpdateRequest,
    PortfolioCreateRequest,
    PortfolioResponse,
    PortfolioUpdateRequest,
    ServiceCreateRequest,
    ServiceResponse,
    ServiceUpdateRequest,
    TestimonialCreateRequest,
    TestimonialResponse,
    TestimonialUpdateRequest,
)
from schemas.lead_schemas import LeadListResponse, LeadResponse, LeadStatusUpdateRequest
from services.blog_service import BlogService
from services.client_service import ClientService
from services.email_service import EmailService
from services.faq_service import FaqService
from services.lead_service import LeadService
from services.portfolio_service import PortfolioService
from services.service_catalog_service import ServiceCatalogService
from services.testimonial_service import TestimonialService

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# ---- Diagnostics ----


@router.post("/test-email", response_model=EmailTestResponse)
async def test_email(
    payload: EmailTestRequest,
    email_service: EmailService = Depends(get_email_service),
) -> EmailTestResponse:
    """Synchronously attempts a real send via the configured email provider
    (Resend) and reports the actual result - including the provider's own
    error message on failure. Unlike signup/lead-capture (where email is
    fire-and-forget in the background), this endpoint waits for the attempt
    so an admin can verify email delivery end-to-end without needing access
    to server logs.
    """
    provider, from_address, is_configured = email_service.connection_summary()
    to_address = payload.to_address or (from_address if is_configured else None)
    if not to_address:
        return EmailTestResponse(
            sent=False,
            to_address="",
            provider=provider,
            from_address=from_address,
            api_key_configured=is_configured,
            detail="No to_address given and RESEND_API_KEY is not configured - nothing to send to.",
        )

    sent, detail = await email_service.send_test_email(to_address)
    return EmailTestResponse(
        sent=sent,
        to_address=to_address,
        provider=provider,
        from_address=from_address,
        api_key_configured=is_configured,
        detail=detail,
    )


# ---- Leads (CRM-lite) ----


@router.get("/leads", response_model=LeadListResponse)
async def list_leads(
    source: str | None = None,
    status_filter: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    lead_service: LeadService = Depends(get_lead_service),
) -> LeadListResponse:
    leads, total = await lead_service.list_leads(source=source, status=status_filter, limit=limit, offset=offset)
    return LeadListResponse(total=total, items=[LeadResponse.model_validate(lead) for lead in leads])


@router.put("/leads/{lead_id}", response_model=LeadResponse)
async def update_lead(
    lead_id: uuid.UUID,
    payload: LeadStatusUpdateRequest,
    lead_service: LeadService = Depends(get_lead_service),
) -> Lead:
    return await lead_service.update_lead_status(lead_id, payload.status)


# ---- Services catalog ----


@router.get("/services", response_model=list[ServiceResponse])
async def admin_list_services(catalog_service: ServiceCatalogService = Depends(get_service_catalog_service)) -> list[Service]:
    return await catalog_service.list_all()


@router.post("/services", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_service(
    payload: ServiceCreateRequest, catalog_service: ServiceCatalogService = Depends(get_service_catalog_service)
) -> Service:
    return await catalog_service.create(**payload.model_dump())


@router.put("/services/{service_id}", response_model=ServiceResponse)
async def admin_update_service(
    service_id: uuid.UUID,
    payload: ServiceUpdateRequest,
    catalog_service: ServiceCatalogService = Depends(get_service_catalog_service),
) -> Service:
    return await catalog_service.update(service_id, **payload.model_dump(exclude_unset=True))


@router.delete("/services/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_service(
    service_id: uuid.UUID, catalog_service: ServiceCatalogService = Depends(get_service_catalog_service)
) -> None:
    await catalog_service.delete(service_id)


# ---- Portfolio items ----


@router.get("/portfolio", response_model=list[PortfolioResponse])
async def admin_list_portfolio(portfolio_service: PortfolioService = Depends(get_portfolio_service)) -> list[PortfolioItem]:
    return await portfolio_service.list_all()


@router.post("/portfolio", response_model=PortfolioResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_portfolio(
    payload: PortfolioCreateRequest, portfolio_service: PortfolioService = Depends(get_portfolio_service)
) -> PortfolioItem:
    return await portfolio_service.create(**payload.model_dump())


@router.put("/portfolio/{item_id}", response_model=PortfolioResponse)
async def admin_update_portfolio(
    item_id: uuid.UUID,
    payload: PortfolioUpdateRequest,
    portfolio_service: PortfolioService = Depends(get_portfolio_service),
) -> PortfolioItem:
    return await portfolio_service.update(item_id, **payload.model_dump(exclude_unset=True))


@router.delete("/portfolio/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_portfolio(
    item_id: uuid.UUID, portfolio_service: PortfolioService = Depends(get_portfolio_service)
) -> None:
    await portfolio_service.delete(item_id)


# ---- Testimonials ----


@router.get("/testimonials", response_model=list[TestimonialResponse])
async def admin_list_testimonials(
    testimonial_service: TestimonialService = Depends(get_testimonial_service),
) -> list[Testimonial]:
    return await testimonial_service.list_all()


@router.post("/testimonials", response_model=TestimonialResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_testimonial(
    payload: TestimonialCreateRequest, testimonial_service: TestimonialService = Depends(get_testimonial_service)
) -> Testimonial:
    return await testimonial_service.create(**payload.model_dump())


@router.put("/testimonials/{testimonial_id}", response_model=TestimonialResponse)
async def admin_update_testimonial(
    testimonial_id: uuid.UUID,
    payload: TestimonialUpdateRequest,
    testimonial_service: TestimonialService = Depends(get_testimonial_service),
) -> Testimonial:
    return await testimonial_service.update(testimonial_id, **payload.model_dump(exclude_unset=True))


@router.delete("/testimonials/{testimonial_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_testimonial(
    testimonial_id: uuid.UUID, testimonial_service: TestimonialService = Depends(get_testimonial_service)
) -> None:
    await testimonial_service.delete(testimonial_id)


# ---- FAQs ----


@router.get("/faqs", response_model=list[FaqResponse])
async def admin_list_faqs(faq_service: FaqService = Depends(get_faq_service)) -> list[Faq]:
    return await faq_service.list_all()


@router.post("/faqs", response_model=FaqResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_faq(payload: FaqCreateRequest, faq_service: FaqService = Depends(get_faq_service)) -> Faq:
    return await faq_service.create(**payload.model_dump())


@router.put("/faqs/{faq_id}", response_model=FaqResponse)
async def admin_update_faq(
    faq_id: uuid.UUID, payload: FaqUpdateRequest, faq_service: FaqService = Depends(get_faq_service)
) -> Faq:
    return await faq_service.update(faq_id, **payload.model_dump(exclude_unset=True))


@router.delete("/faqs/{faq_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_faq(faq_id: uuid.UUID, faq_service: FaqService = Depends(get_faq_service)) -> None:
    await faq_service.delete(faq_id)


# ---- Blog posts ----


@router.get("/blog", response_model=list[BlogPostResponse])
async def admin_list_blog_posts(blog_service: BlogService = Depends(get_blog_service)) -> list[BlogPost]:
    return await blog_service.list_all()


@router.post("/blog", response_model=BlogPostResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_blog_post(payload: BlogPostCreateRequest, blog_service: BlogService = Depends(get_blog_service)) -> BlogPost:
    return await blog_service.create(**payload.model_dump())


@router.put("/blog/{post_id}", response_model=BlogPostResponse)
async def admin_update_blog_post(
    post_id: uuid.UUID, payload: BlogPostUpdateRequest, blog_service: BlogService = Depends(get_blog_service)
) -> BlogPost:
    return await blog_service.update(post_id, **payload.model_dump(exclude_unset=True))


@router.delete("/blog/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_blog_post(post_id: uuid.UUID, blog_service: BlogService = Depends(get_blog_service)) -> None:
    await blog_service.delete(post_id)


# ---- Client project status ----


@router.put("/project-status/{client_user_id}", response_model=ProjectStatusResponse)
async def admin_update_project_status(
    client_user_id: uuid.UUID,
    payload: ProjectStatusUpsertRequest,
    client_service: ClientService = Depends(get_client_service),
) -> ProjectStatus:
    return await client_service.set_project_status(
        client_user_id, payload.project_name, payload.phase, payload.percent_complete
    )
