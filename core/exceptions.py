class DomainError(Exception):
    """Base class for service-layer errors that routers translate to HTTP responses."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    pass


class BadRequestError(DomainError):
    """Raised when a request is well-formed but semantically invalid (unknown
    referenced id, self-referential action, cross-resource mismatch) - maps to 400."""


class ConflictError(DomainError):
    pass


class UnauthorizedError(DomainError):
    pass


class ForbiddenError(DomainError):
    pass


class ValidationError(DomainError):
    pass


class PayloadTooLargeError(DomainError):
    pass


class UnsupportedMediaTypeError(DomainError):
    """Raised when an uploaded/referenced file's MIME type is not on the allowlist - maps to 415."""


class ExternalServiceError(DomainError):
    """Raised when a required third-party service (e.g. Supabase Storage) is
    unreachable or returns an unusable response - maps to 503."""


class DatabaseError(DomainError):
    """Raised when a database operation fails for reasons outside the caller's control
    (connection drop, constraint violation not otherwise mapped, timeout, etc.)."""


class RateLimitedError(DomainError):
    """Raised when a caller must wait before retrying (e.g. OTP resend cooldown)."""


class InvalidPromptError(DomainError):
    """Raised when a prompt/refinement fails basic shape validation (empty, too long)."""


class ContentRejectedError(DomainError):
    """Raised when a prompt/refinement is blocked by the content filter."""


class GenerationUnavailableError(DomainError):
    """Raised when every configured LLM provider failed to produce a result."""
