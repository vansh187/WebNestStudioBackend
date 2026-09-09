import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from api.admin_router import router as admin_router
from api.auth_router import router as auth_router
from api.chat_router import router as chat_router
from api.client_router import router as client_router
from api.coding_router import router as coding_router
from api.content_router import router as content_router
from api.generation_router import router as generation_router
from api.home_router import router as home_router
from api.leads_router import router as leads_router
from api.messaging_router import router as messaging_router
from api.newsletter_router import router as newsletter_router
from api.sitemap_router import router as sitemap_router
from api.users_router import router as users_router
from core.dependencies import container
from core.error_handlers import register_error_handlers
from core.logging_config import LoggingConfigurator
from services.blog_scheduler import register_blog_jobs

LoggingConfigurator().configure()

logger = logging.getLogger("webnest.startup")

# Resolved from this file's own location (not the process cwd) so the mount
# works the same whether uvicorn is launched from the repo root or elsewhere.
ASSETS_DIR = Path(__file__).resolve().parent / "assets"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await container.database.create_all()
    except SQLAlchemyError:
        logger.critical(
            "Could not connect to the database at startup. Check SUPABASE_URL in .env.", exc_info=True
        )
        raise

    try:
        register_blog_jobs(container.scheduler, container.database, container.settings)
        container.scheduler.start()
    except Exception:
        # The blog scheduler is a nice-to-have background feature - a bug in
        # registering/starting it must never prevent the API itself from
        # coming up and serving requests.
        logger.critical("Could not start the blog generation scheduler", exc_info=True)

    yield

    try:
        container.scheduler.shutdown(wait=False)
    except Exception:
        logger.warning("Error while shutting down the blog generation scheduler", exc_info=True)
    try:
        await container.storage_service.aclose()
    except Exception:
        logger.warning("Error while closing the storage service HTTP client", exc_info=True)
    try:
        await container.database.dispose()
    except SQLAlchemyError:
        logger.warning("Error while disposing the database engine on shutdown", exc_info=True)


class WebNestStudioApp:
    """Assembles the FastAPI application: middleware, routers and error handlers."""

    def __init__(self) -> None:
        self.instance = FastAPI(
            title="WebNest Studio API",
            version="1.0.0",
            lifespan=lifespan,
        )
        self._configure_middleware()
        self._register_routers()
        register_error_handlers(self.instance)

    def _configure_middleware(self) -> None:
        self.instance.add_middleware(
            CORSMiddleware,
            allow_origins=container.settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def _register_routers(self) -> None:
        for router in (
            auth_router,
            leads_router,
            newsletter_router,
            content_router,
            home_router,
            admin_router,
            client_router,
            sitemap_router,
            generation_router,
            chat_router,
            coding_router,
            messaging_router,
            users_router,
        ):
            self.instance.include_router(router)

        # Serves static brand assets (e.g. /assets/logo.png) referenced by
        # absolute URL from outside the app itself - transactional emails,
        # where a relative path or bundled frontend asset isn't reachable.
        self.instance.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")

        @self.instance.get("/health", tags=["health"])
        async def health_check() -> dict:
            return {"status": "ok"}


app = WebNestStudioApp().instance
