"""Per-connection request rate limiting (enterprise `connection_rate_limit`).

Hard-blocks agent data queries against a connection once a fixed per-window
threshold is crossed. The limit is scoped to the connection *globally* — all
users share a single budget per window.

Design notes:
  * There is no Redis / shared cache in the stack, and the app runs multi-
    replica, so counters live in Postgres (``ConnectionRateLimitCounter``),
    keyed by (connection, window, truncated-bucket). This mirrors how
    ``UsageCounter`` backs the monthly usage quotas.
  * Windows are *fixed* (start-of-minute / -hour / -day, UTC), not sliding.
    Cheap and good enough; a burst can straddle a boundary, which is the
    accepted trade-off for fixed windows.
  * Enforcement is increment-then-check: the current bucket is bumped
    atomically, then compared to the cap. Blocked requests still count toward
    the window — conservative and standard for fixed-window limiters.
  * A breach is written to the enterprise audit log **once per window** (on the
    under->over transition), so a throttled connection doesn't flood the log.
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.ee.license as ee_license
from app.models.connection import Connection
from app.models.connection_rate_limit_counter import ConnectionRateLimitCounter

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Raised when a connection's per-window request cap is exceeded.

    Propagates out of the query-execution wrapper like any other query error,
    which is exactly the hard-block behaviour we want: the query never runs.
    """

    def __init__(self, detail: str, *, connection_id: str, window: str, limit: int, used: int):
        super().__init__(detail)
        self.detail = detail
        self.connection_id = connection_id
        self.window = window
        self.limit = limit
        self.used = used


def _bucket_start(now: datetime, window: str) -> datetime:
    """Truncate ``now`` to the start of its window (UTC)."""
    if window == "minute":
        return now.replace(second=0, microsecond=0)
    if window == "hour":
        return now.replace(minute=0, second=0, microsecond=0)
    if window == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unknown rate-limit window: {window}")


class ConnectionRateLimitService:
    async def check_and_consume(
        self,
        db: AsyncSession,
        *,
        connection: Connection,
        user_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Count one request against ``connection`` and hard-block if over a cap.

        No-op unless the feature is licensed AND the connection has at least one
        positive per-window limit configured. Raises ``RateLimitExceeded`` when a
        window is exceeded (the narrowest exceeded window wins).
        """
        if not ee_license.has_feature("connection_rate_limit"):
            return
        windows = connection.rate_limit_windows
        if not windows:
            return

        now = datetime.utcnow()
        # Narrowest window first so the tightest cap is the one reported. If the
        # narrowest is already over, wider windows aren't incremented — a
        # request blocked at the minute cap shouldn't burn the daily budget.
        for window, _seconds in Connection.RATE_LIMIT_WINDOWS:
            limit = windows.get(window)
            if limit is None:
                continue
            bucket = _bucket_start(now, window)
            used = await self._increment(db, str(connection.id), window, bucket)
            if used > limit:
                # Log the breach once, on the under->over transition, to keep
                # the audit trail bounded while the window stays over.
                if used == limit + 1:
                    await self._audit_breach(db, connection, user_id, window, limit, used, metadata)
                await db.commit()
                raise RateLimitExceeded(
                    f"Connection '{connection.name}' exceeded its rate limit "
                    f"of {limit} requests per {window}.",
                    connection_id=str(connection.id),
                    window=window,
                    limit=limit,
                    used=used,
                )
        await db.commit()

    async def check_and_consume_by_id(
        self,
        db: AsyncSession,
        *,
        connection_id: str,
        user_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Load the connection then enforce. Skips the DB load entirely when the
        feature is unlicensed so community/unlicensed installs pay nothing."""
        if not ee_license.has_feature("connection_rate_limit"):
            return
        result = await db.execute(
            select(Connection).where(Connection.id == connection_id)
        )
        connection = result.scalar_one_or_none()
        if connection is None:
            return
        await self.check_and_consume(
            db, connection=connection, user_id=user_id, metadata=metadata,
        )

    async def check_and_consume_with_context(
        self,
        context,
        *,
        connection_id: str,
        metadata: Optional[dict] = None,
    ) -> None:
        """Enforce using a ``UsageLimitContext`` (the object the query wrapper
        already carries): opens its own session and enforces on that connection.
        No-op without a session maker."""
        if context is None or context.session_maker is None:
            return
        if not ee_license.has_feature("connection_rate_limit"):
            return
        async with context.session_maker() as db:
            await self.check_and_consume_by_id(
                db,
                connection_id=connection_id,
                user_id=getattr(context, "user_id", None),
                metadata=metadata,
            )

    async def _increment(
        self,
        db: AsyncSession,
        connection_id: str,
        window: str,
        bucket_start: datetime,
    ) -> int:
        """Atomically add 1 to the bucket's counter and return the new count.

        Single ``UPDATE ... SET count = count + 1``; if the row doesn't exist
        yet, insert it inside a SAVEPOINT so a losing race doesn't roll back
        counters already bumped for other windows in this same transaction.
        """
        update_stmt = (
            update(ConnectionRateLimitCounter)
            .where(
                ConnectionRateLimitCounter.connection_id == connection_id,
                ConnectionRateLimitCounter.window == window,
                ConnectionRateLimitCounter.bucket_start == bucket_start,
            )
            .values(count=ConnectionRateLimitCounter.count + 1)
        )
        result = await db.execute(update_stmt)
        if result.rowcount == 0:
            row = ConnectionRateLimitCounter(
                connection_id=connection_id,
                window=window,
                bucket_start=bucket_start,
                count=1,
            )
            try:
                async with db.begin_nested():
                    db.add(row)
            except IntegrityError:
                # Another request inserted the same bucket first — retry the UPDATE.
                await db.execute(update_stmt)

        count = await db.execute(
            select(ConnectionRateLimitCounter.count).where(
                ConnectionRateLimitCounter.connection_id == connection_id,
                ConnectionRateLimitCounter.window == window,
                ConnectionRateLimitCounter.bucket_start == bucket_start,
            )
        )
        return int(count.scalar_one_or_none() or 0)

    async def _audit_breach(
        self,
        db: AsyncSession,
        connection: Connection,
        user_id: Optional[str],
        window: str,
        limit: int,
        used: int,
        metadata: Optional[dict],
    ) -> None:
        """Record a rate-limit breach in the enterprise audit log.

        Best-effort — never let an audit failure mask the (intended) block.
        """
        try:
            from app.ee.audit.service import audit_service

            details = {
                "window": window,
                "limit": limit,
                "used": used,
                "connection_name": connection.name,
            }
            if metadata:
                if metadata.get("sql"):
                    details["sql"] = metadata.get("sql")
                if metadata.get("data_source_name"):
                    details["data_source_name"] = metadata.get("data_source_name")
            await audit_service.log(
                db,
                organization_id=str(connection.organization_id),
                action="connection.rate_limit_exceeded",
                user_id=user_id,
                resource_type="connection",
                resource_id=str(connection.id),
                details=details,
                commit=False,
            )
        except Exception:  # pragma: no cover - defensive
            logger.warning("Failed to write rate-limit audit log", exc_info=True)


connection_rate_limit_service = ConnectionRateLimitService()
