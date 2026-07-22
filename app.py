import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError

from api.admin_router import router as admin_router
from api.auth_router import router as auth_router
from api.client_router import router as client_router
from api.content_router import router as content_router
from api.home_router import router as home_router
from api.leads_router import router as leads_router
from api.newsletter_router import router as newsletter_router
from core.dependencies import container
from core.error_handlers import register_error_handlers

logger = logging.getLogger("webnest.startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await container.database.create_all()
    except SQLAlchemyError:
        logger.critical(
            "Could not connect to the database at startup. Check SUPABASE_URL in .env.", exc_info=True
        )
        raise
    yield
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
        ):
            self.instance.include_router(router)

        @self.instance.get("/health", tags=["health"])
        async def health_check() -> dict:
            return {"status": "ok"}


app = WebNestStudioApp().instance
