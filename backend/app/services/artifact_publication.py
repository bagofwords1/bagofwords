"""Explicit UI publication; never changes records, policies or existing sharing."""

from datetime import datetime
from sqlalchemy import select, update, or_
from sqlalchemy.orm import lazyload
from app.models.artifact import Artifact, ArtifactVersion
from app.models.artifact_resource import ArtifactPublication
from app.errors import AppError
from app.services.artifact_resource_service import fail, digest


def published_version_clause():
    """Use after an outer join on ArtifactPublication.artifact_id."""
    return or_(ArtifactPublication.id.is_(None), ArtifactPublication.version_id == ArtifactVersion.id)


async def publish(service, version_id, expected_revision, request_key):
    import os

    if os.environ.get("BOW_ARTIFACT_RESOURCES_READ_ONLY") == "true":
        fail("UNAVAILABLE", "Artifact writes are temporarily disabled", 503)
    if not service.owner or not service.member:
        fail("FORBIDDEN", "Only the owner can publish", 403)
    db = service.db
    await db.execute(update(Artifact).where(Artifact.id == service.artifact.id).values(updated_at=datetime.utcnow()))
    fingerprint = digest(["publish", version_id, expected_revision])
    replay = await service.replay(request_key, fingerprint)
    if replay is not None:
        return replay
    state = await db.scalar(select(ArtifactPublication).where(ArtifactPublication.artifact_id == service.artifact.id))
    if expected_revision != (state.revision if state else 0):
        fail("CONFLICT", "Publication changed; read its current revision", 409)
    version = await db.scalar(
        select(ArtifactVersion)
        .options(lazyload("*"))
        .where(
            ArtifactVersion.id == version_id,
            ArtifactVersion.artifact_id == service.artifact.id,
            ArtifactVersion.deleted_at.is_(None),
            ArtifactVersion.status == "completed",
        )
    )
    if version is None:
        fail("NOT_FOUND", "Completed artifact version not found", 404)
    for name, requirement in (version.content or {}).get("resource_requirements", {}).items():
        try:
            _, definition = await service.resource(name)
        except AppError as exc:
            if exc.status_code != 404:
                raise
            fail("CONFLICT", f"This UI version uses resource '{name}', which no longer exists", 409)
        if definition.kind != requirement.get("kind") or any(
            key not in definition.fields or definition.fields[key].type != kind
            for key, kind in requirement.get("fields", {}).items()
        ):
            fail("CONFLICT", "UI requirements no longer match its resources", 409)
    if state is None:
        state = ArtifactPublication(artifact_id=service.artifact.id, version_id=version.id, revision=1)
        db.add(state)
    else:
        state.version_id, state.revision = version.id, state.revision + 1
    result = {"versionId": version.id, "revision": state.revision}
    await service.audit("artifact.published", service.artifact.id, version_id=version.id, revision=state.revision)
    service.remember(request_key, fingerprint, result)
    await db.flush()
    return result
