"""Domain errors, one class per row of the draft error table (B5.2) plus the
request-shape, kill-switch and registry errors. The router renders them as
{"detail", "code", **extra} with X-WebNest-DB-Error."""


class DbError(Exception):
    def __init__(self, message: str, code: str, http_status: int, extra: dict | None = None,
                 headers: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.extra = extra or {}
        self.headers = headers or {}


class BadRequest(DbError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"The request is not valid: {reason}", "db_bad_request", 400)


class BodyTooLarge(DbError):
    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"The request body is larger than {limit_bytes} bytes.", "db_payload_too_large", 413,
                         {"limit_bytes": limit_bytes})


class BadUrl(DbError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"The connection URL is not valid: {reason}", "db_bad_url", 400)


class LoginRequired(DbError):
    def __init__(self) -> None:
        super().__init__("Log in to WebNest to connect to a database from the playground.", "db_login_required", 401)


class HostNotAllowed(DbError):
    def __init__(self, host: str, reason: str = "is not a supported database host") -> None:
        super().__init__(f"{host} {reason}. See the playground's list of supported database providers.",
                         "db_host_not_allowed", 403, {"host": host})


class StatementBlocked(DbError):
    def __init__(self, what: str) -> None:
        super().__init__(f"{what} isn't allowed in the CodeLab database playground.",
                         "db_statement_blocked", 403, {"blocked": what})


class UserBlocked(DbError):
    def __init__(self) -> None:
        super().__init__("Your account can't use the CodeLab database proxy. Contact support if this is a mistake.",
                         "db_user_blocked", 403)


class QueryTimeout(DbError):
    def __init__(self, timeout_seconds: int) -> None:
        super().__init__(f"The query ran longer than {timeout_seconds} seconds and was stopped.",
                         "db_timeout", 408, {"timeout_seconds": timeout_seconds})


class ResultTooLarge(DbError):
    """Carries the part of the result that fit, with truncated: true."""

    def __init__(self, max_rows: int, max_bytes: int, partial: dict) -> None:
        super().__init__(
            f"The result is larger than {max_rows} rows or {max_bytes} bytes, so only the first part is shown. "
            "Add a LIMIT or a filter. Any change made by this statement was not saved.",
            "db_result_too_large", 413, {**partial, "truncated": True, "max_rows": max_rows, "max_bytes": max_bytes})


class RateLimited(DbError):
    def __init__(self, retry_after: int, scope: str, limit_text: str) -> None:
        super().__init__(f"Database proxy limit reached: {limit_text}. Try again in {retry_after} seconds.",
                         "db_rate_limited", 429, {"retry_after": retry_after, "scope": scope},
                         {"Retry-After": str(retry_after)})


class Busy(DbError):
    """Too many queries are running at once across all learners."""

    def __init__(self) -> None:
        super().__init__("The database proxy is busy. Try again in a few seconds.", "db_rate_limited", 429,
                         {"retry_after": 2, "scope": "busy"}, {"Retry-After": "2"})


class ConnectFailed(DbError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"Could not connect to the database: {reason}", "db_connect_failed", 502)


class QueryError(DbError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason, "db_query_error", 422)


class Disabled(DbError):
    def __init__(self, temporary: bool = False, reason: str | None = None) -> None:
        message = reason or ("The CodeLab database proxy is unavailable right now. Try again in a moment." if temporary
                             else "The CodeLab database proxy is switched off right now.")
        super().__init__(message, "db_disabled", 503)


class RegistryInvalid(DbError):
    """hosts.json failed validation. Raised at import, so the app refuses to
    start instead of connecting with a broken allowlist."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Invalid CodeLab database host allowlist: {reason}", "db_disabled", 503)
