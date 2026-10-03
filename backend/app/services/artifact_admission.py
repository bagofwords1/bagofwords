"""Shared fixed-window limits with atomic database admission and bounded retention."""

import time
from sqlalchemy import update, delete
from sqlalchemy.exc import IntegrityError
from app.models.artifact_resource import ArtifactRateBucket
from app.services.artifact_resource_service import digest, fail


async def admit(db, scope, limit, now=None):
    bucket = int(time.time() if now is None else now) // 60
    hashed = digest(scope)
    clause = (
        ArtifactRateBucket.scope_hash == hashed,
        ArtifactRateBucket.bucket == bucket,
        ArtifactRateBucket.count < limit,
    )
    result = await db.execute(update(ArtifactRateBucket).where(*clause).values(count=ArtifactRateBucket.count + 1))
    if result.rowcount == 0:
        try:
            async with db.begin_nested():
                db.add(ArtifactRateBucket(scope_hash=hashed, bucket=bucket, count=1))
                await db.flush()
        except IntegrityError:
            result = await db.execute(
                update(ArtifactRateBucket).where(*clause).values(count=ArtifactRateBucket.count + 1)
            )
            if result.rowcount == 0:
                fail("RATE_LIMITED", "Too many requests; wait before trying again", 429)
    await db.execute(delete(ArtifactRateBucket).where(ArtifactRateBucket.bucket < bucket - 2))
    await db.commit()
