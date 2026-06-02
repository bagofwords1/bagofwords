from datetime import datetime, timezone
from typing import Optional


def ensure_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Return ``value`` as a naive UTC datetime, suitable for storage.

    All timestamp columns in this app are ``TIMESTAMP WITHOUT TIME ZONE``
    (the base model stores ``created_at`` / ``updated_at`` via
    ``datetime.utcnow``). asyncpg rejects timezone-aware datetimes for those
    columns on PostgreSQL with ``can't subtract offset-naive and offset-aware
    datetimes``. Convert any aware value to UTC and drop the tzinfo so writes
    are consistent with the rest of the schema (a no-op for naive values and
    ``None``).
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value
