"""Background connection-status refresher.

Keeps ``last_connection_status`` / ``last_connection_checked_at`` fresh so
read endpoints can serve the cached status without ever dialing a warehouse.
This replaces the old read-path behavior where ``GET /data_sources/{id}``
live-tested every connection whose cached status was older than 5 minutes —
sequentially, inside the request, with no connect timeout — which stalled the
agent detail view for minutes on many-connection agents and pinned pooled DB
connections for the whole sweep (see
docs/feedback-loops/agents-hub-agent-many-connections.md).

Design mirrors ``scheduled_reindex.sweep_due_reindexes``:

  * Frequent, cheap tick claimed via ``claim_scheduled_run`` — one
    worker/replica per fire.
  * Own staleness TTL, decoupled from per-connection reindex schedules (those
    run daily/weekly; status wants minutes). Only connections whose last check
    is older than the TTL are tested, oldest first, bounded per tick.
  * Tests run concurrently under a semaphore, each on its own short-lived
    session (``test_connection`` commits), so a slow warehouse never blocks a
    request or holds a request-scoped pool slot.
  * ``system_only`` connections only: per-user (``user_required``) status is
    resolved per user at read time and a system-side probe would test the
    wrong identity. Inactive connections ARE swept — a successful test is what
    reactivates an auto-deactivated ``system_only`` connection.

Unlike the reindex sweeper this is NOT enterprise-gated: with the read-path
retest gone, this is the only thing keeping status badges honest.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

from sqlalchemy import select

logger = logging.getLogger(__name__)

SWEEP_JOB_ID = "connection_status_sweep"

# How old a cached test may get before the sweeper re-tests it. Matches the
# TTL the read path used to enforce.
_DEFAULT_TTL_SECONDS = 300

# Max connections tested per tick (oldest-checked first; the rest roll over).
_DEFAULT_BATCH = 200

# Concurrent tests. Each test occupies a thread from the shared
# ``asyncio.to_thread`` pool for its full dial time, so keep this well below
# the pool size (~32) — query execution shares that pool.
_DEFAULT_CONCURRENCY = 8


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


async def sweep_stale_connection_status() -> None:
    """Scheduled entrypoint: re-test every system_only connection whose cached
    status is stale past the TTL. Safe on a short APScheduler interval."""
    from app.core.scheduler import claim_scheduled_run

    if not await asyncio.to_thread(claim_scheduled_run, SWEEP_JOB_ID):
        return

    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.models.organization import Organization
    from app.services.connection_service import ConnectionService

    ttl = _env_int("BOW_CONN_STATUS_TTL", _DEFAULT_TTL_SECONDS)
    batch = _env_int("BOW_CONN_STATUS_SWEEP_BATCH", _DEFAULT_BATCH)
    concurrency = _env_int("BOW_CONN_STATUS_SWEEP_CONCURRENCY", _DEFAULT_CONCURRENCY)
    cutoff = datetime.utcnow() - timedelta(seconds=ttl)
    t0 = time.perf_counter()

    async with async_session_maker() as db:
        rows = (
            await db.execute(
                select(Connection.id, Connection.organization_id)
                .where(
                    Connection.deleted_at.is_(None),
                    Connection.auth_policy == "system_only",
                    (Connection.last_connection_checked_at.is_(None))
                    | (Connection.last_connection_checked_at <= cutoff),
                )
                .order_by(Connection.last_connection_checked_at.asc().nullsfirst())
                .limit(batch)
            )
        ).all()
        if not rows:
            return
        org_ids = {str(oid) for _, oid in rows}
        orgs = {
            str(o.id): o
            for o in (
                await db.execute(select(Organization).where(Organization.id.in_(org_ids)))
            ).scalars().all()
        }

    svc = ConnectionService()
    sem = asyncio.Semaphore(concurrency)
    results = {"ok": 0, "failed": 0, "errored": 0}

    async def _test_one(conn_id: str, org_id: str) -> None:
        org = orgs.get(org_id)
        if org is None:
            return
        async with sem:
            # Own session per test: test_connection commits, and a slow dial
            # must not hold a shared session/pool slot for its neighbors.
            async with async_session_maker() as sdb:
                try:
                    status = await svc.test_connection(
                        db=sdb,
                        connection_id=str(conn_id),
                        organization=org,
                    )
                    ok = bool(status.get("success")) if isinstance(status, dict) else bool(status)
                    results["ok" if ok else "failed"] += 1
                except Exception as exc:
                    results["errored"] += 1
                    logger.warning(
                        "connection_status_sweep.test_failed",
                        extra={"connection_id": str(conn_id), "error": str(exc)},
                    )

    await asyncio.gather(*(_test_one(str(cid), str(oid)) for cid, oid in rows))

    logger.info(
        "connection_status_sweep.done",
        extra={
            "swept": len(rows),
            **results,
            "ttl_s": ttl,
            "elapsed_s": round(time.perf_counter() - t0, 3),
        },
    )
