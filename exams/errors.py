"""Exam domain errors (plan section 5). Each maps to one HTTP status and the
contract's machine-readable `code`; `extra` fields are merged into the body:
{"detail": "...", "code": "...", **extra}. Messages are written for learners
and never carry answers, tokens or stack traces."""

from core.exceptions import DomainError


class ExamDomainError(DomainError):
    status_code = 500
    code = "internal_error"
    default_message = "Something went wrong with the exam service. Please try again."

    def __init__(self, message: str | None = None, **extra) -> None:
        super().__init__(message or self.default_message)
        self.extra = extra


class CourseNotFound(ExamDomainError):
    status_code = 404
    code = "course_not_found"
    default_message = "Course not found."


class AttemptNotFound(ExamDomainError):
    status_code = 404
    code = "attempt_not_found"
    default_message = "Exam attempt not found."


class CertificateNotFound(ExamDomainError):
    status_code = 404
    code = "certificate_not_found"
    default_message = "Certificate not found."


class QuestionNotFound(ExamDomainError):
    status_code = 404
    code = "question_not_found"
    default_message = "Question not found."


class JobNotFound(ExamDomainError):
    status_code = 404
    code = "job_not_found"
    default_message = "Generation job not found."


class ExamDisabled(ExamDomainError):
    status_code = 409
    code = "exam_disabled"
    default_message = "The final exam is not available for this course."


class NotEligible(ExamDomainError):
    """extra: reason (+ cooldown_until / lessons_remaining when relevant)."""

    status_code = 409
    code = "not_eligible"
    default_message = "You can't start this exam yet."


class AttemptClosed(ExamDomainError):
    status_code = 409
    code = "attempt_closed"
    default_message = "This exam attempt is closed."


class AttemptLost(ExamDomainError):
    status_code = 409
    code = "attempt_lost"
    default_message = (
        "This attempt's questions could not be recovered after a server problem. "
        "It doesn't count as a fail - start the exam again for a new set of questions."
    )


class PoolNotReady(ExamDomainError):
    status_code = 409
    code = "pool_not_ready"
    default_message = "The exam isn't ready yet. Please try again later."

    def __init__(self, message: str | None = None, retry_after: int = 600, **extra) -> None:
        super().__init__(message, retry_after=retry_after, **extra)


class ExamConflict(ExamDomainError):
    status_code = 409
    code = "conflict"
    default_message = "This change conflicts with the current state."


class CertificateRevoked(ExamDomainError):
    status_code = 410
    code = "certificate_revoked"
    default_message = "This certificate has been revoked."


class ExamValidationError(ExamDomainError):
    """422 with the contract's flattened shape: detail is a plain string and
    errors is [{msg}]."""

    status_code = 422
    code = "validation_error"
    default_message = "Request validation failed."

    def __init__(self, message: str | None = None, **extra) -> None:
        message = message or self.default_message
        super().__init__(message, errors=extra.pop("errors", None) or [{"msg": message}], **extra)


class RateLimited(ExamDomainError):
    status_code = 429
    code = "rate_limited"
    default_message = "Too many requests. Please wait a moment and try again."

    def __init__(self, retry_after: int, message: str | None = None) -> None:
        super().__init__(message, retry_after=max(1, int(retry_after)))


class GenerationFailed(ExamDomainError):
    status_code = 502
    code = "generation_failed"
    default_message = "Question generation failed. Please try again later."


class ExamStoreUnavailable(ExamDomainError):
    status_code = 503
    code = "service_unavailable"
    default_message = "The exam service is temporarily unavailable. Please try again in a moment."


class CrossCourseQuestion(ExamDomainError):
    """A drawn question does not belong to the examined course - a bug guard.
    The learner sees a generic 500; details go to the log only."""

    status_code = 500
    code = "internal_error"


class ExamInternalError(ExamDomainError):
    status_code = 500
    code = "internal_error"


# --------------------------------------------------------------------------- #
# Internal signals between stores/clients and services. Services always catch
# these; they never reach a router.
# --------------------------------------------------------------------------- #
class ExamSignal(Exception):
    pass


class ActiveAttemptExists(ExamSignal):
    """The partial unique index refused a second active attempt."""


class JobAlreadyRunning(ExamSignal):
    """The course already has a running refill job."""


class DuplicateQuestion(ExamSignal):
    """The course already holds a non-rejected question with this prompt hash."""


class CertificateIdTaken(ExamSignal):
    """The random certificate ID collided with an existing one."""


class ValidCertificateExists(ExamSignal):
    """The user already holds a valid certificate for the course, or the
    attempt already has one."""


class QuotaExhausted(ExamSignal):
    """A provider refused the call for quota reasons (HTTP 429 /
    RESOURCE_EXHAUSTED / daily limit). Pauses refills; never retried hot."""

    def __init__(self, provider: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.retry_after = retry_after


class ProviderFailure(ExamSignal):
    """A provider call failed (timeout, 5xx, bad response shape)."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider
