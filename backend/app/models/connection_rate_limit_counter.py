from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class ConnectionRateLimitCounter(BaseSchema):
    """Fixed-window request counter backing the per-connection rate limit
    (enterprise `connection_rate_limit`).

    One row per (connection, window granularity, window bucket). `bucket_start`
    is "now" truncated to the window (start of the minute / hour / day, UTC), so
    all requests landing in the same window increment the same row. Counters are
    never reset — a new window simply starts a new row — so stale rows for past
    windows accumulate; a periodic cleanup can prune `bucket_start` older than a
    day without affecting enforcement.

    The rate limit is connection-global (not per user), so there is no user_id
    here. Postgres is the only shared store in the stack (no Redis), hence a DB
    table rather than an in-memory token bucket.
    """
    __tablename__ = "connection_rate_limit_counters"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "window", "bucket_start",
            name="uq_conn_rate_limit_window_bucket",
        ),
        Index("ix_conn_rate_limit_lookup", "connection_id", "window", "bucket_start"),
    )

    connection_id = Column(
        String(36),
        ForeignKey("connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    window = Column(String(16), nullable=False)   # minute | hour | day
    bucket_start = Column(DateTime, nullable=False)  # window start, truncated (UTC)
    count = Column(Integer, nullable=False, default=0)

    connection = relationship("Connection")
