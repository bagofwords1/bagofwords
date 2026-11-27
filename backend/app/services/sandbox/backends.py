"""Sandbox backends: the places sandboxes run.

Everything here is driver-neutral. A backend row holds what its type's
schemas describe; this module validates through the registry, builds the
driver, runs its steps and stores what it reports. It never looks inside a
backend's config or driver state.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.sandbox import Sandbox, SandboxBackend, SandboxEnvironment
from app.schemas.sandbox_schema import SandboxBackendCreate, SandboxBackendSchema, SandboxBackendUpdate
from app.services.sandbox import errors
from app.services.sandbox.audit import audit
from app.services.sandbox.drivers import registry
from app.services.sandbox.drivers.base import (
    BackendContext, BackendHealth, SandboxDriver, SandboxDriverError, StepResult,
)
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.settings import ensure_instance_id

logger = logging.getLogger(__name__)

# States in which a sandbox occupies capacity on its backend.
RUNNING_STATES = ("provisioning", "ready", "busy", "idle")
# States in which a sandbox still exists on its backend.
LIVE_STATES = RUNNING_STATES + ("stopped", "failed")


def _now() -> datetime:
    return datetime.utcnow()


def _bow_base_url() -> Optional[str]:
    from app.settings.config import settings

    return getattr(settings.bow_config, "base_url", None)


async def build_driver(db: AsyncSession, backend: SandboxBackend, *,
                       extra_credentials: Optional[Dict[str, Any]] = None) -> SandboxDriver:
    """The driver for one backend, built from its stored settings."""
    instance_id = await ensure_instance_id(db, backend.organization_id)
    credentials = dict(backend.credentials or {})
    credentials.update(extra_credentials or {})
    ctx = BackendContext(
        backend_id=backend.id,
        name=backend.name,
        instance_id=instance_id,
        config=dict(backend.config or {}),
        credentials=credentials,
        driver_state=dict(backend.driver_state or {}),
        base_url=_bow_base_url(),
    )
    try:
        return registry.create_driver(backend.type, ctx)
    except SandboxDriverError as exc:
        raise SandboxError(errors.BACKEND_NOT_READY, exc.message) from exc


async def store_driver_state(db: AsyncSession, backend: SandboxBackend, driver: SandboxDriver) -> None:
    """Write back what the driver learned (pinned keys, detected isolation, ...)."""
    state = dict(driver.ctx.driver_state or {})
    if state != dict(backend.driver_state or {}):
        backend.driver_state = state
        flag_modified(backend, "driver_state")
    capabilities = sorted(driver.capabilities())
    if capabilities != list(backend.capabilities or []):
        backend.capabilities = capabilities
        flag_modified(backend, "capabilities")
    await db.flush()


async def running_counts(db: AsyncSession, organization_id: str) -> Dict[str, int]:
    """Sandboxes occupying capacity, per backend id."""
    rows = await db.execute(
        select(Sandbox.backend_id, func.count())
        .where(Sandbox.organization_id == str(organization_id), Sandbox.deleted_at.is_(None),
               Sandbox.state.in_(RUNNING_STATES))
        .group_by(Sandbox.backend_id)
    )
    return {backend_id: count for backend_id, count in rows.all() if backend_id}


def to_schema(backend: SandboxBackend, running: int = 0) -> SandboxBackendSchema:
    try:
        info = registry.public_info(backend.type, backend.driver_state)
    except SandboxDriverError:
        info = {}
    return SandboxBackendSchema.from_model(
        backend, public_config=dict(backend.config or {}), info=info, running_sandboxes=running,
    )


async def get_backend(db: AsyncSession, organization_id: str, backend_id: str) -> SandboxBackend:
    backend = (await db.execute(
        select(SandboxBackend).where(
            SandboxBackend.id == str(backend_id), SandboxBackend.organization_id == str(organization_id),
            SandboxBackend.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if backend is None:
        raise SandboxError(errors.BACKEND_NOT_FOUND, "Sandbox backend not found", status=404)
    return backend


async def list_backends(db: AsyncSession, organization_id: str) -> List[SandboxBackend]:
    rows = await db.execute(
        select(SandboxBackend)
        .where(SandboxBackend.organization_id == str(organization_id), SandboxBackend.deleted_at.is_(None))
        .order_by(SandboxBackend.name)
    )
    return list(rows.scalars().all())


async def _name_taken(db: AsyncSession, organization_id: str, name: str, except_id: Optional[str] = None) -> bool:
    query = select(SandboxBackend.id).where(
        SandboxBackend.organization_id == str(organization_id), SandboxBackend.deleted_at.is_(None),
        func.lower(SandboxBackend.name) == name.lower(),
    )
    if except_id:
        query = query.where(SandboxBackend.id != except_id)
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
                           backend: SandboxBackend, fingerprint: str) -> None:
    """Trust the backend's identity after the admin confirmed the fingerprint Test connection showed."""
    driver = await build_driver(db, backend)
    try:
        await driver.confirm_identity(fingerprint)
        await store_driver_state(db, backend, driver)
    except SandboxDriverError as exc:
        raise SandboxError(errors.INVALID if exc.code != "unreachable" else errors.UNAVAILABLE, exc.message) from exc
    finally:
        await driver.close()
    await audit(db, organization_id, "sandbox_backend.identity_confirmed", actor_id, "sandbox_backend", backend.id,
                {"title": backend.name, "fingerprint": fingerprint})
    await db.commit()


async def _enroll(db: AsyncSession, backend: SandboxBackend, transient: Dict[str, Any]) -> None:
    """Use one-time credentials once. They are in memory for this call only."""
    if not transient:
        return
    driver = await build_driver(db, backend)
    try:
        await driver.enroll(transient)
        await store_driver_state(db, backend, driver)
    except SandboxDriverError as exc:
        raise SandboxError(errors.UNAVAILABLE, exc.message) from exc
    finally:
        await driver.close()


async def create_backend(db: AsyncSession, organization_id: str, actor_id: Optional[str],
                         data: SandboxBackendCreate) -> SandboxBackend:
    try:
        registry.get_backend_type(data.type)
        config = registry.validate_config(data.type, data.config)
    except SandboxDriverError as exc:
        raise SandboxError(errors.INVALID, exc.message) from exc
    stored, transient = _split_credentials(data.type, data.credentials)
    if await _name_taken(db, organization_id, data.name):
        raise SandboxError(errors.NAME_TAKEN, f"A sandbox backend named '{data.name}' already exists", status=409)
    config, stored = _complete(data.type, config, stored, previous_config={})

    backend = SandboxBackend(
        organization_id=str(organization_id), name=data.name.strip(), type=data.type, config=config,
        credentials=stored or None, driver_state={}, capabilities=[], labels=list(data.labels),
        capacity=data.capacity, enabled=False, bootstrap_status="none", bootstrap_steps=[],
        status="unknown", created_by=actor_id,
    )
    db.add(backend)
    await db.flush()
    await audit(db, organization_id, "sandbox_backend.created", actor_id, "sandbox_backend", backend.id,
                {"title": backend.name, "type": backend.type, "labels": list(backend.labels or []),
                 "capacity": backend.capacity, "stored_fields": sorted(stored)})
    await _enroll(db, backend, transient)
    await db.commit()
    return backend


async def update_backend(db: AsyncSession, organization_id: str, actor_id: Optional[str], backend_id: str,
                         data: SandboxBackendUpdate) -> SandboxBackend:
    backend = await get_backend(db, organization_id, backend_id)
    changes = data.model_dump(exclude_unset=True)
    changed: List[str] = []
    transient: Dict[str, Any] = {}

    if "name" in changes and changes["name"] and changes["name"].strip() != backend.name:
        if await _name_taken(db, organization_id, changes["name"], except_id=backend.id):
            raise SandboxError(errors.NAME_TAKEN, f"A sandbox backend named '{changes['name']}' already exists", status=409)
        backend.name = changes["name"].strip()
        changed.append("name")
    if changes.get("config") is not None or changes.get("credentials") is not None:
        previous_config = dict(backend.config or {})
        config = previous_config
        if changes.get("config") is not None:
            try:
                config = registry.validate_config(backend.type, changes["config"])
            except SandboxDriverError as exc:
                raise SandboxError(errors.INVALID, exc.message) from exc
        stored: Dict[str, Any] = {}
        if changes.get("credentials") is not None:
            stored, transient = _split_credentials(backend.type, changes["credentials"])
        merged = dict(backend.credentials or {})
        merged.update(stored)
        config, merged = _complete(backend.type, config, merged, previous_config=previous_config)
        if config != previous_config:
            if registry.is_another_backend(backend.type, previous_config, config):
                # It now points at a different server. Nothing learned about the
                # old one holds: not its pinned identity, not its certificate,
                # not "bootstrapped". Start over, disabled, so no sandbox is
                # placed there before the admin has confirmed and bootstrapped it.
                remaining = await live_sandbox_count(db, backend.id)
                if remaining:
                    raise SandboxError(
                        errors.BACKEND_IN_USE,
                        f"This backend still has {remaining} sandbox(es) on the current server. Delete them "
                        "before pointing it at another one.", status=409,
                    )
                backend.driver_state = {}
                flag_modified(backend, "driver_state")
                backend.bootstrap_status, backend.bootstrap_steps = "none", []
                flag_modified(backend, "bootstrap_steps")
                backend.enabled = False
                backend.status, backend.last_error, backend.last_checked_at = "unknown", None, None
                changed.append("server")
            backend.config = config
            flag_modified(backend, "config")
            changed.append("config")
        if stored:
            backend.credentials = merged
            changed.append("credentials")
    if changes.get("labels") is not None and list(changes["labels"]) != list(backend.labels or []):
        backend.labels = list(changes["labels"])
        flag_modified(backend, "labels")
        changed.append("labels")
    if changes.get("capacity") is not None and changes["capacity"] != backend.capacity:
        backend.capacity = changes["capacity"]
        changed.append("capacity")

    if changed:
        await audit(db, organization_id, "sandbox_backend.updated", actor_id, "sandbox_backend", backend.id,
                    {"title": backend.name, "changed": changed})
    if changes.get("enabled") is not None and bool(changes["enabled"]) != bool(backend.enabled):
        await set_enabled(db, organization_id, actor_id, backend, bool(changes["enabled"]), commit=False)
    await db.flush()
    await _enroll(db, backend, transient)
    await db.commit()
    return backend


async def set_enabled(db: AsyncSession, organization_id: str, actor_id: Optional[str], backend: SandboxBackend,
                      enabled: bool, *, commit: bool = True) -> SandboxBackend:
    """Enable or disable. Disabling drains: existing sandboxes stay, no new ones start."""
    if enabled and backend.bootstrap_status != "ready":
        raise SandboxError(
            errors.BACKEND_NOT_READY, "Bootstrap this backend before enabling it.", status=409,
        )
    backend.enabled = enabled
    await audit(db, organization_id, "sandbox_backend.enabled" if enabled else "sandbox_backend.disabled",
                actor_id, "sandbox_backend", backend.id, {"title": backend.name})
    if commit:
        await db.commit()
    return backend


def _apply_health(backend: SandboxBackend, health: BackendHealth) -> None:
    backend.last_checked_at = _now()
    if not health.reachable:
        backend.status = "unreachable"
        backend.last_error = health.error or "The backend could not be reached"
    else:
        backend.status = "healthy" if health.healthy else "unknown"
        backend.last_error = None if health.healthy else health.error


async def check_backend(db: AsyncSession, organization_id: str, backend: SandboxBackend) -> BackendHealth:
    """Test connection: read-only. Stores what it learned and returns the per-check lines."""
    driver = await build_driver(db, backend)
    try:
        health = await driver.check()
    except SandboxDriverError as exc:
        health = BackendHealth(reachable=False, error=exc.message,
                               checks=[{"key": "connect", "title": "Connect", "status": "failed", "detail": exc.message}])
    finally:
        await driver.close()
    _apply_health(backend, health)
    await store_driver_state(db, backend, driver)
    await db.commit()
    return health


async def claim_bootstrap(db: AsyncSession, backend: SandboxBackend, *, stale_seconds: int) -> bool:
    """Take the right to bootstrap this backend, atomically: two requests at the
    same moment cannot both get it. A claim older than ``stale_seconds`` is
    taken over (its worker died)."""
    now = _now()
    claimed = await db.execute(
        update(SandboxBackend).where(
            SandboxBackend.id == backend.id,
            or_(
                SandboxBackend.bootstrap_status != "running",
                SandboxBackend.updated_at.is_(None),
                SandboxBackend.updated_at < now - timedelta(seconds=stale_seconds),
            ),
        ).values(bootstrap_status="running", updated_at=now)
    )
    await db.commit()
    await db.refresh(backend)
    return claimed.rowcount == 1


async def mark_bootstrap_started(db: AsyncSession, backend: SandboxBackend, driver: Optional[SandboxDriver] = None) -> List[dict]:
    """Record that a bootstrap is under way, with its steps all pending."""
    own = driver is None
    driver = driver or await build_driver(db, backend)
    try:
        steps = [StepResult(key=s.key, title=s.title).as_dict() for s in driver.prepare_steps()]
    finally:
        if own:
            await driver.close()
    backend.bootstrap_status = "running"
    backend.bootstrap_steps = steps
    backend.updated_at = _now()
    flag_modified(backend, "bootstrap_steps")
    await db.commit()
    return steps


async def live_sandboxes(db: AsyncSession, backend_id: str) -> List[Sandbox]:
    rows = await db.execute(
        select(Sandbox).where(Sandbox.backend_id == str(backend_id), Sandbox.deleted_at.is_(None),
                              Sandbox.state.in_(LIVE_STATES))
    )
    return list(rows.scalars().all())


async def bootstrap_backend(db: AsyncSession, organization_id: str, actor_id: Optional[str],
                            backend: SandboxBackend, *, take_over: bool = False) -> List[StepResult]:
    """Prepare a backend. Each step's result is stored as it happens, so any
    worker can show progress. A failed step leaves the backend disabled."""
    from app.services.sandbox.environments import pinned_image

    images = [pinned_image(env) for env in await _environments_matching(db, organization_id, backend)]
    driver = await build_driver(db, backend)
    await mark_bootstrap_started(db, backend, driver)

    async def on_step(result: StepResult) -> None:
        current = [dict(s) for s in (backend.bootstrap_steps or [])]
        for index, step in enumerate(current):
            if step["key"] == result.key:
                current[index] = result.as_dict()
        backend.bootstrap_steps = current
        flag_modified(backend, "bootstrap_steps")
        await db.commit()

    try:
        results = await driver.prepare(on_step, take_over=take_over, images=images)
    except SandboxDriverError as exc:
        results = [StepResult(key="prepare", title="Prepare the backend", status="failed", detail=exc.message)]
    except Exception as exc:  # noqa: BLE001 - a driver bug must not leave "running" forever
        logger.exception("sandbox backend bootstrap crashed (backend=%s)", backend.id)
        results = [StepResult(key="prepare", title="Prepare the backend", status="failed",
                              detail=f"Unexpected error: {type(exc).__name__}")]
    finally:
        await driver.close()

    failed = next((r for r in results if r.status == "failed"), None)
    backend.bootstrap_steps = [r.as_dict() for r in results]
    flag_modified(backend, "bootstrap_steps")
    if failed is None:
        backend.bootstrap_status = "ready"
        backend.bootstrapped_at = _now()
        backend.status, backend.last_error, backend.last_checked_at = "healthy", None, _now()
    else:
        backend.bootstrap_status = "failed"
        backend.enabled = False  # a host cannot be enabled until bootstrap has succeeded
        backend.last_error = failed.detail
    await store_driver_state(db, backend, driver)
    await audit(
        db, organization_id, "sandbox_backend.taken_over" if take_over else "sandbox_backend.bootstrapped",
        actor_id, "sandbox_backend", backend.id,
        {"title": backend.name, "result": backend.bootstrap_status,
         "failed_step": failed.key if failed else None,
         "warnings": [r.key for r in results if r.status == "warning"]},
        outcome="success" if failed is None else "failure",
    )
    await db.commit()
    return results


async def _environments_matching(db: AsyncSession, organization_id: str, backend: SandboxBackend) -> Sequence[SandboxEnvironment]:
    rows = await db.execute(
        select(SandboxEnvironment).where(
            SandboxEnvironment.organization_id == str(organization_id), SandboxEnvironment.deleted_at.is_(None),
            SandboxEnvironment.enabled.is_(True),
        )
    )
    labels = set(backend.labels or [])
    return [env for env in rows.scalars().all() if set(env.backend_labels or []) <= labels]


async def live_sandbox_count(db: AsyncSession, backend_id: str) -> int:
    return (await db.execute(
        select(func.count()).select_from(Sandbox).where(
            Sandbox.backend_id == str(backend_id), Sandbox.deleted_at.is_(None), Sandbox.state.in_(LIVE_STATES),
        )
    )).scalar() or 0


async def delete_backend(db: AsyncSession, organization_id: str, actor_id: Optional[str], backend_id: str,
                         *, cleanup: bool = False) -> None:
    """Delete a backend. Refused while it still has sandboxes."""
    backend = await get_backend(db, organization_id, backend_id)
    remaining = await live_sandbox_count(db, backend.id)
    if remaining:
        raise SandboxError(
            errors.BACKEND_IN_USE,
            f"This backend still has {remaining} sandbox(es). Delete them first.", status=409,
        )
    cleaned = False
    if cleanup:
        driver = await build_driver(db, backend)
        try:
            await driver.cleanup()
            cleaned = True
        except SandboxDriverError as exc:
            raise SandboxError(errors.UNAVAILABLE, f"Could not clean up the backend: {exc.message}") from exc
        finally:
            await driver.close()
    backend.deleted_at = _now()
    backend.enabled = False
    backend.credentials = None
    await audit(db, organization_id, "sandbox_backend.deleted", actor_id, "sandbox_backend", backend.id,
                {"title": backend.name, "cleaned_up": cleaned})
    await db.commit()
