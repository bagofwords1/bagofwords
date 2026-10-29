"""Host applications (Settings -> Integrations) and an artifact's embedding.

A host application is an external system (an ERP, ServiceNow, a portal) that
frames artifacts and signs its users in. There is one per system, configured
by admins with ``manage_settings``. An artifact opts in to a host application
from its own Embed tab; that needs edit rights on the artifact, and the hosts
it may pick are the organization's.
"""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permission_resolver import resolve_permissions
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact as ArtifactModel
from app.models.host_application import HostApplication
from app.models.organization import Organization
from app.models.user import User
from app.schemas.host_application_schema import (
    ArtifactEmbeddingHost,
    ArtifactEmbeddingSchema,
    ArtifactEmbeddingUpdate,
    HostApplicationCreate,
    HostApplicationKeySchema,
    HostApplicationSchema,
    HostApplicationUpdate,
    IdentitySourceOption,
)
from app.services import host_identity
from app.services.host_application_service import host_application_service as service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["host applications"])


async def audit(db, organization_id, action, *, user_id=None, resource_type="host_application",
                resource_id=None, details=None, request=None):
    try:
        from app.ee.audit.service import audit_service

        await audit_service.log(db=db, organization_id=str(organization_id), action=action, user_id=user_id,
                                resource_type=resource_type, resource_id=resource_id, details=details or {},
                                request=request)
    except Exception:
        logger.debug("host application audit failed", exc_info=True)


async def _schema(db, host: HostApplication, cls=HostApplicationSchema, **extra):
    counters = await service.counters(db, host)
    return cls(
        id=str(host.id), public_id=host.public_id, name=host.name, enabled=bool(host.enabled),
        allowed_origins=list(host.allowed_origins or []), identity_mode=host.identity_mode,
        identity_source=host.identity_source, create_users=bool(host.create_users), issuer=host.issuer,
        audience=host.audience, key_fingerprint=host.key_fingerprint, epoch=int(host.epoch or 1),
        revision=int(host.revision or 1), created_at=host.created_at, updated_at=host.updated_at,
        **counters, **extra,
    )


def _summary(host: HostApplication) -> dict:
    return {"name": host.name, "public_id": host.public_id, "identity_mode": host.identity_mode,
            "identity_source": host.identity_source, "create_users": bool(host.create_users),
            "allowed_origins": list(host.allowed_origins or []), "enabled": bool(host.enabled)}


# ── admin: Settings -> Integrations ───────────────────────────────────────

@router.get("/host_applications", response_model=List[HostApplicationSchema])
@requires_permission("manage_settings")
async def list_host_applications(
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    return [await _schema(db, h) for h in await service.list(db, organization)]


@router.get("/host_applications/identity_sources", response_model=List[IdentitySourceOption])
@requires_permission("manage_settings")
async def list_identity_sources(
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Where a host's subject can be looked up: the sign-in methods this
    deployment has configured, plus SCIM and member attributes."""
    return host_identity.source_options(str(organization.id))


@router.post("/host_applications", response_model=HostApplicationKeySchema, status_code=201)
@requires_permission("manage_settings")
async def create_host_application(
    payload: HostApplicationCreate,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    host, private_key = await service.create(db, organization, current_user, payload)
    await audit(db, organization.id, "host_application.created", user_id=str(current_user.id),
                resource_id=str(host.id), details=_summary(host), request=request)
    return await _schema(db, host, HostApplicationKeySchema, private_key=private_key, public_key=host.public_key)


@router.get("/host_applications/{host_id}", response_model=HostApplicationKeySchema)
@requires_permission("manage_settings")
async def get_host_application(
    host_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    host = await service.get(db, organization, host_id)
    return await _schema(db, host, HostApplicationKeySchema, public_key=host.public_key)


@router.patch("/host_applications/{host_id}", response_model=HostApplicationKeySchema)
@requires_permission("manage_settings")
async def update_host_application(
    host_id: str,
    payload: HostApplicationUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    before = await service.get(db, organization, host_id)
    epoch = int(before.epoch or 1)
    host, private_key = await service.update(db, organization, host_id, payload)
    await audit(db, organization.id, "host_application.updated", user_id=str(current_user.id),
                resource_id=str(host.id), request=request,
                details={**_summary(host), "sessions_ended": int(host.epoch) != epoch})
    return await _schema(db, host, HostApplicationKeySchema, private_key=private_key, public_key=host.public_key)


@router.post("/host_applications/{host_id}/rotate_key", response_model=HostApplicationKeySchema)
@requires_permission("manage_settings")
async def rotate_host_application_key(
    host_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """A new signing key. The old one stops working at once and every session
    ends, so the host must switch to the new key in the same change."""
    host, private_key = await service.rotate_key(db, organization, host_id)
    await audit(db, organization.id, "host_application.key_rotated", user_id=str(current_user.id),
                resource_id=str(host.id), details={"key_fingerprint": host.key_fingerprint}, request=request)
    return await _schema(db, host, HostApplicationKeySchema, private_key=private_key, public_key=host.public_key)


@router.post("/host_applications/{host_id}/revoke_sessions", response_model=HostApplicationSchema)
@requires_permission("manage_settings")
async def revoke_host_application_sessions(
    host_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    host = await service.revoke_sessions(db, organization, host_id)
    await audit(db, organization.id, "host_application.sessions_revoked", user_id=str(current_user.id),
                resource_id=str(host.id), request=request)
    return await _schema(db, host)


@router.delete("/host_applications/{host_id}", status_code=204)
@requires_permission("manage_settings")
async def delete_host_application(
    host_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    host = await service.get(db, organization, host_id)
    summary = _summary(host)
    await service.delete(db, organization, host_id)
    await audit(db, organization.id, "host_application.deleted", user_id=str(current_user.id),
                resource_id=str(host_id), details=summary, request=request)
    return Response(status_code=204)


# ── the artifact's Embed tab ──────────────────────────────────────────────

async def _embedding(db, artifact: ArtifactModel, organization, user) -> ArtifactEmbeddingSchema:
    allowed = set(await service.artifact_host_ids(db, artifact.id))
    hosts = [
        ArtifactEmbeddingHost(
            id=str(h.id), public_id=h.public_id, name=h.name, enabled=bool(h.enabled),
            allowed_origins=list(h.allowed_origins or []), identity_mode=h.identity_mode,
            allowed=str(h.id) in allowed,
        )
        for h in await service.list(db, organization)
    ]
    perms = await resolve_permissions(db, str(user.id), str(organization.id))
    return ArtifactEmbeddingSchema(
        artifact_id=str(artifact.id), visibility=str(artifact.visibility or "none"),
        hosts=hosts, can_manage_hosts=perms.has_org_permission("manage_settings"),
    )


async def _artifact(db, artifact_id: str, user, organization, capability) -> ArtifactModel:
    """The dashboard, if the caller holds ``capability`` on it (the same gate
    as the rest of its Settings window, routes/dashboard.py)."""
    from app.services import dashboard_access
    from app.services import dashboard_settings_service

    return await dashboard_settings_service.dashboard_for(
        db, artifact_id, user, organization, getattr(dashboard_access, capability))


@router.get("/artifacts/{artifact_id}/embedding", response_model=ArtifactEmbeddingSchema)
@requires_permission("publish_reports")
async def get_artifact_embedding(
    artifact_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    artifact = await _artifact(db, artifact_id, current_user, organization, "CAP_EDIT")
    return await _embedding(db, artifact, organization, current_user)


@router.put("/artifacts/{artifact_id}/embedding", response_model=ArtifactEmbeddingSchema)
@requires_permission("publish_reports")
async def set_artifact_embedding(
    artifact_id: str,
    payload: ArtifactEmbeddingUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    artifact = await _artifact(db, artifact_id, current_user, organization, "CAP_SETTINGS")
    before = set(await service.artifact_host_ids(db, artifact.id))
    after = set(await service.set_artifact_hosts(db, artifact, payload.host_application_ids, current_user))
    if before != after:
        await audit(db, organization.id, "artifact.embedding_updated", user_id=str(current_user.id),
                    resource_type="artifact", resource_id=str(artifact.id), request=request,
                    details={"added": sorted(after - before), "removed": sorted(before - after)})
    return await _embedding(db, artifact, organization, current_user)
