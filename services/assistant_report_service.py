"""Members reporting an AI reply, and admins reviewing the reports, as Google Play's
AI-generated content policy requires. Logs ids and reasons only, never the reported text."""

import logging
import uuid
from dataclasses import asdict

from core.exceptions import NotFoundError, RateLimitedError
from database.assistant_report_persistence import AnsweredBy, AssistantReportStore, NewAssistantReport, SavedAssistantReport
from schemas.assistant_schemas import AssistantReportCreate, AssistantReportCreated, AssistantReportOut
from services.assistant_llm import AssistantProvider
from services.user_rate_limiter import UserRateLimiter

logger = logging.getLogger("webnest.assistant.reports")

REPORTS_PER_HOUR = 20
MAX_LISTED_REPORTS = 200


def _out(report: SavedAssistantReport) -> AssistantReportOut:
    return AssistantReportOut(**asdict(report))


class AssistantReportService:
    def __init__(self, store: AssistantReportStore, limiter: UserRateLimiter, provider: AssistantProvider | None) -> None:
        self._store = store
        self._limiter = limiter
        # Only a stand-in for the call log. Reviewers see which model answered; members never do.
        self._provider = provider

    async def report(self, user_id: uuid.UUID, payload: AssistantReportCreate) -> AssistantReportCreated:
        if not self._limiter.allow(user_id):
            raise RateLimitedError("You have sent a lot of reports. Please try again later.")
        answered = await self._answered_by(user_id)
        report_id = await self._store.create(
            NewAssistantReport(
                user_id=user_id,
                reason=payload.reason,
                details=payload.details,
                reply_text=payload.reply_text,
                prompt_text=payload.prompt_text,
                client_message_id=payload.client_message_id,
                provider_name=answered.provider if answered is not None else None,
                model=answered.model if answered is not None else None,
            )
        )
        logger.info("AI reply reported: report=%s user=%s reason=%s", report_id, user_id, payload.reason)
        return AssistantReportCreated(id=report_id, status="open")

    async def _answered_by(self, user_id: uuid.UUID) -> AnsweredBy | None:
        """Who wrote the member's latest reply, from the call log. The configured provider is
        only a stand-in when that has no row, such as when the log was cleared."""
        answered = await self._store.latest_answer(user_id)
        if answered is None and self._provider is not None:
            return AnsweredBy(self._provider.name, self._provider.model)
        return answered

    async def list_reports(self, status: str) -> list[AssistantReportOut]:
        return [_out(report) for report in await self._store.list_by_status(status, MAX_LISTED_REPORTS)]

    async def resolve(self, report_id: uuid.UUID, admin_id: uuid.UUID) -> AssistantReportOut:
        report = await self._store.resolve(report_id, admin_id)
        if report is None:
            raise NotFoundError("Report not found.")
        logger.info("AI reply report resolved: report=%s admin=%s", report_id, admin_id)
        return _out(report)
