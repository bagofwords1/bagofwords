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

  * Failure backoff. Before kicking, we stamp `next_retry_at = now + interval`,
    so a connection that fails (or is skipped because a user_required source
    has no system creds) is not re-kicked every tick. A successful index clears
    `next_retry_at` and advances `last_synced_at` (both gate re-selection).

  * Per-user overlays refresh on use, not here. This sweep only refreshes the
    shared catalog; a user on a delegated connection reads their own overlay,
    which only their own credentials can rebuild. Rather than crawl for every
    user (most of them idle, many with expired tokens), the schema context
    calls `kick_stale_user_overlays` when that user's overlay is actually read,
    on the same per-connection schedule.

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
    from app.services.connection_identity import catalog_requires_user_sign_in
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
            # Per-user OAuth connectors (MCP/Custom API connected via DCR or an
            # admin OAuth app) hold an OAuth client, not a token — a sweep run
            # has no identity to crawl as and would 401 on every tick. Their
            # catalog is discovered when a user signs in, or on an admin's
            # manual retry, which runs with that admin's own credentials.
            if catalog_requires_user_sign_in(conn):
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


async def kick_stale_user_overlays(connections, user_id: str) -> int:
    """Start a background per-user catalog sync for every one of `connections`
    whose overlay for `user_id` is stale past the connection's schedule.

    Called when a user's overlay is read for a prompt. Never blocks on the
    sync itself: the current prompt keeps the overlay it has and the next one
    sees the refreshed catalog. The last user-scoped `ConnectionIndexing` run
    is the sync clock, and a failed run counts too, so a revoked token is
    retried once per interval rather than on every prompt. Same gates as the
    sweep: the `scheduled_reindex` license feature and the connection's own
    `auto_reindex_enabled`. Returns how many syncs were started.
    """
    from app.ee.license import has_feature
    if not has_feature("scheduled_reindex"):
        return 0

    from app.schemas.data_source_registry import tool_provider_types

    # A user-scoped run on a tool provider re-discovers the SHARED tool list
    # with this user's token; that is not an overlay refresh.
    tool_types = tool_provider_types()
    eligible = [
        c for c in connections
        if c is not None
        and c.is_active
        and c.deleted_at is None
        and c.auto_reindex_enabled
        and c.type not in tool_types
    ]
    if not eligible:
        return 0

    from sqlalchemy import func
    from app.dependencies import async_session_maker
    from app.models.connection_indexing import ConnectionIndexing
    from app.services.connection_indexing_service import ConnectionIndexingService
    from app.services.reindex_schedule import get_org_timezone, is_stale

    now = datetime.utcnow()
    kicked = 0
    # Own session: `start` commits, and the caller is mid-way through building
    # a prompt on its session.
    async with async_session_maker() as db:
        conn_ids = [str(c.id) for c in eligible]
        last_by_conn = dict(
            (
                await db.execute(
                    select(
                        ConnectionIndexing.connection_id,
                        func.max(func.coalesce(ConnectionIndexing.started_at, ConnectionIndexing.created_at)),
                    )
                    .where(
                        ConnectionIndexing.connection_id.in_(conn_ids),
                        ConnectionIndexing.user_id == str(user_id),
                    )
                    .group_by(ConnectionIndexing.connection_id)
                )
            ).all()
        )

        indexing_service = ConnectionIndexingService()
        tz_cache: dict[str, object] = {}
        for conn in eligible:
            org_key = str(conn.organization_id)
            if org_key not in tz_cache:
                tz_cache[org_key] = await get_org_timezone(db, org_key)
            if not is_stale(conn, last_by_conn.get(str(conn.id)), now, tz_cache[org_key]):
                continue
            try:
                # Idempotent: an in-flight run for this user is returned as is.
                await indexing_service.start(db=db, connection=conn, user_id=str(user_id))
                kicked += 1
            except Exception as exc:
                logger.warning(
                    "schema_reindex.user_overlay_kick_failed",
                    extra={"connection_id": str(conn.id), "user_id": str(user_id), "error": str(exc)},
                )

    if kicked:
        logger.info(
            "schema_reindex.user_overlay_kicked",
            extra={"user_id": str(user_id), "kicked": kicked},
        )
    return kicked
