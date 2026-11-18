"""The caller's World view. Read-only, permission filtered, independently paged."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user, forbid_service_account_principal
from app.core.permission_resolver import assert_principal_belongs_to_org
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings
from app.models.user import User
from app.services.world_service import WorldService

router = APIRouter(tags=["world"], dependencies=[Depends(forbid_service_account_principal)])


@router.get("/world")
async def world(
    section: Literal[
        "overview",
        "tasks",
        "evals",
        "instructions",
        "files",
        "projects",
        "reports",
        "artifacts",
        "memory",
        "usage",
        "agent",
        "folder",
        "report",
    ] = "overview",
    folder_id: str | None = None,
    agent_id: str | None = None,
    report_id: str | None = None,
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    await assert_principal_belongs_to_org(db, user, organization.id)
    config = await db.scalar(
        select(OrganizationSettings.config).where(OrganizationSettings.organization_id == organization.id)
    )
    entry = (config or {}).get("show_world")
    if not (isinstance(entry, dict) and entry.get("value") is True):
        raise HTTPException(404)
    ids = {"agent": agent_id, "folder": folder_id, "report": report_id}
    if section in ids and not ids[section]:
        raise HTTPException(422, f"{section}_id is required")
    return await WorldService().build(
        db,
        user,
        organization,
        section=section,
        folder_id=folder_id,
        agent_id=agent_id,
        report_id=report_id,
        offset=offset,
    )
