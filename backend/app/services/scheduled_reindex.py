"""Scheduled schema auto-reload sweeper.

Periodically re-indexes connection schemas so tables stay fresh without a
manual reindex — the schema-side counterpart to the QVD/PBIRS cache warmups.

Design (see docs/design discussion in the PR):

  * Decoupled tick vs. work. A frequent, cheap sweep selects only the
    connections whose schema is *due* (stale past their per-connection
    interval) and kicks a bounded batch of them. Steady-state load is
    O(N / interval), not O(N) per tick, so it stays flat at hundreds of
    connections.

  * Bounded batch. At most `BOW_REINDEX_SWEEP_BATCH` connections are kicked
    per tick (oldest-synced first), so a burst of newly-due connections can
    never become a thundering herd against upstream sources.

  * Idempotent + deduped. The sweep tick is claimed via `claim_scheduled_run`
    (one worker/replica per fire) and each connection goes through
    `ConnectionIndexingService.start`, which is itself idempotent (returns the
    in-flight row if an index is already running).

  * Failure backoff / heal-on-login. Before kicking, we stamp
    `next_retry_at = now + interval`, so a connection that fails (or is skipped
    because a user_required source has no system creds) is not re-kicked every
    tick. user_required per-user catalogs heal on the next user login instead.
    A successful index clears `next_retry_at` and advances `last_synced_at`
    (both gate re-selection).

  * Enterprise-gated. No-ops entirely unless the `scheduled_reindex` license
    feature is active.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

from sqlalchemy import select

logger = logging.getLogger(__name__)

SWEEP_JOB_ID = "schema_reindex_sweep"

# Max connections kicked per sweep tick. Caps concurrent upstream load; the
# rest roll to the next tick. Generous default — steady-state due-count per
# tick is N/(interval/sweep_period), far below this for typical deployments.
_DEFAULT_BATCH = 10


def _batch_size() -> int:
    try:
        return max(1, int(os.environ.get("BOW_REINDEX_SWEEP_BATCH", _DEFAULT_BATCH)))
    except (TypeError, ValueError):
        return _DEFAULT_BATCH


def _is_due(connection, now: datetime, tz=None) -> bool:
    """True if this connection's shared catalog is stale past its schedule.

    NULL `last_synced_at` (never indexed) counts as due. The per-connection
    schedule — interval or fixed time-of-day (in the org timezone `tz`, UTC if
    unspecified) — governs cadence. Delegates to `reindex_schedule.is_due`.
    """
    from app.services.reindex_schedule import is_due, UTC
    return is_due(connection, now, tz or UTC)


async def sweep_due_reindexes() -> None:
    """Scheduled entrypoint: re-index every connection whose schema is due.

    Safe to register on a short APScheduler interval (e.g. every 1 minute);
    the staleness gate keeps actual reindex work proportional to N / interval.
    """
    from app.core.scheduler import claim_scheduled_run

    # One worker/replica per fire.
    if not await asyncio.to_thread(claim_scheduled_run, SWEEP_JOB_ID):
        return

    # Enterprise gate — community installs get manual refresh only.
    from app.ee.license import has_feature
    if not has_feature("scheduled_reindex"):
        return

    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.services.connection_indexing_service import ConnectionIndexingService
    from app.services.connection_service import ConnectionService

    now = datetime.utcnow()
    batch = _batch_size()
    t0 = time.perf_counter()

    svc = ConnectionService()
    indexing_service = ConnectionIndexingService()

    async with async_session_maker() as db:
        # Coarse SQL filter: enabled, live, and past any failure-backoff gate.
        # The per-connection interval check happens in Python (interval varies
        # per row). Oldest-synced first so the most stale get priority; we pull
        # a bounded candidate window and never scan the whole table.
        candidates = (
            await db.execute(
                select(Connection)
                .where(
                    Connection.is_active.is_(True),
                    Connection.deleted_at.is_(None),
                    Connection.auto_reindex_enabled.is_(True),
                    (Connection.next_retry_at.is_(None))
                    | (Connection.next_retry_at <= now),
                )
                .order_by(Connection.last_synced_at.asc().nullsfirst())
                .limit(batch * 5)
            )
        ).scalars().all()

        from app.services.reindex_schedule import get_org_timezone, next_run_after

        tz_cache: dict[str, object] = {}

        async def _org_tz(org_id: str):
            key = str(org_id)
            if key not in tz_cache:
                tz_cache[key] = await get_org_timezone(db, key)
            return tz_cache[key]

        due = []
        for conn in candidates:
            # Per-user catalogs (OneDrive, personal Drive) have no admin-side
            # catalog to re-index — they heal on each user's sign-in. Skip.
            if svc._is_per_user_catalog(conn.type):
                continue
            tz = await _org_tz(conn.organization_id)
            if _is_due(conn, now, tz):
                due.append(conn)
            if len(due) >= batch:
                break

        if not due:
            logger.info("schema_reindex.sweep", extra={"due": 0, "candidates": len(candidates)})
            return

        # Stamp the backoff gate BEFORE kicking so a crash/failure mid-run can't
        # leave the connection eligible to be re-kicked on the very next tick.
        # A successful index clears this and advances last_synced_at.
        for conn in due:
            tz = await _org_tz(conn.organization_id)
            conn.next_retry_at = next_run_after(conn, now, tz)
        await db.commit()

        kicked = 0
        for conn in due:
            try:
                await indexing_service.start(db=db, connection=conn)
                kicked += 1
            except Exception as exc:
                logger.warning(
                    "schema_reindex.kick_failed",
                    extra={"connection_id": str(conn.id), "error": str(exc)},
                )

    logger.info(
        "schema_reindex.sweep.done",
        extra={
            "due": len(due),
            "kicked": kicked,
            "batch": batch,
            "elapsed_s": round(time.perf_counter() - t0, 3),
        },
    )
