"""Embedded chat administration (Settings → Embedded chat). manage_settings only.

The private key is returned exactly once, from create and from rotate-key;
nothing here (or anywhere) can read it back later.
"""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.user import User
from app.schemas.embed_schema import (
    EmbedBranding,
    EmbedCreate,
    EmbedCreatedSchema,
    EmbedRuntime,
    EmbedSchema,
    EmbedUpdate,
)
from app.services.embed_service import FORCED_OFF_TOOLS, embed_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["embeds"])


async def _audit(db, organization, action, user, embed, request, details=None):
    try:
        from app.ee.audit.service import audit_service
        await audit_service.log(
            db=db, organization_id=str(organization.id), action=action, user_id=str(user.id),
            resource_type="embed", resource_id=str(embed.id), details=details or {}, request=request,
        )
    except Exception:
        logger.debug("embed audit log failed", exc_info=True)


async def _schema(db, embed) -> EmbedSchema:
    counters = await embed_service.counters(db, embed)
    return EmbedSchema(
        id=str(embed.id),
        public_id=embed.public_id,
        name=embed.name,
        enabled=bool(embed.enabled),
        allowed_origins=list(embed.allowed_origins or []),
        issuer=embed.issuer,
        subject_attribute=embed.subject_attribute,
        key_fingerprint=embed.key_fingerprint,
        epoch=int(embed.epoch or 1),
        runtime=EmbedRuntime(**(embed.runtime or {})),
        branding=EmbedBranding(**(embed.branding or {})),
        revision=int(embed.revision or 1),
        created_at=embed.created_at,
        updated_at=embed.updated_at,
        **counters,
    )


async def _created_schema(db, embed, private_key) -> EmbedCreatedSchema:
    base = await _schema(db, embed)
    return EmbedCreatedSchema(**base.model_dump(), private_key=private_key, public_key=embed.public_key)


@router.get("/embeds", response_model=List[EmbedSchema])
@requires_permission("manage_settings")
async def list_embeds(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    embeds = await embed_service.list_embeds(db, organization)
    return [await _schema(db, e) for e in embeds]


@router.get("/embeds/options")
@requires_permission("manage_settings")
async def embed_options(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Vocabulary for the settings form: the public tool switches an admin may
    turn off and the ones the compact chat always keeps off."""
    from app.schemas.report_api_settings import TOOL_SWITCHES
    from app.settings.config import settings

    return {
        "tool_switches": [k for k in TOOL_SWITCHES if k not in FORCED_OFF_TOOLS],
        "forced_off_tools": list(FORCED_OFF_TOOLS),
        "locales": list(settings.bow_config.i18n.enabled_locales),
        "sdk_path": "/api/embed/sdk/v1/bow-embed.js",
    }


@router.post("/embeds", response_model=EmbedCreatedSchema)
@requires_permission("manage_settings")
async def create_embed(
    payload: EmbedCreate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    embed, private_key = await embed_service.create_embed(db, organization, current_user, payload)
    await _audit(db, organization, "embed.created", current_user, embed, request,
                 {"name": embed.name, "public_id": embed.public_id, "origins": embed.allowed_origins})
    return await _created_schema(db, embed, private_key)


@router.get("/embeds/{embed_id}", response_model=EmbedSchema)
@requires_permission("manage_settings")
async def get_embed(
    embed_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await _schema(db, await embed_service.get_embed(db, organization, embed_id))


@router.get("/embeds/{embed_id}/members")
@requires_permission("manage_settings")
async def get_embed_members(
    embed_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Members whose admin-set attribute maps them into this embed."""
    embed = await embed_service.get_embed(db, organization, embed_id)
    return await embed_service.mapped_members(db, embed)


@router.put("/embeds/{embed_id}", response_model=EmbedSchema)
@requires_permission("manage_settings")
async def update_embed(
    embed_id: str,
    payload: EmbedUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    embed = await embed_service.update_embed(db, organization, embed_id, payload)
    await _audit(db, organization, "embed.updated", current_user, embed, request,
                 {"fields": [k for k, v in payload.model_dump().items() if v is not None and k != "revision"],
                  "enabled": embed.enabled})
    return await _schema(db, embed)


@router.post("/embeds/{embed_id}/rotate-key", response_model=EmbedCreatedSchema)
@requires_permission("manage_settings")
async def rotate_embed_key(
    embed_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    embed, private_key = await embed_service.rotate_key(db, organization, embed_id)
    await _audit(db, organization, "embed.key_rotated", current_user, embed, request,
                 {"key_fingerprint": embed.key_fingerprint})
    return await _created_schema(db, embed, private_key)


@router.post("/embeds/{embed_id}/revoke-sessions", response_model=EmbedSchema)
@requires_permission("manage_settings")
async def revoke_embed_sessions(
    embed_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    embed = await embed_service.revoke_sessions(db, organization, embed_id)
    await _audit(db, organization, "embed.sessions_revoked", current_user, embed, request)
    return await _schema(db, embed)


@router.post("/embeds/{embed_id}/logo", response_model=EmbedSchema)
@requires_permission("manage_settings")
async def upload_embed_logo(
    embed_id: str,
    request: Request,
    logo: UploadFile = File(...),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    raw = await logo.read()
    embed = await embed_service.set_logo(db, organization, embed_id, raw, logo.content_type or "")
    await _audit(db, organization, "embed.logo_uploaded", current_user, embed, request)
    return await _schema(db, embed)


@router.delete("/embeds/{embed_id}/logo", response_model=EmbedSchema)
@requires_permission("manage_settings")
async def delete_embed_logo(
    embed_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    embed = await embed_service.remove_logo(db, organization, embed_id)
    await _audit(db, organization, "embed.logo_removed", current_user, embed, request)
    return await _schema(db, embed)


@router.delete("/embeds/{embed_id}", status_code=204)
@requires_permission("manage_settings")
async def delete_embed(
    embed_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    embed = await embed_service.get_embed(db, organization, embed_id)
    await _audit(db, organization, "embed.deleted", current_user, embed, request, {"name": embed.name})
    await embed_service.delete_embed(db, organization, embed_id)
    return None
