from datetime import datetime, timezone
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Response

from core.config import settings
from core.dependencies import get_blog_service, get_portfolio_service
from services.blog_service import BlogService
from services.portfolio_service import PortfolioService

router = APIRouter(tags=["sitemap"])

# (path, priority, changefreq) for the SPA's public, indexable static routes.
# Mirrors the routes in App.jsx; /login, /portal and /admin are intentionally excluded.
STATIC_PATHS = [
    ("", "1.0", "daily"),
    ("about", "0.7", "monthly"),
    ("services", "0.8", "monthly"),
    ("portfolio", "0.8", "weekly"),
    ("blog", "0.8", "daily"),
    ("faqs", "0.6", "monthly"),
    ("contact", "0.7", "monthly"),
]


def _url_entry(loc: str, lastmod: datetime | None, priority: str, changefreq: str) -> str:
    lines = [f"  <url>\n    <loc>{escape(loc)}</loc>"]
    if lastmod is not None:
        lines.append(f"    <lastmod>{lastmod.strftime('%Y-%m-%d')}</lastmod>")
    lines.append(f"    <changefreq>{changefreq}</changefreq>")
    lines.append(f"    <priority>{priority}</priority>")
    lines.append("  </url>")
    return "\n".join(lines)


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap(
    blog_service: BlogService = Depends(get_blog_service),
    portfolio_service: PortfolioService = Depends(get_portfolio_service),
) -> Response:
    base_url = settings.frontend_base_url.rstrip("/")
    now = datetime.now(timezone.utc)

    entries = [
        _url_entry(f"{base_url}/{path}" if path else base_url, now, priority, changefreq)
        for path, priority, changefreq in STATIC_PATHS
    ]

    posts = await blog_service.list_published()
    for post in posts:
        entries.append(_url_entry(f"{base_url}/blog/{post.slug}", post.updated_at or post.published_at, "0.6", "monthly"))

    portfolio_items = await portfolio_service.list_published()
    for item in portfolio_items:
        entries.append(_url_entry(f"{base_url}/portfolio/{item.slug}", item.updated_at, "0.6", "monthly"))

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )
    return Response(content=body, media_type="application/xml")


@router.get("/robots.txt", include_in_schema=False)
async def robots_txt() -> Response:
    base_url = settings.frontend_base_url.rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /portal\n"
        "Disallow: /login\n"
        f"Sitemap: {base_url}/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain")
