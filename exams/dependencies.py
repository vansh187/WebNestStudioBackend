"""The only place exam services are assembled from concrete classes. Process
singletons (in-memory attempt cache, rate limiter, PDF cache, PDF renderer)
live at module level; everything that needs the database is built per request
(or per background run in exams/jobs.py) on one session."""

import random

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.dependencies import bearer_scheme, container, get_auth_service, get_db_session
from core.exceptions import UnauthorizedError
from database.models import User
from exams.infra.attempt_cache import LayeredAttemptCache, MemoryAttemptCache, SqlAttemptCache
from exams.infra.code_runner import ExamCodeRunner
from exams.infra.llm_client import ExamLlmClient, GeminiEmbedder
from exams.infra.pdf_renderer import CertificatePdfRenderer
from exams.infra.sql_attempt_store import SqlAttemptStore
from exams.infra.sql_base import SqlUnitOfWork
from exams.infra.sql_certificate_store import SqlCertificateStore, SqlReportStore
from exams.infra.sql_course_reader import SqlCourseReader, SqlProgressReader
from exams.infra.sql_generation_store import SqlContextStore, SqlGenerationLogStore, SqlJobStore
from exams.infra.sql_question_store import SqlQuestionStore
from exams.infra.system import (
    EmailAlertSender,
    SecretsIdGenerator,
    SlidingWindowRateLimiter,
    SystemClock,
    SystemRandomSource,
)
from exams.services.attempt_lifecycle import AttemptLifecycle
from exams.services.attempt_service import AttemptService
from exams.services.certificate_service import CertificateService, PdfCache
from exams.services.eligibility_service import EligibilityService
from exams.services.generation_service import GenerationService
from exams.services.maintenance_service import MaintenanceService
from exams.services.pool_service import PoolService
from exams.services.question_admin_service import QuestionAdminService
from exams.services.verification import VerificationPipeline
from services.auth_service import AuthService

memory_cache = MemoryAttemptCache(max_entries=500)
rate_limiter = SlidingWindowRateLimiter()
pdf_cache = PdfCache(max_entries=64)
pdf_renderer = CertificatePdfRenderer()
clock = SystemClock()
ids = SecretsIdGenerator()
random_source = SystemRandomSource()


class ExamServices:
    """Everything the exam routers and jobs need, on one session."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.settings = settings
        self.uow = SqlUnitOfWork(session)
        self.courses = SqlCourseReader(session)
        self.progress = SqlProgressReader(session)
        self.questions = SqlQuestionStore(session)
        self.attempts = SqlAttemptStore(session)
        self.certificate_store = SqlCertificateStore(session)
        self.reports = SqlReportStore(session)
        self.contexts = SqlContextStore(session)
        self.jobs = SqlJobStore(session)
        self.logs = SqlGenerationLogStore(session)
        self.cache = LayeredAttemptCache(memory_cache, SqlAttemptCache(session), clock)
        self.llm = ExamLlmClient(settings)

        self.certificates = CertificateService(
            self.certificate_store, self.uow, clock, ids, pdf_renderer, pdf_cache, rate_limiter, settings
        )
        self.pool = PoolService(self.courses, self.questions, self.uow, clock, settings)
        self.lifecycle = AttemptLifecycle(
            self.attempts, self.cache, self.courses, self.certificates, self.uow, clock, settings
        )
        self.eligibility = EligibilityService(
            self.courses, self.pool, self.lifecycle, self.attempts, self.certificate_store, self.progress, clock, settings
        )

    def attempt_service(self) -> AttemptService:
        return AttemptService(
            self.courses, self.questions, self.attempts, self.cache, self.certificate_store, self.reports,
            self.pool, self.eligibility, self.lifecycle, self.uow, clock, ids, random_source, rate_limiter,
            self.settings,
        )  # fmt: skip

    def pipeline(self) -> VerificationPipeline:
        return VerificationPipeline(
            self.questions, self.logs, self.llm, GeminiEmbedder(self.settings), ExamCodeRunner(self.settings),
            clock, random.SystemRandom(), self.settings,
        )  # fmt: skip

    def admin_service(self) -> QuestionAdminService:
        return QuestionAdminService(
            self.courses, self.questions, self.attempts, self.reports, self.contexts, self.jobs, self.logs,
            self.pool, self.pipeline(), self.certificates, self.uow, clock, self.settings,
        )  # fmt: skip

    def generation_service(self) -> GenerationService:
        return GenerationService(
            self.courses, self.questions, self.contexts, self.jobs, self.pool, self.pipeline(), self.llm,
            self.uow, clock, self.settings,
        )  # fmt: skip

    def maintenance_service(self) -> MaintenanceService:
        return MaintenanceService(
            self.courses, self.questions, self.attempts, self.cache, self.jobs, self.pool, self.lifecycle,
            EmailAlertSender(container.email_service), self.uow, clock, self.settings,
        )  # fmt: skip


def get_exam_services(session: AsyncSession = Depends(get_db_session)) -> ExamServices:
    return ExamServices(session, container.settings)


def get_attempt_service(services: ExamServices = Depends(get_exam_services)) -> AttemptService:
    return services.attempt_service()


def get_eligibility_service(services: ExamServices = Depends(get_exam_services)) -> EligibilityService:
    return services.eligibility


def get_certificate_service(services: ExamServices = Depends(get_exam_services)) -> CertificateService:
    return services.certificates


def get_admin_service(services: ExamServices = Depends(get_exam_services)) -> QuestionAdminService:
    return services.admin_service()


async def get_optional_user_strict(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> User | None:
    """Contract rule 9: no Authorization header -> anonymous; an expired or
    invalid token -> 401 (so the frontend refreshes and retries)."""
    if credentials is None or not credentials.credentials:
        return None
    try:
        return await auth_service.get_current_user(credentials.credentials)
    except (UnauthorizedError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired access token") from exc
