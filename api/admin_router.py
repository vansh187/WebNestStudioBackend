import uuid

from fastapi import APIRouter, Depends, Query, status

from core.dependencies import (
    get_auth_service,
    get_blog_generation_service,
    get_blog_service,
    get_client_service,
    get_email_service,
    get_faq_service,
    get_lead_service,
    get_messaging_service,
    get_portfolio_service,
    get_project_service,
    get_service_catalog_service,
    get_testimonial_service,
    require_admin,
)
from core.exceptions import BadRequestError
from database.models import BlogPost, Faq, Lead, PortfolioItem, ProjectStatus, Service, Testimonial, User
from schemas.blog_generation_schemas import (
    BlogGenerationLogResponse,
    BlogGenerationTriggerRequest,
    BlogGenerationTriggerResponse,
)
from schemas.client_schemas import ProjectStatusResponse, ProjectStatusUpsertRequest
from schemas.diagnostics_schemas import EmailTestRequest, EmailTestResponse
from schemas.project_schemas import (
    AdminProjectCreateRequest,
    AdminProjectListResponse,
    AdminProjectRow,
    AdminProjectUpdateRequest,
    AdminStageUpdateRequest,
)
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
from schemas.messaging_schemas import BlockUserResponse, ReportListResponse, ReportOut
from services.auth_service import AuthService
from services.blog_generation_service import BlogGenerationError, BlogGenerationService
from services.blog_service import BlogService
from services.client_service import ClientService
from services.email_service import EmailService
from services.faq_service import FaqService
from services.lead_service import LeadService
from services.llm_provider import GenerationFailedError
from services.messaging_service import MessagingService
from services.portfolio_service import PortfolioService
from services.project_service import ProjectService
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


@router.post("/blog/generate", response_model=BlogGenerationTriggerResponse)
async def admin_trigger_blog_generation(
    payload: BlogGenerationTriggerRequest = BlogGenerationTriggerRequest(),
    generation_service: BlogGenerationService = Depends(get_blog_generation_service),
) -> BlogGenerationTriggerResponse:
    """Manually runs the automated blog generation pipeline right now,
    bypassing the 2-day cooldown (SEO/slug validation still applies). For
    recovery from a missed scheduled run, testing without waiting for the
    next cron fire, or - via the optional topic_hint - directing the model
    to write about a specific subject instead of picking its own. LLM/
    generation failures are reported as a normal (success=False) response
    rather than an HTTP error, since a provider being temporarily
    unavailable is an expected outcome, not a server bug.
    """
    try:
        post = await generation_service.generate_and_publish(trigger_source="manual-admin", topic_hint=payload.topic_hint)
    except (BlogGenerationError, GenerationFailedError) as exc:
        return BlogGenerationTriggerResponse(success=False, detail=str(exc))
    return BlogGenerationTriggerResponse(success=True, detail="Blog post generated and published", post_id=post.id, slug=post.slug)


@router.get("/blog/generation-logs", response_model=list[BlogGenerationLogResponse])
async def admin_list_blog_generation_logs(
    limit: int = Query(default=20, ge=1, le=200),
    generation_service: BlogGenerationService = Depends(get_blog_generation_service),
) -> list:
    return await generation_service.list_recent_logs(limit=limit)


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


# ---- Projects (SDLC pipeline) ----


@router.get("/projects", response_model=AdminProjectListResponse)
async def admin_list_projects(
    client_email: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    project_service: ProjectService = Depends(get_project_service),
) -> AdminProjectListResponse:
    projects, total = await project_service.list_admin(
        client_email=client_email, status=status_filter, limit=limit, offset=offset
    )
    return AdminProjectListResponse(projects=projects, total=total)


@router.post("/projects", response_model=AdminProjectRow, status_code=status.HTTP_201_CREATED)
async def admin_create_project(
    payload: AdminProjectCreateRequest,
    current_user: User = Depends(require_admin),
    project_service: ProjectService = Depends(get_project_service),
) -> AdminProjectRow:
    return await project_service.create_project(
        admin_user_id=current_user.id,
        client_email=payload.client_email,
        name=payload.name,
        summary=payload.summary,
        current_stage=payload.current_stage,
        create_conversation=payload.create_conversation,
    )


@router.get("/projects/{project_id}", response_model=AdminProjectRow)
async def admin_get_project(
    project_id: uuid.UUID,
    current_user: User = Depends(require_admin),
    project_service: ProjectService = Depends(get_project_service),
) -> AdminProjectRow:
    return await project_service.get_admin(project_id, admin_user_id=current_user.id)


@router.patch("/projects/{project_id}", response_model=AdminProjectRow)
async def admin_update_project(
    project_id: uuid.UUID,
    payload: AdminProjectUpdateRequest,
    current_user: User = Depends(require_admin),
    project_service: ProjectService = Depends(get_project_service),
) -> AdminProjectRow:
    return await project_service.update_project(
        project_id, admin_user_id=current_user.id, **payload.model_dump(exclude_unset=True)
    )


@router.patch("/projects/{project_id}/stages/{stage_key}", response_model=AdminProjectRow)
async def admin_update_project_stage(
    project_id: uuid.UUID,
    stage_key: str,
    payload: AdminStageUpdateRequest,
    project_service: ProjectService = Depends(get_project_service),
) -> AdminProjectRow:
    return await project_service.update_stage(
        project_id, stage_key, state=payload.state, note=payload.note
    )


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_project(
    project_id: uuid.UUID,
    hard: bool = False,
    project_service: ProjectService = Depends(get_project_service),
) -> None:
    if hard:
        await project_service.hard_delete(project_id)
    else:
        await project_service.archive(project_id)


# ---- Moderation: reported messages & user blocking ----
#
# Play Store / general UGC policy requires (a) a way for users to report
# abusive content and (b) a way for the operator to act on it. Reporting
# itself lives on the messaging router (any participant can report a
# message they can see); everything here - reviewing those reports and
# blocking/unblocking the offending account - is admin-only via the
# router-level require_admin dependency.


@router.get("/reports", response_model=ReportListResponse)
async def list_reports(
    status_filter: str = Query(default="open", alias="status", pattern="^(open|resolved)$"),
    messaging: MessagingService = Depends(get_messaging_service),
) -> ReportListResponse:
    return await messaging.list_reports(status_filter)


@router.post("/reports/{report_id}/resolve", response_model=ReportOut)
async def resolve_report(
    report_id: uuid.UUID,
    messaging: MessagingService = Depends(get_messaging_service),
) -> ReportOut:
    return await messaging.resolve_report(report_id)


@router.post("/users/{user_id}/block", response_model=BlockUserResponse)
async def block_user(
    user_id: uuid.UUID,
    current_user: User = Depends(require_admin),
    auth_service: AuthService = Depends(get_auth_service),
) -> BlockUserResponse:
    if user_id == current_user.id:
        raise BadRequestError("You cannot block your own account")
    user = await auth_service.set_user_active(user_id, False)
    return BlockUserResponse(id=user.id, is_active=user.is_active)


@router.post("/users/{user_id}/unblock", response_model=BlockUserResponse)
async def unblock_user(
    user_id: uuid.UUID,
    auth_service: AuthService = Depends(get_auth_service),
) -> BlockUserResponse:
    user = await auth_service.set_user_active(user_id, True)
    return BlockUserResponse(id=user.id, is_active=user.is_active)
