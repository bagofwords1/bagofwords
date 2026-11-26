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


# ── Settings ─────────────────────────────────────────────────────────────

from app.services.sandbox.settings import SandboxSettingsUpdate, save_settings  # noqa: E402


class SandboxSettingsResponse(BaseModel):
    enabled: bool
    default_environment_id: Optional[str]
    mark_idle_after_seconds: int
    idle_timeout_seconds: int
    stopped_retention_seconds: int
    max_lifetime_seconds: int
    registry_domains: List[str]
    max_running_per_user: int
    max_running_total: Optional[int]
    # What is still missing before sandboxes can be used, for the Settings tab.
    backends_ready: int = 0
    environments: int = 0
    running_sandboxes: int = 0


async def _settings_response(db: AsyncSession, organization_id: str) -> SandboxSettingsResponse:
    from app.services.sandbox import backends as backend_service

    settings = await load_settings(db, organization_id)
    backends = await backend_service.list_backends(db, organization_id)
    counts = await backend_service.running_counts(db, organization_id)
    return SandboxSettingsResponse(
        **settings.model_dump(include=set(SandboxSettingsResponse.model_fields) - {"backends_ready", "environments", "running_sandboxes"}),
        backends_ready=sum(1 for b in backends if b.enabled and b.bootstrap_status == "ready"),
        environments=len(await environment_service.list_environments(db, organization_id, enabled_only=True)),
        running_sandboxes=sum(counts.values()),
    )


@router.get("/settings", response_model=SandboxSettingsResponse)
@requires_permission("manage_sandboxes")
async def get_sandbox_settings(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await _settings_response(db, organization.id)


@router.put("/settings", response_model=SandboxSettingsResponse)
@requires_permission("manage_sandboxes")
async def update_sandbox_settings(
    data: SandboxSettingsUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Change the Sandbox settings. Every change is audited with the old and new value."""
    from app.services.sandbox.audit import audit

    changes = data.model_dump(exclude_unset=True)
    settings = await load_settings(db, organization.id)
    if changes.get("default_environment_id"):
        try:
            await environment_service.get_environment(db, organization.id, changes["default_environment_id"])
        except SandboxError as exc:
            raise _http(exc)
    changed = {}
    for key, value in changes.items():
        if getattr(settings, key) != value:
            changed[key] = {"from": getattr(settings, key), "to": value}
            setattr(settings, key, value)
    if changed:
        try:
            settings = type(settings)(**settings.model_dump())  # re-validate the whole of it
        except ValueError as exc:
            raise AppError("sandbox.invalid_request", str(exc), status_code=422)
        await save_settings(db, organization.id, settings)
        await audit(db, organization.id, "sandbox_settings.updated", str(current_user.id), "sandbox_settings",
                    None, {"title": "Sandbox settings", "changed": changed})
        await db.commit()
    return await _settings_response(db, organization.id)


# ── Backends ─────────────────────────────────────────────────────────────

from app.schemas.sandbox_schema import SandboxBackendCreate, SandboxBackendSchema, SandboxBackendUpdate  # noqa: E402
from app.services.sandbox import backends as backend_service  # noqa: E402
from app.services.sandbox.drivers import registry as driver_registry  # noqa: E402
from app.services.sandbox.drivers.base import SandboxDriverError  # noqa: E402

# A bootstrap still "running" after this long was abandoned by its worker and may be started again.
BOOTSTRAP_STALE_SECONDS = 30 * 60


async def _backend_response(db: AsyncSession, organization_id: str, backend) -> SandboxBackendSchema:
    counts = await backend_service.running_counts(db, organization_id)
    return backend_service.to_schema(backend, counts.get(backend.id, 0))


@router.get("/backend-types")
@requires_permission("manage_sandboxes")
async def list_backend_types(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """The backend types this deployment offers, each with the form it needs
    (config and credentials schemas) and what it can support."""
    described = []
    for backend_type in driver_registry.backend_types():
        try:
            described.append(driver_registry.describe_type(backend_type.name))
        except SandboxDriverError as exc:
            raise AppError("sandbox.invalid_request", exc.message, status_code=500)
    return described


@router.get("/backends", response_model=List[SandboxBackendSchema])
@requires_permission("manage_sandboxes")
async def list_backends(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    counts = await backend_service.running_counts(db, organization.id)
    return [backend_service.to_schema(b, counts.get(b.id, 0)) for b in await backend_service.list_backends(db, organization.id)]


@router.post("/backends", response_model=SandboxBackendSchema)
@requires_permission("manage_sandboxes")
async def create_backend(
    data: SandboxBackendCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    try:
        backend = await backend_service.create_backend(db, organization.id, str(current_user.id), data)
    except SandboxError as exc:
        raise _http(exc)
    return await _backend_response(db, organization.id, backend)


@router.get("/backends/{backend_id}", response_model=SandboxBackendSchema)
@requires_permission("manage_sandboxes")
async def get_backend(
    backend_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    try:
        backend = await backend_service.get_backend(db, organization.id, backend_id)
    except SandboxError as exc:
        raise _http(exc)
    return await _backend_response(db, organization.id, backend)


@router.put("/backends/{backend_id}", response_model=SandboxBackendSchema)
@requires_permission("manage_sandboxes")
async def update_backend(
    backend_id: str,
    data: SandboxBackendUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    try:
        backend = await backend_service.update_backend(db, organization.id, str(current_user.id), backend_id, data)
    except SandboxError as exc:
        raise _http(exc)
    return await _backend_response(db, organization.id, backend)


class BackendTestResponse(BaseModel):
    reachable: bool
    prepared: bool
    healthy: bool
    error: Optional[str]
    checks: List[dict]
    backend: SandboxBackendSchema


@router.post("/backends/{backend_id}/test", response_model=BackendTestResponse)
@requires_permission("manage_sandboxes")
async def test_backend(
    backend_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Test connection. Changes nothing on the backend."""
    try:
        backend = await backend_service.get_backend(db, organization.id, backend_id)
        health = await backend_service.check_backend(db, organization.id, backend)
    except SandboxError as exc:
        raise _http(exc)
    return BackendTestResponse(
        reachable=health.reachable, prepared=health.prepared, healthy=health.healthy, error=health.error,
        checks=health.checks, backend=await _backend_response(db, organization.id, backend),
    )


class ConfirmIdentityRequest(BaseModel):
    fingerprint: str


@router.post("/backends/{backend_id}/confirm-identity", response_model=SandboxBackendSchema)
@requires_permission("manage_sandboxes")
async def confirm_backend_identity(
    backend_id: str,
    data: ConfirmIdentityRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Trust the fingerprint Test connection showed. Nothing is sent to a backend before this."""
    try:
        backend = await backend_service.get_backend(db, organization.id, backend_id)
        await backend_service.confirm_identity(db, organization.id, str(current_user.id), backend, data.fingerprint)
    except SandboxError as exc:
        raise _http(exc)
    return await _backend_response(db, organization.id, backend)


class BootstrapRequest(BaseModel):
    take_over: bool = False
    # Wait for the result instead of returning at once and being polled.
    wait: bool = False


async def _bootstrap_in_background(organization_id: str, actor_id: str, backend_id: str, take_over: bool) -> None:
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        try:
            backend = await backend_service.get_backend(db, organization_id, backend_id)
            await backend_service.bootstrap_backend(db, organization_id, actor_id, backend, take_over=take_over)
        except Exception:  # noqa: BLE001 - recorded on the backend by bootstrap_backend; nothing to return to
            import logging

            logging.getLogger(__name__).exception("sandbox backend bootstrap failed (backend=%s)", backend_id)


@router.post("/backends/{backend_id}/bootstrap", response_model=SandboxBackendSchema)
@requires_permission("manage_sandboxes")
async def bootstrap_backend(
    backend_id: str,
    data: BootstrapRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Prepare (or repair) a backend. Returns at once; the steps are stored on
    the backend as they finish, so reading the backend shows the progress."""
    from datetime import datetime

    from app.core.fire_and_forget import spawn

    try:
        backend = await backend_service.get_backend(db, organization.id, backend_id)
    except SandboxError as exc:
        raise _http(exc)
    if backend.bootstrap_status == "running":
        age = (datetime.utcnow() - (backend.updated_at or datetime.utcnow())).total_seconds()
        if age < BOOTSTRAP_STALE_SECONDS:
            raise AppError("sandbox.bootstrap_running", "This backend is being bootstrapped already.", status_code=409)
    if data.wait:
        await backend_service.bootstrap_backend(db, organization.id, str(current_user.id), backend, take_over=data.take_over)
    else:
        steps = await backend_service.mark_bootstrap_started(db, backend)
        spawn(_bootstrap_in_background(str(organization.id), str(current_user.id), backend.id, data.take_over))
        assert steps is not None
    return await _backend_response(db, organization.id, backend)


@router.delete("/backends/{backend_id}")
@requires_permission("manage_sandboxes")
async def delete_backend(
    backend_id: str,
    cleanup: bool = False,
    delete_sandboxes: bool = False,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Delete a backend. Refused while it has sandboxes unless ``delete_sandboxes``
    is set; ``cleanup`` also removes everything BoW put on it."""
    from app.services.sandbox.service import SandboxService

    try:
        backend = await backend_service.get_backend(db, organization.id, backend_id)
        if delete_sandboxes:
            service = SandboxService()
            for sandbox in await backend_service.live_sandboxes(db, backend.id):
                await service.delete(db, sandbox, str(current_user.id), reason="backend deleted")
        await backend_service.delete_backend(db, organization.id, str(current_user.id), backend_id, cleanup=cleanup)
    except SandboxError as exc:
        raise _http(exc)
    return {"deleted": True}
