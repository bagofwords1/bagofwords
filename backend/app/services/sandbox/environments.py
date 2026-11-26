"""Sandbox environments: the admin-defined templates sandboxes are created from.

An environment says what a sandbox is (image, resources, run timeout), what
it may reach (network policy), what it carries (variables and secrets) and
where it may run (backend labels, strong isolation). Who may use it is the
per-environment ``use`` grant, handled in ``access.py``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.sandbox import SandboxAgentDefault, SandboxBackend, SandboxEnvironment
from app.schemas.sandbox_schema import SandboxEnvironmentCreate, SandboxEnvironmentUpdate
from app.services.sandbox import errors
from app.services.sandbox.audit import audit
from app.services.sandbox.drivers.base import (
    CAP_DOMAIN_ALLOWLIST, CAP_PORTS, CAP_STRONG_ISOLATION, DOMAIN_FILTERED_POLICIES, POLICY_FULL,
)
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.settings import load_settings, save_settings

BUILTIN_ENVIRONMENT_NAME = "Default"
BUILTIN_ENVIRONMENT_DESCRIPTION = (
    "The built-in environment: Python with common data packages. No internet access; "
    "switch its network policy to \"package registries only\" to let runs install more packages."
)


def builtin_image() -> str:
    """The BoW sandbox image published with each release (see deploy/sandbox/)."""
    import os

    override = os.getenv("BOW_SANDBOX_IMAGE", "").strip()
    if override:
        return override
    from app.settings.config import settings

    version = str(getattr(settings, "PROJECT_VERSION", "") or "latest").lstrip("v")
    return f"bagofwords/sandbox:{version}"


def pinned_image(env: SandboxEnvironment) -> str:
    """The image reference a sandbox is created from: by digest when one is known."""
    image = env.image
    if env.image_digest and "@" not in image:
        repository = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
        return f"{repository}@{env.image_digest}"
    return image


def required_capabilities(env: SandboxEnvironment) -> set:
    """What a backend must be able to do to run this environment."""
    needed = set()
    if env.require_strong_isolation:
        needed.add(CAP_STRONG_ISOLATION)
    if env.network_policy in DOMAIN_FILTERED_POLICIES:
        needed.add(CAP_DOMAIN_ALLOWLIST)
    if env.ports:
        needed.add(CAP_PORTS)
    return needed


def backend_fits(backend: SandboxBackend, env: SandboxEnvironment) -> bool:
    """Labels match and the backend supports what the environment needs."""
    if not set(env.backend_labels or []) <= set(backend.labels or []):
        return False
    return required_capabilities(env) <= set(backend.capabilities or [])


async def get_environment(db: AsyncSession, organization_id: str, environment_id: str) -> SandboxEnvironment:
    env = (await db.execute(
        select(SandboxEnvironment).where(
            SandboxEnvironment.id == str(environment_id),
            SandboxEnvironment.organization_id == str(organization_id),
            SandboxEnvironment.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if env is None:
        raise SandboxError(errors.ENVIRONMENT_NOT_FOUND, "Sandbox environment not found", status=404)
    return env


async def list_environments(db: AsyncSession, organization_id: str, *, enabled_only: bool = False) -> List[SandboxEnvironment]:
    query = select(SandboxEnvironment).where(
        SandboxEnvironment.organization_id == str(organization_id), SandboxEnvironment.deleted_at.is_(None),
    )
    if enabled_only:
        query = query.where(SandboxEnvironment.enabled.is_(True))
    rows = await db.execute(query.order_by(SandboxEnvironment.name))
    return list(rows.scalars().all())


async def _name_taken(db: AsyncSession, organization_id: str, name: str, except_id: Optional[str] = None) -> bool:
    query = select(SandboxEnvironment.id).where(
        SandboxEnvironment.organization_id == str(organization_id), SandboxEnvironment.deleted_at.is_(None),
        func.lower(SandboxEnvironment.name) == name.lower(),
    )
    if except_id:
        query = query.where(SandboxEnvironment.id != except_id)
    return (await db.execute(query)).first() is not None


async def validation_warnings(db: AsyncSession, organization_id: str, env: SandboxEnvironment) -> List[str]:
    """Things an admin should know about this environment as saved. Not errors:
    an environment may be defined before the backend that will run it exists."""
    warnings: List[str] = []
    if env.secrets and env.network_policy == POLICY_FULL:
        warnings.append(
            "This environment has secrets and full internet access: code run in it can read the secrets and "
            "send them anywhere. Restrict the network policy, or who may use the environment."
        )
    rows = await db.execute(
        select(SandboxBackend).where(
            SandboxBackend.organization_id == str(organization_id), SandboxBackend.deleted_at.is_(None),
        )
    )
    backends = list(rows.scalars().all())
    labelled = [b for b in backends if set(env.backend_labels or []) <= set(b.labels or [])]
    if backends and not labelled:
        warnings.append("No backend has the labels this environment asks for, so it cannot run anywhere yet.")
    elif labelled and not any(backend_fits(b, env) for b in labelled):
        missing = sorted(required_capabilities(env) - set().union(*(set(b.capabilities or []) for b in labelled)))
        warnings.append(
            "No matching backend supports what this environment needs: " + ", ".join(missing).replace("_", " ") + "."
        )
    return warnings


async def _clear_other_defaults(db: AsyncSession, organization_id: str, keep_id: str) -> None:
    rows = await db.execute(
        select(SandboxEnvironment).where(
            SandboxEnvironment.organization_id == str(organization_id), SandboxEnvironment.deleted_at.is_(None),
            SandboxEnvironment.is_default.is_(True), SandboxEnvironment.id != keep_id,
        )
    )
    for other in rows.scalars().all():
        other.is_default = False


async def create_environment(db: AsyncSession, organization_id: str, actor_id: Optional[str],
                             data: SandboxEnvironmentCreate) -> SandboxEnvironment:
    if await _name_taken(db, organization_id, data.name):
        raise SandboxError(errors.NAME_TAKEN, f"A sandbox environment named '{data.name}' already exists", status=409)
    env = SandboxEnvironment(
        organization_id=str(organization_id), name=data.name.strip(), description=data.description,
        image=data.image.strip(), registry_username=data.registry_username,
        registry_password=data.registry_password or None, setup_script=data.setup_script,
        cpus=data.cpus, memory_mb=data.memory_mb, disk_mb=data.disk_mb,
        run_timeout_seconds=data.run_timeout_seconds, network_policy=data.network_policy,
        allowed_domains=list(data.allowed_domains or []), env_vars=dict(data.env_vars or {}),
        secrets=dict(data.secrets) if data.secrets else None,
        backend_labels=list(data.backend_labels or []), require_strong_isolation=data.require_strong_isolation,
        ports=list(data.ports or []), is_default=data.is_default, enabled=data.enabled, created_by=actor_id,
    )
    db.add(env)
    await db.flush()
    if env.is_default:
        await _clear_other_defaults(db, organization_id, env.id)
    await audit(db, organization_id, "sandbox_environment.created", actor_id, "sandbox_environment", env.id, {
        "title": env.name, "image": env.image, "network_policy": env.network_policy,
        "protected_variable_names": sorted((env.secrets or {}).keys()), "is_default": bool(env.is_default),
    })
    await db.commit()
    return env


async def update_environment(db: AsyncSession, organization_id: str, actor_id: Optional[str],
                             environment_id: str, data: SandboxEnvironmentUpdate) -> SandboxEnvironment:
    env = await get_environment(db, organization_id, environment_id)
    changes = data.model_dump(exclude_unset=True)
    changed: List[str] = []
    image_changed = False
    secrets_changed: List[str] = []

    if changes.get("name") and changes["name"].strip() != env.name:
        if await _name_taken(db, organization_id, changes["name"], except_id=env.id):
            raise SandboxError(errors.NAME_TAKEN, f"A sandbox environment named '{changes['name']}' already exists", status=409)
        env.name = changes["name"].strip()
        changed.append("name")
    if changes.get("image") and changes["image"].strip() != env.image:
        env.image = changes["image"].strip()
        env.image_digest = None  # resolved again when the image is pulled
        image_changed = True
        changed.append("image")

    for field in ("description", "registry_username", "setup_script"):
        if field in changes and changes[field] != getattr(env, field):
            setattr(env, field, changes[field])
            changed.append(field)
    for field in ("cpus", "memory_mb", "disk_mb", "run_timeout_seconds", "network_policy",
                  "require_strong_isolation", "enabled"):
        if changes.get(field) is not None and changes[field] != getattr(env, field):
            setattr(env, field, changes[field])
            changed.append(field)
    for field in ("allowed_domains", "env_vars", "backend_labels", "ports"):
        if changes.get(field) is not None and changes[field] != getattr(env, field):
            setattr(env, field, changes[field])
            flag_modified(env, field)
            changed.append(field)

    if "registry_password" in changes and changes["registry_password"] is not None:
        env.registry_password = changes["registry_password"] or None
        changed.append("registry_password")
    if changes.get("secrets") is not None:
        current: Dict[str, str] = dict(env.secrets or {})
        for name, value in changes["secrets"].items():
            if value is None:
                if name in current:
                    del current[name]
                    secrets_changed.append(name)
            elif current.get(name) != value:
                current[name] = value
                secrets_changed.append(name)
        if secrets_changed:
            env.secrets = current or None
            changed.append("secrets")

    if changes.get("is_default") is not None and bool(changes["is_default"]) != bool(env.is_default):
        env.is_default = bool(changes["is_default"])
        changed.append("is_default")
        if env.is_default:
            await _clear_other_defaults(db, organization_id, env.id)

    if changed:
        await audit(db, organization_id, "sandbox_environment.updated", actor_id, "sandbox_environment", env.id,
                    {"title": env.name, "changed": changed})
    if image_changed:
        await audit(db, organization_id, "sandbox_environment.image_changed", actor_id, "sandbox_environment",
                    env.id, {"title": env.name, "image": env.image})
    if secrets_changed:
        # Names only; never a value. (The key avoids the word "secret": the audit
        # service redacts any detail whose key looks like it holds one.)
        await audit(db, organization_id, "sandbox_environment.secrets_changed", actor_id, "sandbox_environment",
                    env.id, {"title": env.name, "protected_variable_names": sorted(secrets_changed)})
    await db.commit()
    return env


async def delete_environment(db: AsyncSession, organization_id: str, actor_id: Optional[str], environment_id: str) -> None:
    env = await get_environment(db, organization_id, environment_id)
    env.deleted_at = datetime.utcnow()
    env.is_default = False
    env.secrets = None
    env.registry_password = None
    rows = await db.execute(
        select(SandboxAgentDefault).where(
            SandboxAgentDefault.environment_id == env.id, SandboxAgentDefault.deleted_at.is_(None),
        )
    )
    for default in rows.scalars().all():
        default.deleted_at = datetime.utcnow()
    settings = await load_settings(db, organization_id)
    if settings.default_environment_id == env.id:
        settings.default_environment_id = None
        await save_settings(db, organization_id, settings)
    await audit(db, organization_id, "sandbox_environment.deleted", actor_id, "sandbox_environment", env.id,
                {"title": env.name})
    await db.commit()


async def ensure_builtin_environment(db: AsyncSession, organization_id: str) -> Optional[SandboxEnvironment]:
    """Seed the built-in "Default" environment once per organization.

    It starts with network policy "none", like every environment. An admin who
    deletes it does not get it back: the seed marker is kept in the settings.
    """
    settings = await load_settings(db, organization_id)
    if settings.builtin_environment_seeded:
        return None
    env: Optional[SandboxEnvironment] = None
    if not await _name_taken(db, organization_id, BUILTIN_ENVIRONMENT_NAME):
        has_default = (await db.execute(
            select(SandboxEnvironment.id).where(
                SandboxEnvironment.organization_id == str(organization_id),
                SandboxEnvironment.deleted_at.is_(None), SandboxEnvironment.is_default.is_(True),
            )
        )).first() is not None
        env = SandboxEnvironment(
            organization_id=str(organization_id), name=BUILTIN_ENVIRONMENT_NAME,
            description=BUILTIN_ENVIRONMENT_DESCRIPTION, image=builtin_image(), network_policy="none",
            allowed_domains=[], env_vars={}, backend_labels=[], ports=[], is_default=not has_default, enabled=True,
        )
        db.add(env)
        await db.flush()
        await audit(db, organization_id, "sandbox_environment.created", None, "sandbox_environment", env.id,
                    {"title": env.name, "image": env.image, "network_policy": "none", "builtin": True})
    settings.builtin_environment_seeded = True
    await save_settings(db, organization_id, settings)
    await db.commit()
    return env


async def agent_default_environment_id(db: AsyncSession, data_source_ids: Iterable[str]) -> Optional[str]:
    """The default environment of the first of these agents that has one."""
    ids = [str(i) for i in data_source_ids if i]
    if not ids:
        return None
    rows = await db.execute(
        select(SandboxAgentDefault).where(
            SandboxAgentDefault.data_source_id.in_(ids), SandboxAgentDefault.deleted_at.is_(None),
        )
    )
    by_agent = {row.data_source_id: row.environment_id for row in rows.scalars().all()}
    return next((by_agent[i] for i in ids if i in by_agent), None)


async def set_agent_default(db: AsyncSession, organization_id: str, actor_id: Optional[str],
                            data_source_id: str, environment_id: Optional[str]) -> None:
    """Set or clear an agent's default environment."""
    existing = (await db.execute(
        select(SandboxAgentDefault).where(
            SandboxAgentDefault.data_source_id == str(data_source_id), SandboxAgentDefault.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    title = None
    if environment_id:
        env = await get_environment(db, organization_id, environment_id)
        title = env.name
        if existing is not None:
            existing.environment_id = env.id
        else:
            db.add(SandboxAgentDefault(organization_id=str(organization_id), data_source_id=str(data_source_id),
                                       environment_id=env.id))
    elif existing is not None:
        existing.deleted_at = datetime.utcnow()
    await audit(db, organization_id, "sandbox_environment.agent_default_changed", actor_id, "data_source",
                str(data_source_id), {"title": title, "environment_id": environment_id})
    await db.commit()


def choose_environment(candidates: Sequence[SandboxEnvironment], *, requested: Optional[str],
                       agent_default_id: Optional[str], org_default_id: Optional[str]) -> SandboxEnvironment:
    """Pick the environment for a new sandbox from the ones the user may use.

    Order: the one asked for (by id or name), the agent's default, the
    organization's default, an environment flagged default, and finally the
    only one there is.
    """
    usable = [e for e in candidates if e.enabled]
    if not usable:
        raise SandboxError(errors.NO_ENVIRONMENT, "There is no sandbox environment you may use.", status=403)
    if requested:
        wanted = requested.strip().lower()
        for env in usable:
            if env.id == requested or env.name.lower() == wanted:
                return env
        names = ", ".join(sorted(e.name for e in usable))
        raise SandboxError(
            errors.ENVIRONMENT_NOT_ALLOWED,
            f"There is no environment '{requested}' you may use. Available: {names}.", status=403,
        )
    by_id = {e.id: e for e in usable}
    for candidate_id in (agent_default_id, org_default_id):
        if candidate_id and candidate_id in by_id:
            return by_id[candidate_id]
    flagged = [e for e in usable if e.is_default]
    if flagged:
        return flagged[0]
    if len(usable) == 1:
        return usable[0]
    names = ", ".join(sorted(e.name for e in usable))
    raise SandboxError(
        errors.NO_ENVIRONMENT,
        f"No default sandbox environment is set. Choose one of: {names}.", status=409,
    )


async def pull_image_on_backends(organization_id: str, environment_id: str, session_factory=None) -> Dict[str, str]:
    """Pull an environment's image on every backend that could run it, and
    record the digest it resolved to. Runs in the background after an
    environment is saved with a new image; returns ``{backend name: outcome}``."""
    import logging

    from app.services.sandbox import backends as backend_service
    from app.services.sandbox.drivers.base import SandboxDriverError

    logger = logging.getLogger(__name__)
    if session_factory is None:
        from app.dependencies import async_session_maker as session_factory

    outcomes: Dict[str, str] = {}
    async with session_factory() as db:
        try:
            env = await get_environment(db, organization_id, environment_id)
        except SandboxError:
            return outcomes
        digest: Optional[str] = None
        for backend in await backend_service.list_backends(db, organization_id):
            if backend.bootstrap_status != "ready" or not set(env.backend_labels or []) <= set(backend.labels or []):
                continue
            try:
                driver = await backend_service.build_driver(db, backend)
            except SandboxError as exc:
                outcomes[backend.name] = exc.message
                continue
            try:
                found = await driver.pull_image(
                    env.image, registry_username=env.registry_username, registry_password=env.registry_password,
                )
                digest = digest or found
                outcomes[backend.name] = "pulled"
            except SandboxDriverError as exc:
                outcomes[backend.name] = exc.message
                logger.warning("could not pull %s on sandbox backend %s: %s", env.image, backend.name, exc.message)
            finally:
                await driver.close()
        if digest and env.image_digest != digest:
            env.image_digest = digest
        await audit(db, organization_id, "sandbox_environment.image_pulled", None, "sandbox_environment", env.id,
                    {"title": env.name, "image": env.image, "digest": digest, "backends": outcomes})
        await db.commit()
    return outcomes
