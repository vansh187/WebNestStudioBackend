import logging
import sys


class LoggingConfigurator:
    """Configures root logging once at process startup.

    Without this, Python's root logger defaults to WARNING with no handler,
    so INFO-level diagnostics (e.g. "OTP email sent to X") never reach
    Render's log stream at all - only warnings/errors would show, and even
    those rely on the interpreter's last-resort handler rather than a
    consistent, timestamped format.
    """

    def __init__(self, level: int = logging.INFO) -> None:
        self._level = level

    def configure(self) -> None:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
        )
        root_logger = logging.getLogger()
        root_logger.setLevel(self._level)
        root_logger.handlers = [handler]
