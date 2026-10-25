"""Reads the request body with a size cap, streamed, without trusting
Content-Length (shared with the relay)."""

import logging

from fastapi import Request

from codelab_db.constants import LOGGER_NAME
from codelab_db.errors import BadRequest, BodyTooLarge, DbError
from core.playground_requests import read_capped_body

logger = logging.getLogger(LOGGER_NAME)


def _unreadable() -> BadRequest:
    try:
        return BadRequest("the body could not be read")
    except Exception as exc:
        raise DbError("The request could not be read.", "db_bad_request", 400) from exc


class BodyReader:
    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes

    async def read(self, request: Request) -> bytes:
        try:
            body = await read_capped_body(request, self._max_bytes, DbError, BodyTooLarge, _unreadable)
            if not body:
                raise BadRequest("the body is empty")
            return body
        except DbError:
            raise
        except Exception as exc:
            logger.warning("could not read a database proxy body type=%s", type(exc).__name__)
            raise BadRequest("the body could not be read") from exc
