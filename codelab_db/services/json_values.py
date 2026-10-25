"""Turns driver values into JSON and enforces the row and byte caps.

Postgres/MySQL: dates and times → ISO 8601 strings, Decimal → string (no
precision lost), bytes → "\\x" + hex (the psql display form), UUID / network
types / ranges / intervals → their text form, NaN and infinities → strings.
Lists and JSON documents keep their structure."""

import json
import logging
import math
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from codelab_db.constants import LOGGER_NAME
from codelab_db.errors import DbError, QueryError, ResultTooLarge

logger = logging.getLogger(LOGGER_NAME)

MAX_VALUE_DEPTH = 32


def to_json_value(value, depth: int = 0):
    try:
        if value is None or isinstance(value, (bool, int, str)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, timedelta):
            return str(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "\\x" + bytes(value).hex()
        if isinstance(value, uuid.UUID):
            return str(value)
        if depth >= MAX_VALUE_DEPTH:
            return str(value)
        if isinstance(value, dict):
            return {str(key): to_json_value(child, depth + 1) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [to_json_value(child, depth + 1) for child in value]
        if isinstance(value, (set, frozenset)):
            return sorted(str(child) for child in value)
        return str(value)
    except Exception as exc:
        logger.warning("a result value could not be converted type=%s", type(value).__name__)
        raise QueryError("A value in the result could not be converted to JSON.") from exc


def json_size(value) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise QueryError("A value in the result could not be converted to JSON.") from exc


class RowCollector:
    """Converts rows one at a time and stops at the row or byte cap."""

    def __init__(self, max_rows: int, max_bytes: int) -> None:
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.rows: list = []
        self.size = 2
        self.truncated = False

    def add(self, row) -> bool:
        """False once the result is too large (the row is not kept)."""
        try:
            if len(self.rows) >= self.max_rows:
                self.truncated = True
                return False
            converted = to_json_value(row)
            size = json_size(converted) + 1
            if self.size + size > self.max_bytes:
                self.truncated = True
                return False
            self.size += size
            self.rows.append(converted)
            return True
        except DbError:
            raise
        except Exception as exc:
            raise QueryError("The result could not be read.") from exc

    def too_large(self, partial: dict) -> ResultTooLarge:
        try:
            return ResultTooLarge(self.max_rows, self.max_bytes, partial)
        except Exception as exc:
            raise QueryError("The result is too large.") from exc
