"""GET /api/codelab/relay/usage (section 2.2)."""

import logging
import uuid

from codelab_relay.errors import RelayDisabled, RelayError
from codelab_relay.protocols import Clock, RateLimiter
from codelab_relay.schemas import UsageResponse

logger = logging.getLogger("webnest.codelab_relay")


class UsageService:
    def __init__(self, limiter: RateLimiter, clock: Clock) -> None:
        self._limiter = limiter
        self._clock = clock

    async def for_user(self, user_id: uuid.UUID) -> UsageResponse:
        try:
            return UsageResponse(**await self._limiter.remaining(user_id, self._clock.now()))
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("usage lookup failed user=%s", user_id)
            raise RelayDisabled(temporary=True) from exc
