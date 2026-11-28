"""File bytes, deleted for good.

``discard_paths`` removes the local copy and the durable copy of each path
and returns the paths it could not remove; ``remember_failed`` keeps those in
``pending_blob_deletes``; ``retry_pending`` (nightly) tries them again until
they are gone. A path is never reported deleted while bytes remain.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# After this many failed nights a path stays listed (an operator can look)
# but is retried less noisily; it is never dropped silently.
MAX_LOGGED_ATTEMPTS = 30


async def discard_paths(paths: Iterable[str]) -> dict[str, str]:
    """Remove each path's bytes, locally and in durable storage.
    Returns ``{path: error}`` for those that could not be removed."""
    from app.services import storage

    failed: dict[str, str] = {}
    for path in paths:
        if not path:
            continue
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as e:
            failed[path] = f"local: {e}"[:500]
        try:
            ok = await storage.adiscard(path)
            if ok is False:
                failed.setdefault(path, "storage: delete refused")
        except Exception as e:  # noqa: BLE001 — any store error means: retry later
            failed.setdefault(path, f"storage: {e}"[:500])
    return failed


async def remember_failed(failed: dict[str, str]) -> None:
    """Keep the paths whose bytes are still there, for the nightly retry."""
    if not failed:
        return
    from app.dependencies import async_session_maker
    from app.models.pending_blob_delete import PendingBlobDelete

    try:
        async with async_session_maker() as db:
            known = {p for (p,) in (await db.execute(
                select(PendingBlobDelete.path).where(PendingBlobDelete.path.in_(list(failed)))
            )).all()}
            now = datetime.utcnow()
            for path, error in failed.items():
                if path not in known:
                    db.add(PendingBlobDelete(path=path, attempts=1, last_error=error, last_attempt_at=now))
            await db.commit()
    except Exception:  # noqa: BLE001 — the delete itself already happened
        logger.exception("Could not record %d file(s) whose bytes are still to delete", len(failed))


async def retry_pending() -> dict[str, int]:
    """Try again every path whose bytes a delete could not remove."""
    from app.dependencies import async_session_maker
    from app.models.pending_blob_delete import PendingBlobDelete

    async with async_session_maker() as db:
        rows = (await db.execute(select(PendingBlobDelete))).scalars().all()
        if not rows:
            return {"deleted": 0, "still_pending": 0}
        failed = await discard_paths([r.path for r in rows])
        now = datetime.utcnow()
        done = [r.id for r in rows if r.path not in failed]
        for r in rows:
            if r.path in failed:
                r.attempts = (r.attempts or 0) + 1
                r.last_error = failed[r.path]
                r.last_attempt_at = now
                if r.attempts <= MAX_LOGGED_ATTEMPTS:
                    logger.warning("Retention: file bytes still not deleted after %d attempts: %s (%s)",
                                   r.attempts, r.path, r.last_error)
        if done:
            await db.execute(delete(PendingBlobDelete).where(PendingBlobDelete.id.in_(done)))
        await db.commit()
        return {"deleted": len(done), "still_pending": len(rows) - len(done)}


async def purge_file_ids(db: AsyncSession, file_ids: list[str]) -> int:
    """Delete these files for good (rows, links, tags, schemas, bytes)."""
    from app.models.file import File
    from app.services.file_service import purge_files

    if not file_ids:
        return 0
    files = (await db.execute(select(File).where(File.id.in_(list(file_ids))))).scalars().all()
    return len(await purge_files(db, list(files)))
