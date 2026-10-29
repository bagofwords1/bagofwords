"""Bounded retention and orphan-blob cleanup, invoked by the deployment scheduler.

Never follows filenames from user input. Live blobs are protected by bindings;
new or uncommitted blobs get a 24-hour grace period. Database backup and storage
snapshots must use the same quiescent point.
"""

import asyncio
import time
from datetime import datetime, timedelta
from sqlalchemy import select, delete
from app.models.artifact_resource import (
    ArtifactMutation,
    ArtifactView,
    ArtifactViewDay,
    ArtifactRateBucket,
    ArtifactFileBinding,
)
from app.services.artifact_file_service import storage_root


async def maintain(db, batch=500):
    now = datetime.utcnow()
    counts = {}
    for model, predicate in (
        (ArtifactMutation, ArtifactMutation.created_at < now - timedelta(days=7)),
        (ArtifactView, ArtifactView.created_at < now - timedelta(days=30)),
        (ArtifactViewDay, ArtifactViewDay.day < (now - timedelta(days=400)).date().isoformat()),
        (ArtifactRateBucket, ArtifactRateBucket.bucket < int(time.time()) // 60 - 2),
    ):
        ids = list((await db.execute(select(model.id).where(predicate).limit(batch))).scalars())
        if ids:
            await db.execute(delete(model).where(model.id.in_(ids)))
        counts[model.__tablename__] = len(ids)
    await db.commit()
    removed = 0
    # File scanning runs only in this maintenance process, never an API request.
    # Limit deletions per invocation; storage names contain no customer filenames.
    root = storage_root()
    for path in root.glob("*.blob"):
        if removed >= batch:
            break
        if path.is_symlink() or path.stat().st_mtime > time.time() - 86400:
            continue
        binding = await db.scalar(
            select(ArtifactFileBinding.id).where(
                ArtifactFileBinding.file_id == path.stem, ArtifactFileBinding.deleted_at.is_(None)
            )
        )
        if binding is None:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            removed += 1
    counts["orphan_blobs"] = removed
    return counts
