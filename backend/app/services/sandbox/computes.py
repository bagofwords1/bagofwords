"""Sandbox computes: the places sandboxes run.

Everything here is driver-neutral. A compute row holds what its type's
schemas describe; this module validates through the registry, builds the
driver, runs its steps and stores what it reports. It never looks inside a
compute's config or driver state.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.sandbox import Sandbox, SandboxCompute, SandboxEnvironment
from app.schemas.sandbox_schema import SandboxComputeCreate, SandboxComputeSchema, SandboxComputeUpdate
from app.services.sandbox import errors
from app.services.sandbox.audit import audit
from app.services.sandbox.drivers import registry
from app.services.sandbox.drivers.base import (
    ComputeContext, ComputeHealth, SandboxDriver, SandboxDriverError, StepResult,
)
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.settings import ensure_instance_id

logger = logging.getLogger(__name__)

# States in which a sandbox occupies capacity on its compute.
RUNNING_STATES = ("provisioning", "ready", "busy", "idle")
# States in which a sandbox still exists on its compute.
LIVE_STATES = RUNNING_STATES + ("stopped", "failed")


def _now() -> datetime:
    return datetime.utcnow()


def _bow_base_url() -> Optional[str]:
    from app.settings.config import settings

    return getattr(settings.bow_config, "base_url", None)


async def build_driver(db: AsyncSession, compute: SandboxCompute, *,
                       extra_credentials: Optional[Dict[str, Any]] = None) -> SandboxDriver:
    """The driver for one compute, built from its stored settings."""
    instance_id = await ensure_instance_id(db, compute.organization_id)
    credentials = dict(compute.credentials or {})
    credentials.update(extra_credentials or {})
    ctx = ComputeContext(
        compute_id=compute.id,
        name=compute.name,
        instance_id=instance_id,
        config=dict(compute.config or {}),
        credentials=credentials,
        driver_state=dict(compute.driver_state or {}),
        base_url=_bow_base_url(),
    )
    try:
        return registry.create_driver(compute.type, ctx)
    except SandboxDriverError as exc:
        raise SandboxError(errors.COMPUTE_NOT_READY, exc.message) from exc


async def store_driver_state(db: AsyncSession, compute: SandboxCompute, driver: SandboxDriver) -> None:
    """Write back what the driver learned (pinned keys, detected isolation, ...)."""
    state = dict(driver.ctx.driver_state or {})
    if state != dict(compute.driver_state or {}):
        compute.driver_state = state
        flag_modified(compute, "driver_state")
    capabilities = sorted(driver.capabilities())
    if capabilities != list(compute.capabilities or []):
        compute.capabilities = capabilities
        flag_modified(compute, "capabilities")
    await db.flush()


async def running_counts(db: AsyncSession, organization_id: str) -> Dict[str, int]:
    """Sandboxes occupying capacity, per compute id."""
    rows = await db.execute(
        select(Sandbox.compute_id, func.count())
        .where(Sandbox.organization_id == str(organization_id), Sandbox.deleted_at.is_(None),
               Sandbox.state.in_(RUNNING_STATES))
        .group_by(Sandbox.compute_id)
    )
    return {compute_id: count for compute_id, count in rows.all() if compute_id}


def to_schema(compute: SandboxCompute, running: int = 0) -> SandboxComputeSchema:
    try:
        info = registry.public_info(compute.type, compute.driver_state)
    except SandboxDriverError:
        info = {}
    return SandboxComputeSchema.from_model(
        compute, public_config=dict(compute.config or {}), info=info, running_sandboxes=running,
    )


async def get_compute(db: AsyncSession, organization_id: str, compute_id: str) -> SandboxCompute:
    compute = (await db.execute(
        select(SandboxCompute).where(
            SandboxCompute.id == str(compute_id), SandboxCompute.organization_id == str(organization_id),
            SandboxCompute.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if compute is None:
        raise SandboxError(errors.COMPUTE_NOT_FOUND, "Sandbox compute not found", status=404)
    return compute


async def list_computes(db: AsyncSession, organization_id: str) -> List[SandboxCompute]:
    rows = await db.execute(
        select(SandboxCompute)
        .where(SandboxCompute.organization_id == str(organization_id), SandboxCompute.deleted_at.is_(None))
        .order_by(SandboxCompute.name)
    )
    return list(rows.scalars().all())


async def _name_taken(db: AsyncSession, organization_id: str, name: str, except_id: Optional[str] = None) -> bool:
    query = select(SandboxCompute.id).where(
        SandboxCompute.organization_id == str(organization_id), SandboxCompute.deleted_at.is_(None),
        func.lower(SandboxCompute.name) == name.lower(),
    )
    if except_id:
        query = query.where(SandboxCompute.id != except_id)
    return (await db.execute(query)).first() is not None


def _split_credentials(type_name: str, credentials: Optional[Dict[str, Any]]) -> tuple:
    """(stored, transient): one-time credentials are used once and never stored."""
    try:
        cleaned = registry.validate_credentials(type_name, credentials)
        transient_fields = set(registry.transient_credential_fields(type_name))
    except SandboxDriverError as exc:
        raise SandboxError(errors.INVALID, exc.message) from exc
    stored = {k: v for k, v in cleaned.items() if k not in transient_fields}
    transient = {k: v for k, v in cleaned.items() if k in transient_fields}
    return stored, transient


def _complete(type_name: str, config: Dict[str, Any], credentials: Dict[str, Any], *,
              previous_config: Dict[str, Any]) -> tuple:
    """Fields the driver owns keep their stored value whatever was sent; then
    the driver completes the settings (for example by generating a key pair)."""
    try:
        for field in registry.readonly_config_fields(type_name):
            if field in previous_config:
                config[field] = previous_config[field]
            else:
                config.pop(field, None)
        return registry.prepare_credentials(type_name, config, credentials)
    except SandboxDriverError as exc:
        raise SandboxError(errors.INVALID, exc.message) from exc


async def confirm_identity(db: AsyncSession, organization_id: str, actor_id: Optional[str],
                           compute: SandboxCompute, fingerprint: str) -> None:
    """Trust the compute's identity after the admin confirmed the fingerprint Test connection showed."""
    driver = await build_driver(db, compute)
    try:
        await driver.confirm_identity(fingerprint)
        await store_driver_state(db, compute, driver)
    except SandboxDriverError as exc:
        raise SandboxError(errors.INVALID if exc.code != "unreachable" else errors.UNAVAILABLE, exc.message) from exc
    finally:
        await driver.close()
    await audit(db, organization_id, "sandbox_compute.identity_confirmed", actor_id, "sandbox_compute", compute.id,
                {"title": compute.name, "fingerprint": fingerprint})
    await db.commit()


async def _enroll(db: AsyncSession, compute: SandboxCompute, transient: Dict[str, Any]) -> None:
    """Use one-time credentials once. They are in memory for this call only."""
    if not transient:
        return
    driver = await build_driver(db, compute)
    try:
        await driver.enroll(transient)
        await store_driver_state(db, compute, driver)
    except SandboxDriverError as exc:
        raise SandboxError(errors.UNAVAILABLE, exc.message) from exc
    finally:
        await driver.close()


async def create_compute(db: AsyncSession, organization_id: str, actor_id: Optional[str],
                         data: SandboxComputeCreate) -> SandboxCompute:
    try:
        registry.get_compute_type(data.type)
        config = registry.validate_config(data.type, data.config)
    except SandboxDriverError as exc:
        raise SandboxError(errors.INVALID, exc.message) from exc
    stored, transient = _split_credentials(data.type, data.credentials)
    if await _name_taken(db, organization_id, data.name):
        raise SandboxError(errors.NAME_TAKEN, f"A sandbox compute named '{data.name}' already exists", status=409)
    config, stored = _complete(data.type, config, stored, previous_config={})

    compute = SandboxCompute(
        organization_id=str(organization_id), name=data.name.strip(), type=data.type, config=config,
        credentials=stored or None, driver_state={}, capabilities=[], labels=list(data.labels),
        capacity=data.capacity, enabled=False, bootstrap_status="none", bootstrap_steps=[],
        status="unknown", created_by=actor_id,
    )
    db.add(compute)
    await db.flush()
    await audit(db, organization_id, "sandbox_compute.created", actor_id, "sandbox_compute", compute.id,
                {"title": compute.name, "type": compute.type, "labels": list(compute.labels or []),
                 "capacity": compute.capacity, "stored_fields": sorted(stored)})
    await _enroll(db, compute, transient)
    await db.commit()
    return compute


async def update_compute(db: AsyncSession, organization_id: str, actor_id: Optional[str], compute_id: str,
                         data: SandboxComputeUpdate) -> SandboxCompute:
    compute = await get_compute(db, organization_id, compute_id)
    changes = data.model_dump(exclude_unset=True)
    changed: List[str] = []
    transient: Dict[str, Any] = {}

    if "name" in changes and changes["name"] and changes["name"].strip() != compute.name:
        if await _name_taken(db, organization_id, changes["name"], except_id=compute.id):
            raise SandboxError(errors.NAME_TAKEN, f"A sandbox compute named '{changes['name']}' already exists", status=409)
        compute.name = changes["name"].strip()
        changed.append("name")
    if changes.get("config") is not None or changes.get("credentials") is not None:
        previous_config = dict(compute.config or {})
        config = previous_config
        if changes.get("config") is not None:
            try:
                config = registry.validate_config(compute.type, changes["config"])
            except SandboxDriverError as exc:
                raise SandboxError(errors.INVALID, exc.message) from exc
        stored: Dict[str, Any] = {}
        if changes.get("credentials") is not None:
            stored, transient = _split_credentials(compute.type, changes["credentials"])
        merged = dict(compute.credentials or {})
        merged.update(stored)
        config, merged = _complete(compute.type, config, merged, previous_config=previous_config)
        if config != previous_config:
            if registry.is_another_compute(compute.type, previous_config, config):
                # It now points at a different server. Nothing learned about the
                # old one holds: not its pinned identity, not its certificate,
                # not "bootstrapped". Start over, disabled, so no sandbox is
                # placed there before the admin has confirmed and bootstrapped it.
                remaining = await live_sandbox_count(db, compute.id)
                if remaining:
                    raise SandboxError(
                        errors.COMPUTE_IN_USE,
                        f"This compute still has {remaining} sandbox(es) on the current server. Delete them "
                        "before pointing it at another one.", status=409,
                    )
                compute.driver_state = {}
                flag_modified(compute, "driver_state")
                compute.bootstrap_status, compute.bootstrap_steps = "none", []
                flag_modified(compute, "bootstrap_steps")
                compute.enabled = False
                compute.status, compute.last_error, compute.last_checked_at = "unknown", None, None
                changed.append("server")
            compute.config = config
            flag_modified(compute, "config")
            changed.append("config")
        if stored:
            compute.credentials = merged
            changed.append("credentials")
    if changes.get("labels") is not None and list(changes["labels"]) != list(compute.labels or []):
        compute.labels = list(changes["labels"])
        flag_modified(compute, "labels")
        changed.append("labels")
    if changes.get("capacity") is not None and changes["capacity"] != compute.capacity:
        compute.capacity = changes["capacity"]
        changed.append("capacity")

    if changed:
        await audit(db, organization_id, "sandbox_compute.updated", actor_id, "sandbox_compute", compute.id,
                    {"title": compute.name, "changed": changed})
    if changes.get("enabled") is not None and bool(changes["enabled"]) != bool(compute.enabled):
        await set_enabled(db, organization_id, actor_id, compute, bool(changes["enabled"]), commit=False)
    await db.flush()
    await _enroll(db, compute, transient)
    await db.commit()
    return compute


async def set_enabled(db: AsyncSession, organization_id: str, actor_id: Optional[str], compute: SandboxCompute,
                      enabled: bool, *, commit: bool = True) -> SandboxCompute:
    """Enable or disable. Disabling drains: existing sandboxes stay, no new ones start."""
    if enabled and compute.bootstrap_status != "ready":
        raise SandboxError(
            errors.COMPUTE_NOT_READY, "Bootstrap this compute before enabling it.", status=409,
        )
    compute.enabled = enabled
    await audit(db, organization_id, "sandbox_compute.enabled" if enabled else "sandbox_compute.disabled",
                actor_id, "sandbox_compute", compute.id, {"title": compute.name})
    if commit:
        await db.commit()
    return compute


def _apply_health(compute: SandboxCompute, health: ComputeHealth) -> None:
    compute.last_checked_at = _now()
    if not health.reachable:
        compute.status = "unreachable"
        compute.last_error = health.error or "The compute could not be reached"
    else:
        compute.status = "healthy" if health.healthy else "unknown"
        compute.last_error = None if health.healthy else health.error


async def check_compute(db: AsyncSession, organization_id: str, compute: SandboxCompute) -> ComputeHealth:
    """Test connection: read-only. Stores what it learned and returns the per-check lines."""
    driver = await build_driver(db, compute)
    try:
        health = await driver.check()
    except SandboxDriverError as exc:
        health = ComputeHealth(reachable=False, error=exc.message,
                               checks=[{"key": "connect", "title": "Connect", "status": "failed", "detail": exc.message}])
    finally:
        await driver.close()
    _apply_health(compute, health)
    await store_driver_state(db, compute, driver)
    await db.commit()
    return health


async def claim_bootstrap(db: AsyncSession, compute: SandboxCompute, *, stale_seconds: int) -> bool:
    """Take the right to bootstrap this compute, atomically: two requests at the
    same moment cannot both get it. A claim older than ``stale_seconds`` is
    taken over (its worker died)."""
    now = _now()
    claimed = await db.execute(
        update(SandboxCompute).where(
            SandboxCompute.id == compute.id,
            or_(
                SandboxCompute.bootstrap_status != "running",
                SandboxCompute.updated_at.is_(None),
                SandboxCompute.updated_at < now - timedelta(seconds=stale_seconds),
            ),
        ).values(bootstrap_status="running", updated_at=now)
    )
    await db.commit()
    await db.refresh(compute)
    return claimed.rowcount == 1


async def mark_bootstrap_started(db: AsyncSession, compute: SandboxCompute, driver: Optional[SandboxDriver] = None) -> List[dict]:
    """Record that a bootstrap is under way, with its steps all pending."""
    own = driver is None
    driver = driver or await build_driver(db, compute)
    try:
        steps = [StepResult(key=s.key, title=s.title).as_dict() for s in driver.prepare_steps()]
    finally:
        if own:
            await driver.close()
    compute.bootstrap_status = "running"
    compute.bootstrap_steps = steps
    compute.updated_at = _now()
    flag_modified(compute, "bootstrap_steps")
    await db.commit()
    return steps


async def live_sandboxes(db: AsyncSession, compute_id: str) -> List[Sandbox]:
    rows = await db.execute(
        select(Sandbox).where(Sandbox.compute_id == str(compute_id), Sandbox.deleted_at.is_(None),
                              Sandbox.state.in_(LIVE_STATES))
    )
    return list(rows.scalars().all())


async def bootstrap_compute(db: AsyncSession, organization_id: str, actor_id: Optional[str],
                            compute: SandboxCompute, *, take_over: bool = False) -> List[StepResult]:
    """Prepare a compute. Each step's result is stored as it happens, so any
    worker can show progress. A failed step leaves the compute disabled.

    Bootstrap is about the compute only: Docker, isolation, the network, the
    router, the egress proxy, the firewall. It does not depend on which
    environments exist, so an environment with an image that cannot be
    fetched never fails it. Environment images are fetched separately
    (``prefetch_environment_images``, on saving an environment, or when the
    first sandbox is created)."""
    driver = await build_driver(db, compute)
    await mark_bootstrap_started(db, compute, driver)

    async def on_step(result: StepResult) -> None:
        current = [dict(s) for s in (compute.bootstrap_steps or [])]
        for index, step in enumerate(current):
            if step["key"] == result.key:
                current[index] = result.as_dict()
        compute.bootstrap_steps = current
        flag_modified(compute, "bootstrap_steps")
        await db.commit()

    try:
        results = await driver.prepare(on_step, take_over=take_over)
    except SandboxDriverError as exc:
        results = [StepResult(key="prepare", title="Prepare the compute", status="failed", detail=exc.message)]
    except Exception as exc:  # noqa: BLE001 - a driver bug must not leave "running" forever
        logger.exception("sandbox compute bootstrap crashed (compute=%s)", compute.id)
        results = [StepResult(key="prepare", title="Prepare the compute", status="failed",
                              detail=f"Unexpected error: {type(exc).__name__}")]
    finally:
        await driver.close()

    failed = next((r for r in results if r.status == "failed"), None)
    compute.bootstrap_steps = [r.as_dict() for r in results]
    flag_modified(compute, "bootstrap_steps")
    if failed is None:
        compute.bootstrap_status = "ready"
        compute.bootstrapped_at = _now()
        compute.status, compute.last_error, compute.last_checked_at = "healthy", None, _now()
    else:
        compute.bootstrap_status = "failed"
        compute.enabled = False  # a host cannot be enabled until bootstrap has succeeded
        compute.last_error = failed.detail
    await store_driver_state(db, compute, driver)
    await audit(
        db, organization_id, "sandbox_compute.taken_over" if take_over else "sandbox_compute.bootstrapped",
        actor_id, "sandbox_compute", compute.id,
        {"title": compute.name, "result": compute.bootstrap_status,
         "failed_step": failed.key if failed else None,
         "warnings": [r.key for r in results if r.status == "warning"]},
        outcome="success" if failed is None else "failure",
    )
    await db.commit()
    return results


async def _environments_matching(db: AsyncSession, organization_id: str, compute: SandboxCompute) -> Sequence[SandboxEnvironment]:
    rows = await db.execute(
        select(SandboxEnvironment).where(
            SandboxEnvironment.organization_id == str(organization_id), SandboxEnvironment.deleted_at.is_(None),
            SandboxEnvironment.enabled.is_(True),
        )
    )
    labels = set(compute.labels or [])
    return [env for env in rows.scalars().all() if set(env.compute_labels or []) <= labels]


async def prefetch_environment_images(organization_id: str, compute_id: str, session_factory=None) -> Dict[str, str]:
    """After a compute is bootstrapped: fetch the image of every environment it
    could run, so the first sandbox does not wait for a download. Best effort.
    A failure is recorded (log and audit) and changes nothing about the compute;
    the image is fetched again when a sandbox is created. Returns
    ``{environment name: outcome}``."""
    from app.services.sandbox.environments import pinned_image

    if session_factory is None:
        from app.dependencies import async_session_maker as session_factory

    outcomes: Dict[str, str] = {}
    async with session_factory() as db:
        try:
            compute = await get_compute(db, organization_id, compute_id)
            if compute.bootstrap_status != "ready":
                return outcomes
            environments = list(await _environments_matching(db, organization_id, compute))
            if not environments:
                return outcomes
            driver = await build_driver(db, compute)
        except SandboxError:
            return outcomes
        try:
            for env in environments:
                try:
                    # The digest an environment is pinned to is set when the
                    # environment is saved, not here: this only warms the cache.
                    await driver.pull_image(
                        pinned_image(env), registry_username=env.registry_username,
                        registry_password=env.registry_password,
                    )
                    outcomes[env.name] = "ok"
                except SandboxDriverError as exc:
                    outcomes[env.name] = exc.message
                    logger.warning("could not fetch the image of sandbox environment %s on compute %s: %s",
                                   env.name, compute.name, exc.message)
        finally:
            await driver.close()
        failed = sorted(name for name, outcome in outcomes.items() if outcome != "ok")
        await audit(db, organization_id, "sandbox_compute.images_fetched", None, "sandbox_compute", compute.id,
                    {"title": compute.name, "environments": outcomes, "failed": failed},
                    outcome="failure" if failed else "success")
        await db.commit()
    return outcomes


async def live_sandbox_count(db: AsyncSession, compute_id: str) -> int:
    return (await db.execute(
        select(func.count()).select_from(Sandbox).where(
            Sandbox.compute_id == str(compute_id), Sandbox.deleted_at.is_(None), Sandbox.state.in_(LIVE_STATES),
        )
    )).scalar() or 0


async def delete_compute(db: AsyncSession, organization_id: str, actor_id: Optional[str], compute_id: str,
                         *, cleanup: bool = False) -> None:
    """Delete a compute. Refused while it still has sandboxes."""
    compute = await get_compute(db, organization_id, compute_id)
    remaining = await live_sandbox_count(db, compute.id)
    if remaining:
        raise SandboxError(
            errors.COMPUTE_IN_USE,
            f"This compute still has {remaining} sandbox(es). Delete them first.", status=409,
        )
    cleaned = False
    if cleanup:
        driver = await build_driver(db, compute)
        try:
            await driver.cleanup()
            cleaned = True
        except SandboxDriverError as exc:
            raise SandboxError(errors.UNAVAILABLE, f"Could not clean up the compute: {exc.message}") from exc
        finally:
            await driver.close()
    compute.deleted_at = _now()
    compute.enabled = False
    compute.credentials = None
    await audit(db, organization_id, "sandbox_compute.deleted", actor_id, "sandbox_compute", compute.id,
                {"title": compute.name, "cleaned_up": cleaned})
    await db.commit()
