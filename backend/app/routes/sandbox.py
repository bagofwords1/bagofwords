"""Settings → Sandbox: the admin API for agent sandboxes.

Every route here is behind two gates, in this order:

1. the deployment flag (``require_sandbox_feature``): with it off every route
   answers ``agent_sandboxes.disabled``;
2. the ``manage_sandboxes`` permission, which is separate from
   ``manage_settings``.

Responses never include private keys, registry passwords, secret values or
sandbox tokens.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.errors import AppError
from app.models.organization import Organization
from app.models.user import User
from app.schemas.sandbox_schema import (
    EnvironmentAccessGrant,
    EnvironmentAccessUpdate,
    SandboxEnvironmentCreate,
    SandboxEnvironmentSchema,
    SandboxEnvironmentUpdate,
)
from app.services.sandbox import access as access_service
from app.services.sandbox import environments as environment_service
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.feature import require_sandbox_feature
from app.services.sandbox.settings import load_settings

router = APIRouter(
    prefix="/sandbox",
    tags=["sandbox"],
    dependencies=[Depends(require_sandbox_feature)],
)


def _http(exc: SandboxError) -> AppError:
    """A sandbox error as the typed HTTP error the frontend localises by code."""
    return AppError(exc.code, exc.message, status_code=exc.status)


class EnvironmentResponse(SandboxEnvironmentSchema):
    # Who may use it (the per-environment `use` grant).
    access: List[EnvironmentAccessGrant] = []
    # Things the admin should know about it as saved (not errors).
    warnings: List[str] = []


async def _environment_response(db: AsyncSession, organization_id: str, env) -> EnvironmentResponse:
    return EnvironmentResponse(
        **SandboxEnvironmentSchema.from_model(env).model_dump(),
        access=[EnvironmentAccessGrant(**g) for g in await access_service.list_environment_grants(db, organization_id, env.id)],
        warnings=await environment_service.validation_warnings(db, organization_id, env),
    )


@router.get("/status")
@requires_permission("manage_sandboxes")
async def get_sandbox_status(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Where the feature stands for this organization (the three gate levels)."""
    settings = await load_settings(db, organization.id)
    return {"feature_enabled": True, "enabled": bool(settings.enabled)}


# ── Environments ─────────────────────────────────────────────────────────


@router.get("/environments", response_model=List[EnvironmentResponse])
@requires_permission("manage_sandboxes")
async def list_environments(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    # The built-in "Default" environment appears the first time the section is opened.
    await environment_service.ensure_builtin_environment(db, organization.id)
    environments = await environment_service.list_environments(db, organization.id)
    return [await _environment_response(db, organization.id, env) for env in environments]


@router.post("/environments", response_model=EnvironmentResponse)
@requires_permission("manage_sandboxes")
async def create_environment(
    data: SandboxEnvironmentCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    try:
        env = await environment_service.create_environment(db, organization.id, str(current_user.id), data)
    except SandboxError as exc:
        raise _http(exc)
    _pull_in_background(organization.id, env.id)
    return await _environment_response(db, organization.id, env)


@router.get("/environments/{environment_id}", response_model=EnvironmentResponse)
@requires_permission("manage_sandboxes")
async def get_environment(
    environment_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    try:
        env = await environment_service.get_environment(db, organization.id, environment_id)
    except SandboxError as exc:
        raise _http(exc)
    return await _environment_response(db, organization.id, env)


@router.put("/environments/{environment_id}", response_model=EnvironmentResponse)
@requires_permission("manage_sandboxes")
async def update_environment(
    environment_id: str,
    data: SandboxEnvironmentUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    try:
        before = await environment_service.get_environment(db, organization.id, environment_id)
        image_before = before.image
        env = await environment_service.update_environment(db, organization.id, str(current_user.id), environment_id, data)
    except SandboxError as exc:
        raise _http(exc)
    if env.image != image_before:
        _pull_in_background(organization.id, env.id)
    return await _environment_response(db, organization.id, env)


@router.delete("/environments/{environment_id}")
@requires_permission("manage_sandboxes")
async def delete_environment(
    environment_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    try:
        await environment_service.delete_environment(db, organization.id, str(current_user.id), environment_id)
    except SandboxError as exc:
        raise _http(exc)
    return {"deleted": True}


@router.put("/environments/{environment_id}/access", response_model=List[EnvironmentAccessGrant])
@requires_permission("manage_sandboxes")
async def set_environment_access(
    environment_id: str,
    data: EnvironmentAccessUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Replace who may use an environment: users, groups and roles holding `use` on it."""
    try:
        env = await environment_service.get_environment(db, organization.id, environment_id)
        grants = await access_service.set_environment_grants(
            db, organization.id, str(current_user.id), env, [g.model_dump() for g in data.grants],
        )
    except SandboxError as exc:
        raise _http(exc)
    return [EnvironmentAccessGrant(**g) for g in grants]


class PullResponse(BaseModel):
    backends: dict


@router.post("/environments/{environment_id}/pull", response_model=PullResponse)
@requires_permission("manage_sandboxes")
async def pull_environment_image(
    environment_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Pull the environment's image on every matching backend now, and report per backend."""
    try:
        await environment_service.get_environment(db, organization.id, environment_id)
    except SandboxError as exc:
        raise _http(exc)
    return PullResponse(backends=await environment_service.pull_image_on_backends(organization.id, environment_id))


def _pull_in_background(organization_id: str, environment_id: str) -> None:
    """Saving an environment with a new image pulls it on matching backends, without making the admin wait."""
    from app.core.fire_and_forget import spawn

    spawn(environment_service.pull_image_on_backends(str(organization_id), str(environment_id)))
