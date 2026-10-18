"""Shared service helpers."""

import functools
import logging

from exams.errors import ExamDomainError, ExamInternalError

logger = logging.getLogger("webnest.exams")
audit_logger = logging.getLogger("webnest.exams.audit")


def guarded(operation: str):
    """Plan rule 2 for every public service method: domain errors pass
    through; anything unexpected is logged with the operation name (never
    answers or tokens) and re-raised as a generic 500 domain error."""

    def decorate(method):
        @functools.wraps(method)
        async def wrapper(self, *args, **kwargs):
            try:
                return await method(self, *args, **kwargs)
            except ExamDomainError:
                raise
            except Exception as exc:
                logger.error("Exam operation failed: %s", operation, exc_info=True)
                raise ExamInternalError() from exc

        return wrapper

    return decorate


def audit(action: str, admin_id, **fields) -> None:
    """Structured audit line for admin actions (restore, void, enable, revoke, reissue)."""
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    audit_logger.info("exam_audit action=%s admin=%s %s", action, admin_id, details)
