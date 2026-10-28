"""Reclaim crash/failed-swap debris without touching served custom tables."""
import asyncio
import logging
import time

from sqlalchemy import select

from app.data_sources.fast import artifacts
from app.models.connection_table import ConnectionTable

logger = logging.getLogger(__name__)
RETENTION_SECONDS = 24 * 60 * 60


async def purge_orphaned_fast_artifacts(session_factory=None) -> dict:
    """Daily local-volume sweep; skip any connection with an active refresh.

    Files may be created before their DB reference is committed. The same volume
    lock as refresh plus a 24h grace period protects these and older-version
    writers during rolling deploys. Never infer liveness from refresh status.
    A DB read failure aborts the sweep, leaving every file alone.
    """
    if session_factory is None:
        from app.dependencies import async_session_maker
        session_factory = async_session_maker
    totals = {"deleted": 0, "reclaimed_bytes": 0}
    root = artifacts._ARTIFACT_ROOT
    if not root.exists():
        return totals
    # Capture the age boundary BEFORE the reference snapshot. All refreshes
    # publish fresh UUID files; any reference committed after this snapshot
    # points to a file newer than this cutoff and cannot be collected today.
    before = time.time() - RETENTION_SECONDS
    try:
        async with session_factory() as db:
            references = list((await db.execute(
                select(ConnectionTable.artifact_path).where(ConnectionTable.artifact_path.is_not(None))
            )).scalars())
        protected = artifacts.protected_paths(references)
    except Exception:
        logger.exception("fast.artifact.reference_snapshot_failed")
        return totals
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            with artifacts.try_refresh_lock(directory.name) as acquired:
                if not acquired:
                    continue
                # Keep unlink synchronous under the lock: cancelling an
                # offloaded unlink thread could release it before deletion.
                result = artifacts.prune_unreferenced(directory.name, protected, before)
                for key in totals:
                    totals[key] += result[key]
        except Exception:
            logger.exception("fast.artifact.cleanup_failed", extra={"connection_id": directory.name})
    logger.info("fast.artifact.cleanup", extra=totals)
    return totals


async def scheduled_purge_orphaned_fast_artifacts(session_factory=None) -> dict:
    """Shared job stores fire in every worker; one claim owns each sweep."""
    from app.core.scheduler import claim_scheduled_run

    if not await asyncio.to_thread(claim_scheduled_run, "purge_orphaned_fast_artifacts_daily"):
        return {"deleted": 0, "reclaimed_bytes": 0}
    return await purge_orphaned_fast_artifacts(session_factory)
