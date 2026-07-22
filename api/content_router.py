from fastapi import APIRouter, Depends

from core.dependencies import get_blog_service, get_faq_service, get_portfolio_service, get_service_catalog_service
from database.models import BlogPost, Faq, PortfolioItem, Service
from schemas.content_schemas import BlogPostResponse, FaqResponse, PortfolioResponse, ServiceResponse
from services.blog_service import BlogService
from services.faq_service import FaqService
from services.portfolio_service import PortfolioService
from services.service_catalog_service import ServiceCatalogService

router = APIRouter(prefix="/api", tags=["content"])


@router.get("/services", response_model=list[ServiceResponse])
async def list_services(catalog_service: ServiceCatalogService = Depends(get_service_catalog_service)) -> list[Service]:
    return await catalog_service.list_published()


@router.get("/portfolio", response_model=list[PortfolioResponse])
async def list_portfolio(
    category: str | None = None,
    portfolio_service: PortfolioService = Depends(get_portfolio_service),
) -> list[PortfolioItem]:
    return await portfolio_service.list_published(category=category)


@router.get("/portfolio/{slug}", response_model=PortfolioResponse)
async def get_portfolio_item(
    slug: str, portfolio_service: PortfolioService = Depends(get_portfolio_service)
) -> PortfolioItem:
    return await portfolio_service.get_by_slug(slug)


@router.get("/blog", response_model=list[BlogPostResponse])
async def list_blog_posts(tag: str | None = None, blog_service: BlogService = Depends(get_blog_service)) -> list[BlogPost]:
    return await blog_service.list_published(tag=tag)


@router.get("/blog/{slug}", response_model=BlogPostResponse)
async def get_blog_post(slug: str, blog_service: BlogService = Depends(get_blog_service)) -> BlogPost:
    return await blog_service.get_by_slug(slug)


@router.get("/faqs", response_model=list[FaqResponse])
async def list_faqs(category: str | None = None, faq_service: FaqService = Depends(get_faq_service)) -> list[Faq]:
    return await faq_service.list_published(category=category)
