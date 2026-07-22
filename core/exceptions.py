class DomainError(Exception):
    """Base class for service-layer errors that routers translate to HTTP responses."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    pass


class ConflictError(DomainError):
    pass


class UnauthorizedError(DomainError):
    pass


class ForbiddenError(DomainError):
    pass


class ValidationError(DomainError):
    pass


class DatabaseError(DomainError):
    """Raised when a database operation fails for reasons outside the caller's control
    (connection drop, constraint violation not otherwise mapped, timeout, etc.)."""


class RateLimitedError(DomainError):
    """Raised when a caller must wait before retrying (e.g. OTP resend cooldown)."""
