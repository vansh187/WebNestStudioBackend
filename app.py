import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from api.admin_router import router as admin_router
from api.auth_router import router as auth_router
from api.chat_router import router as chat_router
from api.client_router import router as client_router
from api.codelab_router import admin_router as codelab_admin_router
from api.codelab_router import router as codelab_router
from api.coding_router import router as coding_router
from api.content_router import router as content_router
from api.generation_router import router as generation_router
from api.github_router import router as github_router
from api.home_router import router as home_router
from api.java_playground_router import router as java_playground_router
from api.leads_router import router as leads_router
from api.learning_router import admin_router as learning_admin_router
from api.learning_router import progress_router as learning_progress_router
from api.learning_router import router as learning_router
from api.messaging_router import router as messaging_router
from api.newsletter_router import router as newsletter_router
from api.sitemap_router import router as sitemap_router
from api.users_router import router as users_router
from codelab_relay import models as relay_models  # noqa: F401 - registers the relay quota tables for create_all
from codelab_relay.cors import RelayAwareCORSMiddleware
from codelab_relay.dependencies import close_relay_resources, relay_cors_options
from codelab_relay.jobs import register_relay_jobs
from codelab_relay.router import register_relay_error_handlers
from codelab_relay.router import router as relay_router
from core.dependencies import container
from core.error_handlers import register_error_handlers
from core.logging_config import LoggingConfigurator
from core.request_limits import GitHubBodyLimitMiddleware
from exams import models as exam_models  # noqa: F401 - registers the exam tables for create_all
from exams.jobs import register_exam_jobs
from exams.router_admin import router as exam_admin_router
from exams.router_learner import router as exam_learner_router
from exams.router_public import router as exam_public_router
from exams.schema_upgrade import SchemaUpgradeError, upgrade_schema
from services.blog_scheduler import register_blog_jobs
from services.codelab_scheduler import register_codelab_jobs

LoggingConfigurator().configure()

logger = logging.getLogger("webnest.startup")

# Resolved from this file's own location (not the process cwd) so the mount
# works the same whether uvicorn is launched from the repo root or elsewhere.
ASSETS_DIR = Path(__file__).resolve().parent / "assets"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # Must run before create_all: it adds the exam columns/keys to existing
        # tables that the new ORM models and exam foreign keys rely on. If it
        # fails, startup fails and Render keeps the previous deploy serving.
        await upgrade_schema(container.database.engine)
    except SchemaUpgradeError:
        logger.critical("Could not bring the database schema up to date for course exams", exc_info=True)
        raise

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

    try:
        # Jobs can be added to a running scheduler; registered separately so
        # a problem here can never take the blog jobs down with it.
        register_codelab_jobs(container.scheduler, container.database, container.settings)
    except Exception:
        logger.critical("Could not register the CodeLab generation job", exc_info=True)

    try:
        register_exam_jobs(container.scheduler, container.database, container.settings)
    except Exception:
        logger.critical("Could not register the course exam jobs", exc_info=True)

    try:
        register_relay_jobs(container.scheduler, container.database)
    except Exception:
        logger.critical("Could not register the CodeLab relay cleanup job", exc_info=True)

    yield

    await close_relay_resources()
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
        register_relay_error_handlers(self.instance)

    def _configure_middleware(self) -> None:
        # Added before CORS so CORS wraps it and its 413 still reaches the browser.
        self.instance.add_middleware(GitHubBodyLimitMiddleware)
        # /api/codelab/relay/* gets the relay's strict policy (explicit
        # origins and headers, no credentials); every other route keeps this one.
        self.instance.add_middleware(
            RelayAwareCORSMiddleware,
            relay_options=relay_cors_options(),
            default_options=dict(
                allow_origins=container.settings.cors_origin_list,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            ),
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
            java_playground_router,
            codelab_router,
            codelab_admin_router,
            learning_router,
            learning_admin_router,
            learning_progress_router,
            exam_learner_router,
            exam_public_router,
            exam_admin_router,
            github_router,
            relay_router,
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
