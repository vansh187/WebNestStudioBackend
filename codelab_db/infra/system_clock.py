from datetime import datetime, timezone

from codelab_db.errors import Disabled


class SystemClock:
    def now(self) -> datetime:
        try:
            return datetime.now(timezone.utc)
        except (OverflowError, OSError) as exc:
            raise Disabled(temporary=True) from exc
