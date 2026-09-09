from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings, settings
from core.exceptions import UnauthorizedError
from core.security import JWTHandler, OtpGenerator, PasswordHasher
from database.models import User
from database.session import Database
from services.auth_service import AuthService
from services.blog_generation_service import BlogGenerationService
from services.blog_scheduler import create_scheduler
from services.blog_service import BlogService
from services.chat_orchestrator_service import ChatOrchestratorService
from services.chatbot_service import ChatbotService
from services.codelab_service import CodelabService
from services.coding_service import CodingService
from services.client_service import ClientService
from services.learning_service import LearningService
from services.email_service import EmailService
from services.faq_service import FaqService
from services.generation_service import GenerationService
from services.lead_service import LeadService
from services.messaging_service import MessagingService
from services.newsletter_service import NewsletterService
from services.plan_pdf_service import PlanPdfService
from services.storage_service import StorageService
from services.portfolio_service import PortfolioService
from services.service_catalog_service import ServiceCatalogService
from services.testimonial_service import TestimonialService

bearer_scheme = HTTPBearer(auto_error=False)


class DependencyContainer:
    """Wires shared singletons (settings, security helpers, database) once at app startup."""

    def __init__(self, app_settings: Settings) -> None:
        self.settings = app_settings
        self.database = Database(app_settings)
        self.password_hasher = PasswordHasher()
        self.jwt_handler = JWTHandler(app_settings)
        self.otp_generator = OtpGenerator(app_settings)
        self.email_service = EmailService(app_settings)
        self.storage_service = StorageService(app_settings)
        self.plan_pdf_service = PlanPdfService(app_settings)
        self.scheduler = create_scheduler()


container = DependencyContainer(settings)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async for session in container.database.get_session():
        yield session


def get_auth_service(session: AsyncSession = Depends(get_db_session)) -> AuthService:
    return AuthService(
        session=session,
        settings=container.settings,
        password_hasher=container.password_hasher,
        jwt_handler=container.jwt_handler,
        otp_generator=container.otp_generator,
    )


def get_lead_service(session: AsyncSession = Depends(get_db_session)) -> LeadService:
    return LeadService(session=session)


def get_email_service() -> EmailService:
    return container.email_service


def get_newsletter_service(session: AsyncSession = Depends(get_db_session)) -> NewsletterService:
    return NewsletterService(session=session)


def get_service_catalog_service(session: AsyncSession = Depends(get_db_session)) -> ServiceCatalogService:
    return ServiceCatalogService(session=session)


def get_portfolio_service(session: AsyncSession = Depends(get_db_session)) -> PortfolioService:
    return PortfolioService(session=session)


def get_testimonial_service(session: AsyncSession = Depends(get_db_session)) -> TestimonialService:
    return TestimonialService(session=session)


def get_faq_service(session: AsyncSession = Depends(get_db_session)) -> FaqService:
    return FaqService(session=session)


def get_blog_service(session: AsyncSession = Depends(get_db_session)) -> BlogService:
    return BlogService(session=session, settings=container.settings)


def get_blog_generation_service(session: AsyncSession = Depends(get_db_session)) -> BlogGenerationService:
    return BlogGenerationService(session=session, settings=container.settings)


def get_client_service(session: AsyncSession = Depends(get_db_session)) -> ClientService:
    return ClientService(session=session)


def get_generation_service(session: AsyncSession = Depends(get_db_session)) -> GenerationService:
    return GenerationService(session=session, settings=container.settings)


def get_chatbot_service(session: AsyncSession = Depends(get_db_session)) -> ChatbotService:
    return ChatbotService(session=session, settings=container.settings)


def get_chat_orchestrator_service(session: AsyncSession = Depends(get_db_session)) -> ChatOrchestratorService:
    return ChatOrchestratorService(session=session, settings=container.settings)


def get_coding_service(session: AsyncSession = Depends(get_db_session)) -> CodingService:
    return CodingService(session=session, settings=container.settings)


def get_codelab_service(session: AsyncSession = Depends(get_db_session)) -> CodelabService:
    return CodelabService(session=session)


def get_learning_service(session: AsyncSession = Depends(get_db_session)) -> LearningService:
    return LearningService(session=session)


def get_plan_pdf_service() -> PlanPdfService:
    return container.plan_pdf_service


def get_messaging_service(session: AsyncSession = Depends(get_db_session)) -> MessagingService:
    return MessagingService(
        session=session,
        settings=container.settings,
        storage_service=container.storage_service,
    )


def get_client_ip(request: Request) -> str | None:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        first_hop = forwarded_for.split(",")[0].strip()
        if first_hop:
            return first_hop
    if request.client:
        return request.client.host
    return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> User:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    try:
        return await auth_service.get_current_user(credentials.credentials)
    except UnauthorizedError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired access token") from exc


async def get_optional_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> User | None:
    if credentials is None or not credentials.credentials:
        return None
    try:
        return await auth_service.get_current_user(credentials.credentials)
    except (UnauthorizedError, ValueError):
        return None


class RoleChecker:
    """Dependency that enforces the current user has one of the allowed roles."""

    def __init__(self, allowed_roles: list[str]) -> None:
        self._allowed_roles = allowed_roles

    def __call__(self, user: User = Depends(get_current_user)) -> User:
        if user.role not in self._allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user


require_admin = RoleChecker(["admin"])
require_client = RoleChecker(["client", "admin"])
