"""The app launcher: ``GET /api/apps`` (the apps this member can open) and
``POST /api/apps/access-request`` (an app user asking for the workspace).

Open to every member, with or without ``access_workspace`` — it is the one
page of BOW an app user has (see core/workspace_access).
"""
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.user import User
from app.services import app_launcher
from app.services.artifact_admission import admit

router = APIRouter(tags=["apps"])

ACCESS_REQUESTS_PER_MINUTE = 5


class AppCard(BaseModel):
    id: str
    ref: str
    title: Optional[str] = None
    description: Optional[str] = None
    mode: Optional[str] = None
    thumbnail_url: Optional[str] = None
    applications: List[str] = []


class AppLauncherSchema(BaseModel):
    apps: List[AppCard]
    # Whether this member may also use the workspace (the "Open BOW" link).
    workspace_access: bool
    # An app user's pending ask for the workspace.
    access_requested: bool = False


class AccessRequestSchema(BaseModel):
    access_requested: bool
    workspace_access: bool


@router.get("/apps", response_model=AppLauncherSchema)
async def list_apps(
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    from app.core.workspace_access import has_workspace_access

    workspace = await has_workspace_access(db, user, str(organization.id))
    requested = False if workspace else await app_launcher.access_requested(db, user, organization)
    apps = await app_launcher.list_apps(db, user, organization)
    return AppLauncherSchema(apps=[AppCard(**a) for a in apps], workspace_access=workspace,
                             access_requested=requested)


@router.get("/apps/available")
async def apps_available(
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Whether this organization has any app at all (the sidebar shows "Apps"
    only then). Deliberately not per reader: it is two small queries on every
    page load, and the launcher itself answers who may open what."""
    return {"available": await app_launcher.organization_has_apps(db, str(organization.id))}


@router.post("/apps/access-request", response_model=AccessRequestSchema)
async def request_workspace_access(
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Ask the organization's member managers for the workspace. A member who
    already has it is told so and nobody is notified."""
    from app.core.workspace_access import has_workspace_access

    if await has_workspace_access(db, user, str(organization.id)):
        return AccessRequestSchema(access_requested=False, workspace_access=True)
    user_id, organization_id = str(user.id), str(organization.id)
    await admit(db, ["workspace-access-request", organization_id, user_id], ACCESS_REQUESTS_PER_MINUTE)
    await app_launcher.request_access(db, user, organization)
    return AccessRequestSchema(access_requested=True, workspace_access=False)
