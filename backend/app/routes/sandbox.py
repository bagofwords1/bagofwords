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
from pydantic import BaseModel, ValidationError
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
from app.services.sandbox import errors
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
    delete_sandboxes: bool = False,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Delete an environment. Refused while sandboxes created from it exist
    (they keep its network policy) unless ``delete_sandboxes`` is set."""
    from app.services.sandbox.service import SandboxService

    try:
        if delete_sandboxes:
            await environment_service.get_environment(db, organization.id, environment_id)
            service = SandboxService()
            for sandbox in await environment_service.live_sandboxes(db, environment_id):
                await service.delete(db, sandbox, str(current_user.id), reason="environment deleted")
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
    computes: dict


@router.post("/environments/{environment_id}/pull", response_model=PullResponse)
@requires_permission("manage_sandboxes")
async def pull_environment_image(
    environment_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Pull the environment's image on every matching compute now, and report per compute."""
    try:
        await environment_service.get_environment(db, organization.id, environment_id)
    except SandboxError as exc:
        raise _http(exc)
    return PullResponse(computes=await environment_service.pull_image_on_computes(organization.id, environment_id))


def _pull_in_background(organization_id: str, environment_id: str) -> None:
    """Saving an environment with a new image pulls it on matching computes, without making the admin wait."""
    from app.core.fire_and_forget import spawn

    spawn(environment_service.pull_image_on_computes(str(organization_id), str(environment_id)))


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
    computes_ready: int = 0
    environments: int = 0
    running_sandboxes: int = 0


async def _settings_response(db: AsyncSession, organization_id: str) -> SandboxSettingsResponse:
    from app.services.sandbox import computes as compute_service

    settings = await load_settings(db, organization_id)
    computes = await compute_service.list_computes(db, organization_id)
    counts = await compute_service.running_counts(db, organization_id)
    return SandboxSettingsResponse(
        **settings.model_dump(include=set(SandboxSettingsResponse.model_fields) - {"computes_ready", "environments", "running_sandboxes"}),
        computes_ready=sum(1 for b in computes if b.enabled and b.bootstrap_status == "ready"),
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
        except ValidationError as exc:
            # One plain line per problem, not pydantic's multi-line report with its documentation link.
            message = "; ".join(
                f"{'.'.join(str(p) for p in e.get('loc', ()))}: {str(e.get('msg', '')).removeprefix('Value error, ')}"
                for e in exc.errors()
            )
            raise AppError("sandbox.invalid_request", message, status_code=422)
        await save_settings(db, organization.id, settings)
        await audit(db, organization.id, "sandbox_settings.updated", str(current_user.id), "sandbox_settings",
                    None, {"title": "Sandbox settings", "changed": changed})
        await db.commit()
    return await _settings_response(db, organization.id)


# ── Computes ─────────────────────────────────────────────────────────────

from app.schemas.sandbox_schema import SandboxComputeCreate, SandboxComputeSchema, SandboxComputeUpdate  # noqa: E402
from app.services.sandbox import computes as compute_service  # noqa: E402
from app.services.sandbox.drivers import registry as driver_registry  # noqa: E402
from app.services.sandbox.drivers.base import SandboxDriverError  # noqa: E402

# A bootstrap still "running" after this long was abandoned by its worker and may be started again.
BOOTSTRAP_STALE_SECONDS = 30 * 60


async def _compute_response(db: AsyncSession, organization_id: str, compute) -> SandboxComputeSchema:
    usage = await compute_service.usage_by_compute(db, organization_id)
    return compute_service.to_schema(compute, usage=usage.get(compute.id))


@router.get("/compute-types")
@requires_permission("manage_sandboxes")
async def list_compute_types(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """The compute types this deployment offers, each with the form it needs
    (config and credentials schemas) and what it can support."""
    described = []
    for entry in driver_registry.compute_types():
        try:
            described.append(driver_registry.describe_type(entry.name))
        except SandboxDriverError as exc:
            raise AppError("sandbox.invalid_request", exc.message, status_code=500)
    return described


@router.get("/computes", response_model=List[SandboxComputeSchema])
@requires_permission("manage_sandboxes")
async def list_computes(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    usage = await compute_service.usage_by_compute(db, organization.id)
    return [compute_service.to_schema(b, usage=usage.get(b.id)) for b in await compute_service.list_computes(db, organization.id)]


@router.post("/computes", response_model=SandboxComputeSchema)
@requires_permission("manage_sandboxes")
async def create_compute(
    data: SandboxComputeCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    try:
        compute = await compute_service.create_compute(db, organization.id, str(current_user.id), data)
    except SandboxError as exc:
        raise _http(exc)
    return await _compute_response(db, organization.id, compute)


@router.get("/computes/{compute_id}", response_model=SandboxComputeSchema)
@requires_permission("manage_sandboxes")
async def get_compute(
    compute_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    try:
        compute = await compute_service.get_compute(db, organization.id, compute_id)
    except SandboxError as exc:
        raise _http(exc)
    return await _compute_response(db, organization.id, compute)


@router.put("/computes/{compute_id}", response_model=SandboxComputeSchema)
@requires_permission("manage_sandboxes")
async def update_compute(
    compute_id: str,
    data: SandboxComputeUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    try:
        compute = await compute_service.update_compute(db, organization.id, str(current_user.id), compute_id, data)
    except SandboxError as exc:
        raise _http(exc)
    return await _compute_response(db, organization.id, compute)


class ComputeTestResponse(BaseModel):
    reachable: bool
    prepared: bool
    healthy: bool
    error: Optional[str]
    checks: List[dict]
    compute: SandboxComputeSchema


@router.post("/computes/{compute_id}/test", response_model=ComputeTestResponse)
@requires_permission("manage_sandboxes")
async def test_compute(
    compute_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Test connection. Changes nothing on the compute."""
    try:
        compute = await compute_service.get_compute(db, organization.id, compute_id)
        health = await compute_service.check_compute(db, organization.id, compute)
    except SandboxError as exc:
        raise _http(exc)
    return ComputeTestResponse(
        reachable=health.reachable, prepared=health.prepared, healthy=health.healthy, error=health.error,
        checks=health.checks, compute=await _compute_response(db, organization.id, compute),
    )


class ConfirmIdentityRequest(BaseModel):
    fingerprint: str


@router.post("/computes/{compute_id}/confirm-identity", response_model=SandboxComputeSchema)
@requires_permission("manage_sandboxes")
async def confirm_compute_identity(
    compute_id: str,
    data: ConfirmIdentityRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Trust the fingerprint Test connection showed. Nothing is sent to a compute before this."""
    try:
        compute = await compute_service.get_compute(db, organization.id, compute_id)
        await compute_service.confirm_identity(db, organization.id, str(current_user.id), compute, data.fingerprint)
    except SandboxError as exc:
        raise _http(exc)
    return await _compute_response(db, organization.id, compute)


class BootstrapRequest(BaseModel):
    take_over: bool = False
    # Wait for the result instead of returning at once and being polled.
    wait: bool = False


async def _bootstrap_in_background(organization_id: str, actor_id: str, compute_id: str, take_over: bool) -> None:
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        try:
            compute = await compute_service.get_compute(db, organization_id, compute_id)
            await compute_service.bootstrap_compute(db, organization_id, actor_id, compute, take_over=take_over)
        except Exception:  # noqa: BLE001 - recorded on the compute by bootstrap_compute; nothing to return to
            import logging

            logging.getLogger(__name__).exception("sandbox compute bootstrap failed (compute=%s)", compute_id)
            return
    await _prefetch_images(organization_id, compute_id)


async def _prefetch_images(organization_id: str, compute_id: str) -> None:
    """Once a compute is ready, fetch the environments' images in the
    background. Never part of bootstrap: a failure here is only recorded."""
    try:
        await compute_service.prefetch_environment_images(organization_id, compute_id)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("could not prefetch sandbox images (compute=%s)", compute_id)


@router.post("/computes/{compute_id}/bootstrap", response_model=SandboxComputeSchema)
@requires_permission("manage_sandboxes")
async def bootstrap_compute(
    compute_id: str,
    data: BootstrapRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Prepare (or repair) a compute. Returns at once; the steps are stored on
    the compute as they finish, so reading the compute shows the progress."""
    from datetime import datetime

    from app.core.fire_and_forget import spawn

    try:
        compute = await compute_service.get_compute(db, organization.id, compute_id)
    except SandboxError as exc:
        raise _http(exc)
    previous_status = compute.bootstrap_status if compute.bootstrap_status != "running" else "failed"
    # Claimed atomically: a double click or two admins cannot both start one.
    if not await compute_service.claim_bootstrap(db, compute, stale_seconds=BOOTSTRAP_STALE_SECONDS):
        raise AppError("sandbox.bootstrap_running", "This compute is being bootstrapped already.", status_code=409)
    try:
        if data.wait:
            await compute_service.bootstrap_compute(db, organization.id, str(current_user.id), compute, take_over=data.take_over)
            spawn(_prefetch_images(str(organization.id), compute.id))
        else:
            await compute_service.mark_bootstrap_started(db, compute)
            spawn(_bootstrap_in_background(str(organization.id), str(current_user.id), compute.id, data.take_over))
    except SandboxError as exc:
        # Nothing was started (the driver could not even be built): give the claim back.
        compute.bootstrap_status = previous_status
        await db.commit()
        raise _http(exc)
    return await _compute_response(db, organization.id, compute)


@router.delete("/computes/{compute_id}")
@requires_permission("manage_sandboxes")
async def delete_compute(
    compute_id: str,
    cleanup: bool = False,
    delete_sandboxes: bool = False,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Delete a compute. Refused while it has sandboxes unless ``delete_sandboxes``
    is set; ``cleanup`` also removes everything BoW put on it."""
    from app.services.sandbox.service import SandboxService

    try:
        compute = await compute_service.get_compute(db, organization.id, compute_id)
        if delete_sandboxes:
            service = SandboxService()
            for sandbox in await compute_service.live_sandboxes(db, compute.id):
                await service.delete(db, sandbox, str(current_user.id), reason="compute deleted")
        await compute_service.delete_compute(db, organization.id, str(current_user.id), compute_id, cleanup=cleanup)
    except SandboxError as exc:
        raise _http(exc)
    return {"deleted": True}


# ── Sandboxes: what exists right now, and the switches to stop it ────────

from app.schemas.sandbox_schema import (  # noqa: E402
    SandboxDetail,
    SandboxEventSchema,
    SandboxFleetRow,
    SandboxRunSchema,
    SandboxSchema,
    SandboxUsage,
)


async def _fleet_rows(db: AsyncSession, service, sandboxes, *, with_usage: bool) -> List[SandboxFleetRow]:
    from sqlalchemy import select

    from app.models.report import Report
    from app.models.sandbox import SandboxCompute, SandboxEnvironment, SandboxRun
    from app.models.user import User as UserModel

    async def names(model, column, ids):
        ids = {i for i in ids if i}
        if not ids:
            return {}
        rows = await db.execute(select(model.id, column).where(model.id.in_(ids)))
        return {str(row[0]): row[1] for row in rows.all()}

    reports = await names(Report, Report.title, [s.report_id for s in sandboxes])
    users = await names(UserModel, UserModel.name, [s.user_id for s in sandboxes])
    computes = await names(SandboxCompute, SandboxCompute.name, [s.compute_id for s in sandboxes])
    environments = await names(SandboxEnvironment, SandboxEnvironment.name, [s.environment_id for s in sandboxes])
    env_ids = {s.environment_id for s in sandboxes if s.environment_id}
    current_setup = {}
    if env_ids:
        rows = await db.execute(select(SandboxEnvironment).where(SandboxEnvironment.id.in_(env_ids)))
        current_setup = {e.id: environment_service.setup_fingerprint(e) for e in rows.scalars().all()}
    running = {}
    busy = [s.id for s in sandboxes if s.state == "busy"]
    if busy:
        rows = await db.execute(
            select(SandboxRun.sandbox_id, SandboxRun.id, SandboxRun.started_at)
            .where(SandboxRun.sandbox_id.in_(busy), SandboxRun.status == "running")
            .order_by(SandboxRun.started_at)
        )
        running = {row[0]: (row[1], row[2]) for row in rows.all()}
    usage = await service.stats(db, sandboxes) if with_usage else {}

    out = []
    for sandbox in sandboxes:
        run_id, since = running.get(sandbox.id, (None, None))
        stats = usage.get(sandbox.id)
        out.append(SandboxFleetRow(
            **SandboxSchema.from_model(sandbox).model_dump(),
            report_title=reports.get(sandbox.report_id), user_name=users.get(sandbox.user_id or ""),
            compute_name=computes.get(sandbox.compute_id or ""),
            environment_name=environments.get(sandbox.environment_id or "") or (sandbox.limits or {}).get("environment"),
            running_run_id=run_id, running_since=since,
            setup_outdated=bool(
                (sandbox.limits or {}).get("setup") and sandbox.environment_id in current_setup
                and (sandbox.limits or {}).get("setup") != current_setup[sandbox.environment_id]
            ),
            usage=SandboxUsage(**{k: getattr(stats, k, None) for k in SandboxUsage.model_fields}) if stats else None,
        ))
    return out


@router.get("/sandboxes", response_model=List[SandboxFleetRow])
@requires_permission("manage_sandboxes")
async def list_sandboxes(
    usage: bool = False,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Every sandbox that exists in the organization. ``usage=true`` adds live
    CPU, memory and disk, which costs one call to each compute."""
    from app.services.sandbox.service import SandboxService

    service = SandboxService()
    return await _fleet_rows(db, service, await service.list_live(db, organization.id), with_usage=usage)


@router.get("/sandboxes/{sandbox_id}", response_model=SandboxDetail)
@requires_permission("manage_sandboxes")
async def get_sandbox(
    sandbox_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """One sandbox with its recent runs and its timeline. Code and commands
    are left out for a viewer without ``view_code``."""
    from sqlalchemy import select

    from app.core.code_visibility import can_view_code
    from app.core.permission_resolver import resolve_permissions
    from app.models.sandbox import SandboxEvent
    from app.services.sandbox.service import SandboxService

    service = SandboxService()
    try:
        sandbox = await service.get(db, organization.id, sandbox_id)
    except SandboxError as exc:
        raise _http(exc)
    row = (await _fleet_rows(db, service, [sandbox], with_usage=True))[0]
    include_code = can_view_code(await resolve_permissions(db, str(current_user.id), str(organization.id)))
    events = await db.execute(
        select(SandboxEvent).where(SandboxEvent.sandbox_id == sandbox.id).order_by(SandboxEvent.created_at.desc()).limit(200)
    )
    return SandboxDetail(
        **row.model_dump(),
        runs=[SandboxRunSchema.from_model(r, include_code=include_code, include_output=include_code)
              for r in await service.recent_runs(db, sandbox.id, limit=20)],
        events=[SandboxEventSchema.from_model(e) for e in events.scalars().all()],
    )


@router.post("/sandboxes/{sandbox_id}/stop", response_model=SandboxFleetRow)
@requires_permission("manage_sandboxes")
async def stop_sandbox(
    sandbox_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Stop one sandbox, keeping its files. Refused while it is running
    something: stop the run first."""
    from app.services.sandbox.service import SandboxService

    service = SandboxService()
    try:
        sandbox = await service.get(db, organization.id, sandbox_id)
        await service.stop(db, sandbox, str(current_user.id), reason="stopped by an admin")
        if sandbox.state != "stopped":
            # It was no longer on its server, so there was nothing to stop and it
            # is now marked failed. That is not "stopped": say what happened.
            raise SandboxError(
                errors.UNAVAILABLE,
                "The sandbox was not found where it should be running, so it could not be stopped. "
                "It is marked failed and will be recreated on the next run.", status=409,
            )
    except SandboxError as exc:
        raise _http(exc)
    return (await _fleet_rows(db, service, [sandbox], with_usage=False))[0]


@router.post("/sandboxes/{sandbox_id}/kill-run", response_model=SandboxFleetRow)
@requires_permission("manage_sandboxes")
async def kill_sandbox_run(
    sandbox_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Stop the run in progress in one sandbox. The sandbox and its files stay."""
    from app.services.sandbox.service import SandboxService

    service = SandboxService()
    try:
        sandbox = await service.get(db, organization.id, sandbox_id)
        await service.kill_run(db, sandbox, str(current_user.id))
    except SandboxError as exc:
        raise _http(exc)
    await db.refresh(sandbox)
    return (await _fleet_rows(db, service, [sandbox], with_usage=False))[0]


@router.delete("/sandboxes/{sandbox_id}")
@requires_permission("manage_sandboxes")
async def delete_sandbox(
    sandbox_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Delete one sandbox and everything in it. A run in progress is stopped
    first. Files already saved to the report are kept."""
    from app.services.sandbox.service import SandboxService

    service = SandboxService()
    try:
        sandbox = await service.get(db, organization.id, sandbox_id)
        if sandbox.state == "busy":
            try:
                await service.kill_run(db, sandbox, str(current_user.id))
            except SandboxError:
                pass  # nothing to signal yet, or it just ended: delete it either way
        await service.delete(db, sandbox, str(current_user.id), reason="deleted by an admin")
    except SandboxError as exc:
        raise _http(exc)
    return {"deleted": True}
