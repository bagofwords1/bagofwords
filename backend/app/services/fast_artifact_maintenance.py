"""Reclaim crash/failed-swap debris without touching served custom tables."""
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
    A DB read failure aborts cleanup for that directory, leaving its files alone.
    """
    if session_factory is None:
        from app.dependencies import async_session_maker
        session_factory = async_session_maker
    totals = {"deleted": 0, "reclaimed_bytes": 0}
    root = artifacts._ARTIFACT_ROOT
    if not root.exists():
        return totals
    before = time.time() - RETENTION_SECONDS
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            with artifacts.try_refresh_lock(directory.name) as acquired:
                if not acquired:
                    continue
                async with session_factory() as db:
                    # Read ALL references, not only this connection's live rows.
                    # This also protects legacy references across directories.
                    references = list((await db.execute(
                        select(ConnectionTable.artifact_path).where(
                            ConnectionTable.artifact_path.is_not(None)
                        )
                    )).scalars())
                    # Keep this synchronous under the lock: cancelling an
                    # offloaded unlink thread could release it before deletion.
                    result = artifacts.prune_unreferenced(directory.name, references, before)
                for key in totals:
                    totals[key] += result[key]
        except Exception:
            logger.exception("fast.artifact.cleanup_failed", extra={"connection_id": directory.name})
    logger.info("fast.artifact.cleanup", extra=totals)
    return totals
