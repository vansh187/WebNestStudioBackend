from datetime import datetime, timezone


def as_aware(value: datetime | None) -> datetime | None:
    """Coerce a possibly-naive datetime to a UTC-aware one; pass ``None`` through.

    asyncpg returns timezone-aware datetimes for ``timestamptz`` columns, but a
    value that originated in Python (or a mixed source) can still be naive.
    Comparing a naive and an aware datetime raises ``TypeError`` at runtime, so
    every cross-source comparison funnels through here first.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
