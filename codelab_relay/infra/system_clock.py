import math
from datetime import datetime, time, timedelta, timezone

from codelab_relay.errors import RelayDisabled, RelayError


class SystemClock:
    def now(self) -> datetime:
        try:
            return datetime.now(timezone.utc)
        except (OverflowError, OSError) as exc:
            raise RelayDisabled(temporary=True) from exc


def next_utc_midnight(now: datetime) -> datetime:
    """When the daily quotas reset."""
    try:
        return datetime.combine(now.astimezone(timezone.utc).date() + timedelta(days=1), time(0), tzinfo=timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise RelayDisabled(temporary=True) from exc


def seconds_until_midnight(now: datetime) -> int:
    try:
        return max(1, math.ceil((next_utc_midnight(now) - now).total_seconds()))
    except RelayError:
        raise
    except (OverflowError, ValueError) as exc:
        raise RelayDisabled(temporary=True) from exc
