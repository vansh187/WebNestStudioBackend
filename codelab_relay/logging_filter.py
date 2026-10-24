"""Security fix 5: provider keys and query strings must never reach a log line.

The relay never logs headers, bodies or queries on purpose. This filter is the
safety net for anything that slips through: it masks key-shaped values and
Basic/Bearer credentials, and strips the query string from URLs of relay
hosts - httpx itself logs "HTTP Request: POST <full url>" at INFO."""

import logging
import re

_KEY = re.compile(r"\b(rzp|sk|rk|pk)_[A-Za-z0-9_]+")
_CREDENTIAL = re.compile(r"\b(basic|bearer)\s+[A-Za-z0-9+/=._~-]{6,}", re.IGNORECASE)
WITHHELD = "[relay log line withheld: it could not be masked]"
FILTERED_LOGGERS = ("webnest.codelab_relay", "httpx")


def mask_secrets(text: str) -> str:
    try:
        return _CREDENTIAL.sub(r"\1 ***", _KEY.sub(r"\1_***", text))
    except TypeError:
        return WITHHELD


class RelaySecretsFilter(logging.Filter):
    def __init__(self, relay_hosts: tuple[str, ...]) -> None:
        super().__init__()
        # Relay calls connect to the checked IP, so httpx logs the IP, not the host name.
        ip_hosts = r"\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:.]+\]"
        hosts = "|".join([*(re.escape(host) for host in relay_hosts), ip_hosts])
        self._query = re.compile(rf"(https?://(?:{hosts})(?::\d+)?[^\s?\"']*)\?[^\s\"']*", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            masked = mask_secrets(self._query.sub(r"\1", message))
            if masked != message:
                record.msg, record.args = masked, None
            return True
        except Exception:
            # Fail closed, and never raise from inside logging.
            record.msg, record.args = WITHHELD, None
            return True


def install_relay_log_filter(relay_hosts: tuple[str, ...]) -> None:
    try:
        for name in FILTERED_LOGGERS:
            target = logging.getLogger(name)
            for existing in [f for f in target.filters if isinstance(f, RelaySecretsFilter)]:
                target.removeFilter(existing)
            target.addFilter(RelaySecretsFilter(relay_hosts))
    except Exception:
        logging.getLogger("webnest.codelab_relay").critical("could not install the relay log filter", exc_info=True)
        raise
