"""Credentials never leave in an error message or a log line (B5.3): the
password (raw and percent-encoded), the user:password@ part of any URL, and
anything longer than a few lines are removed from database messages."""

import logging
import re
from urllib.parse import quote, quote_plus

from codelab_db.constants import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)

MAX_MESSAGE_LENGTH = 500
MIN_SECRET_LENGTH = 4   # shorter values would mangle the message more than they protect
_URL_CREDENTIALS = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s@]*@", re.IGNORECASE)
_PASSWORD_ASSIGNMENT = re.compile(r"(password\s*[=:]\s*)\S+", re.IGNORECASE)
WITHHELD = "The database returned an error that could not be shown."


def redact(message: str, password: str | None = None) -> str:
    try:
        text = str(message)
        if password and len(password) >= MIN_SECRET_LENGTH:
            for form in {password, quote(password, safe=""), quote_plus(password)}:
                text = text.replace(form, "***")
        text = _URL_CREDENTIALS.sub(r"\1***@", text)
        text = _PASSWORD_ASSIGNMENT.sub(r"\1***", text)
        lines = [line.rstrip() for line in text.strip().splitlines() if line.strip()][:4]
        text = "\n".join(lines)
        return text[:MAX_MESSAGE_LENGTH] + ("…" if len(text) > MAX_MESSAGE_LENGTH else "")
    except Exception:
        logger.warning("a database message could not be redacted")
        return WITHHELD


def _mask(text: str) -> str:
    try:
        return _PASSWORD_ASSIGNMENT.sub(r"\1***", _URL_CREDENTIALS.sub(r"\1***@", text))
    except TypeError:
        return text


class CredentialLogFilter(logging.Filter):
    """Safety net: masks URL credentials and password= assignments in the
    message and in any traceback text of a record."""

    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            masked = _mask(message)
            if masked != message:
                record.msg, record.args = masked, None
            if record.exc_info and not record.exc_text:
                record.exc_text = self._formatter.formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = _mask(record.exc_text)
            return True
        except Exception:
            record.msg, record.args = "[log line withheld: it could not be masked]", None
            record.exc_info, record.exc_text = None, None
            return True


FILTERED_LOGGERS = (LOGGER_NAME, "psycopg", "pymysql", "pymongo")


def _replace_filter(target) -> None:
    try:
        for existing in [f for f in target.filters if isinstance(f, CredentialLogFilter)]:
            target.removeFilter(existing)
        target.addFilter(CredentialLogFilter())
    except Exception:
        logger.critical("could not attach the credential log filter")
        raise


def install_credential_log_filter() -> None:
    """Logger filters only see records logged on that exact logger, so the
    filter also goes on every root handler - those see records from every
    logger, children such as pymongo.connection included. Called at import
    and again at startup, after the app has set up its handlers."""
    try:
        for name in FILTERED_LOGGERS:
            _replace_filter(logging.getLogger(name))
        for handler in logging.getLogger().handlers:
            _replace_filter(handler)
    except Exception:
        logger.critical("could not install the database credential log filter", exc_info=True)
        raise
