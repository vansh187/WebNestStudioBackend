from fastapi import APIRouter, Depends, Query

from core.dependencies import get_portfolio_service, get_service_catalog_service, get_testimonial_service
from database.models import PortfolioItem, Service, Testimonial
from schemas.content_schemas import PortfolioResponse, ServiceResponse, TestimonialResponse
from services.portfolio_service import PortfolioService
from services.service_catalog_service import ServiceCatalogService
from services.testimonial_service import TestimonialService

router = APIRouter(prefix="/api/home", tags=["home"])


@router.get("/services-preview", response_model=list[ServiceResponse])
async def services_preview(
    limit: int = Query(default=4, ge=1, le=50),
    catalog_service: ServiceCatalogService = Depends(get_service_catalog_service),
) -> list[Service]:
    return await catalog_service.list_published(limit=limit)


@router.get("/featured-work", response_model=list[PortfolioResponse])
async def featured_work(
    limit: int = Query(default=6, ge=1, le=50),
    portfolio_service: PortfolioService = Depends(get_portfolio_service),
) -> list[PortfolioItem]:
    return await portfolio_service.list_published(limit=limit)


@router.get("/testimonials", response_model=list[TestimonialResponse])
async def home_testimonials(
    limit: int = Query(default=6, ge=1, le=50),
    testimonial_service: TestimonialService = Depends(get_testimonial_service),
) -> list[Testimonial]:
    return await testimonial_service.list_published(limit=limit)


@router.get("/project-status-demo")
async def project_status_demo() -> dict:
    return {
        "project_name": "WebNest Studio Demo Project",
        "phase": "Development",
        "percent_complete": 65,
    }


@router.get("/stats")
async def home_stats(
    portfolio_service: PortfolioService = Depends(get_portfolio_service),
) -> dict:
    delivered_projects = await portfolio_service.list_published()
    return {
        "projects_delivered": len(delivered_projects),
    }
