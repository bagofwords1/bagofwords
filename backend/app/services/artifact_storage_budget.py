"""Atomic organization/subject byte budgets in the caller's resource transaction."""

import os
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError
from app.models.artifact_resource import ArtifactStorageBudget, ArtifactResource, ArtifactRecord, ArtifactFileBinding
from app.services.artifact_resource_service import fail


async def reserve(service, owner_id, delta, count_delta=0):
    db, org = service.db, str(service.artifact.organization_id)
    # Lock organization before subject consistently. These are byte counters,
    # never request/execution records. Failed resource transactions roll back.
    for actor, limit, count_limit in (
        ("", int(os.environ.get("BOW_ARTIFACT_ORG_MAX_BYTES", 1073741824)), 100000),
        (owner_id, int(os.environ.get("BOW_ARTIFACT_USER_MAX_BYTES", 268435456)), 25000),
    ):
        clause = (ArtifactStorageBudget.organization_id == org, ArtifactStorageBudget.actor_id == actor)
        present = await db.scalar(select(ArtifactStorageBudget.id).where(*clause))
        if present is None:
            record_query = (
                select(func.coalesce(func.sum(ArtifactRecord.size), 0))
                .join(ArtifactResource, ArtifactResource.id == ArtifactRecord.resource_id)
                .where(ArtifactResource.organization_id == org)
            )
            file_query = (
                select(func.coalesce(func.sum(ArtifactFileBinding.size), 0))
                .join(ArtifactResource, ArtifactResource.id == ArtifactFileBinding.resource_id)
                .where(ArtifactResource.organization_id == org, ArtifactFileBinding.deleted_at.is_(None))
            )
            if actor:
                record_query = record_query.where(ArtifactRecord.owner_id == actor)
                file_query = file_query.where(ArtifactFileBinding.owner_id == actor)
            current = int(await db.scalar(record_query)) + int(await db.scalar(file_query))
            current_count = int(await db.scalar(record_query.with_only_columns(func.count(ArtifactRecord.id)))) + int(
                await db.scalar(file_query.with_only_columns(func.count(ArtifactFileBinding.id)))
            )
            try:
                async with db.begin_nested():
                    db.add(
                        ArtifactStorageBudget(organization_id=org, actor_id=actor, bytes=current, count=current_count)
                    )
                    await db.flush()
            except IntegrityError:
                pass  # Another artifact initialized this shared counter.
        result = await db.execute(
            update(ArtifactStorageBudget)
            .where(
                *clause,
                ArtifactStorageBudget.bytes + delta <= limit,
                ArtifactStorageBudget.bytes + delta >= 0,
                ArtifactStorageBudget.count + count_delta <= count_limit,
                ArtifactStorageBudget.count + count_delta >= 0,
            )
            .values(bytes=ArtifactStorageBudget.bytes + delta, count=ArtifactStorageBudget.count + count_delta)
        )
        if result.rowcount != 1:
            fail("QUOTA_EXCEEDED", "Organization or personal artifact storage limit reached", 429)
