"""Sandbox environments: the admin-defined templates sandboxes are created from.

An environment says what a sandbox is (image, resources, run timeout), what
it may reach (network policy), what it carries (variables and secrets) and
where it may run (compute labels, strong isolation). Who may use it is the
per-environment ``use`` grant, handled in ``access.py``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.sandbox import Sandbox, SandboxAgentDefault, SandboxCompute, SandboxEnvironment
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


# After this many creations in a row that failed in a hook, an environment
# stops creating sandboxes until it is changed: every attempt would fail the
# same way, each one costing a container and up to the script's time limit.
HOOK_FAILURE_CAP = 3


def setup_fingerprint(env: SandboxEnvironment) -> str:
    """Identifies what a sandbox was set up with (image and setup script), so a
    sandbox created before either changed can be told apart."""
    import hashlib

    return hashlib.sha256(f"{env.image}\n{env.setup_script or ''}".encode("utf-8")).hexdigest()[:16]


def pinned_image(env: SandboxEnvironment) -> str:
    """The image reference a sandbox is created from: by digest when one is known."""
    image = env.image
    if env.image_digest and "@" not in image:
        repository = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
        return f"{repository}@{env.image_digest}"
    return image


def required_capabilities(env: SandboxEnvironment) -> set:
    """What a compute must be able to do to run this environment."""
    needed = set()
    if env.require_strong_isolation:
        needed.add(CAP_STRONG_ISOLATION)
    if env.network_policy in DOMAIN_FILTERED_POLICIES:
        needed.add(CAP_DOMAIN_ALLOWLIST)
    if env.ports:
        needed.add(CAP_PORTS)
    return needed


def mounted_volumes(env: SandboxEnvironment) -> set:
    return {m.get("volume") for m in (env.mounts or []) if m.get("volume")}


def compute_fits(compute: SandboxCompute, env: SandboxEnvironment) -> bool:
    """Labels match, the compute has every volume the environment mounts, and
    it supports what the environment needs."""
    if not set(env.compute_labels or []) <= set(compute.labels or []):
        return False
    if not mounted_volumes(env) <= {v.get("name") for v in (compute.volumes or [])}:
        return False
    return required_capabilities(env) <= set(compute.capabilities or [])


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
    an environment may be defined before the compute that will run it exists."""
    warnings: List[str] = []
    if env.secrets and env.network_policy == POLICY_FULL:
        warnings.append(
            "This environment has secrets and full internet access: code run in it can read the secrets and "
            "send them anywhere. Restrict the network policy, or who may use the environment."
        )
    if int(env.hook_failures or 0) >= HOOK_FAILURE_CAP:
        warnings.append(
            f"Its setup or start script failed {env.hook_failures} times in a row, so new sandboxes are refused. "
            f"Last error: {(env.hook_last_error or 'unknown')[:300]} Change the script or the image to try again."
        )
    elif env.hook_failures:
        warnings.append(
            f"Its setup or start script failed the last {env.hook_failures} time(s): "
            f"{(env.hook_last_error or 'unknown')[:300]}"
        )
    rows = await db.execute(
        select(SandboxCompute).where(
            SandboxCompute.organization_id == str(organization_id), SandboxCompute.deleted_at.is_(None),
        )
    )
    computes = list(rows.scalars().all())
    labelled = [b for b in computes if set(env.compute_labels or []) <= set(b.labels or [])]
    wanted = mounted_volumes(env)
    if wanted and labelled and not any(wanted <= {v.get("name") for v in (b.volumes or [])} for b in labelled):
        warnings.append(
            "No compute has every volume this environment mounts (" + ", ".join(sorted(wanted)) + "), "
            "so it cannot run anywhere yet. Add the volume to a compute."
        )
    if any(not m.get("read_only", True) and m.get("shared") for m in (env.mounts or [])):
        warnings.append(
            "A volume is mounted writable and shared: every sandbox of this environment can read and change "
            "what the others wrote there, across reports."
        )
    if computes and not labelled:
        warnings.append("No compute has the labels this environment asks for, so it cannot run anywhere yet.")
    elif labelled and not any(compute_fits(b, env) for b in labelled):
        missing = sorted(required_capabilities(env) - set().union(*(set(b.capabilities or []) for b in labelled)))
        warnings.append(
            "No matching compute supports what this environment needs: " + ", ".join(missing).replace("_", " ") + "."
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
        start_script=data.start_script, stop_script=data.stop_script, hook_failures=0,
        cpus=data.cpus, memory_mb=data.memory_mb, disk_mb=data.disk_mb,
        run_timeout_seconds=data.run_timeout_seconds, network_policy=data.network_policy,
        allowed_domains=list(data.allowed_domains or []), env_vars=dict(data.env_vars or {}),
        secrets=dict(data.secrets) if data.secrets else None,
        compute_labels=list(data.compute_labels or []), mounts=[m.model_dump() for m in (data.mounts or [])], require_strong_isolation=data.require_strong_isolation,
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

    for field in ("description", "registry_username", "setup_script", "start_script", "stop_script"):
        if field in changes and changes[field] != getattr(env, field):
            setattr(env, field, changes[field])
            changed.append(field)
    if image_changed or {"setup_script", "start_script"} & set(changed):
        # What kept failing was changed: let sandboxes be created again.
        env.hook_failures, env.hook_last_error = 0, None
    for field in ("cpus", "memory_mb", "disk_mb", "run_timeout_seconds", "network_policy",
                  "require_strong_isolation", "enabled"):
        if changes.get(field) is not None and changes[field] != getattr(env, field):
            setattr(env, field, changes[field])
            changed.append(field)
    if data.mounts is not None:
        mounts = [m.model_dump() for m in data.mounts]
        if mounts != list(env.mounts or []):
            env.mounts = mounts
            flag_modified(env, "mounts")
            changed.append("mounts")
    for field in ("allowed_domains", "env_vars", "compute_labels", "ports"):
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


async def live_sandboxes(db: AsyncSession, environment_id: str) -> List[Sandbox]:
    """Sandboxes created from this environment that still exist. They keep the
    network policy and limits they were created with."""
    rows = await db.execute(
        select(Sandbox).where(Sandbox.environment_id == str(environment_id), Sandbox.deleted_at.is_(None),
                              Sandbox.state != "deleted")
    )
    return list(rows.scalars().all())


async def delete_environment(db: AsyncSession, organization_id: str, actor_id: Optional[str], environment_id: str) -> None:
    """Delete an environment and everything that hangs off it: who may use it
    and the agent defaults pointing at it. Refused while sandboxes created from
    it still exist, because they keep running with its network policy; the
    caller deletes those first (the route does, when asked to)."""
    from app.services.sandbox import access as access_module

    env = await get_environment(db, organization_id, environment_id)
    remaining = len(await live_sandboxes(db, env.id))
    if remaining:
        raise SandboxError(
            errors.ENVIRONMENT_IN_USE,
            f"{remaining} sandbox(es) created from this environment still exist and keep its network "
            "policy. Delete them too, or wait until they expire.", status=409,
        )
    await access_module.set_environment_grants(db, organization_id, actor_id, env, [])
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
            allowed_domains=[], env_vars={}, compute_labels=[], ports=[], is_default=not has_default, enabled=True,
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


async def pull_image_on_computes(organization_id: str, environment_id: str, session_factory=None) -> Dict[str, str]:
    """Pull an environment's image on every compute that could run it, and
    record the digest it resolved to. Runs in the background after an
    environment is saved with a new image; returns ``{compute name: outcome}``."""
    import logging

    from app.services.sandbox import computes as compute_service
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
        for compute in await compute_service.list_computes(db, organization_id):
            if compute.bootstrap_status != "ready" or not set(env.compute_labels or []) <= set(compute.labels or []):
                continue
            try:
                driver = await compute_service.build_driver(db, compute)
            except SandboxError as exc:
                outcomes[compute.name] = exc.message
                continue
            try:
                found = await driver.pull_image(
                    env.image, registry_username=env.registry_username, registry_password=env.registry_password,
                )
                digest = digest or found
                outcomes[compute.name] = "pulled"
            except SandboxDriverError as exc:
                outcomes[compute.name] = exc.message
                logger.warning("could not pull %s on sandbox compute %s: %s", env.image, compute.name, exc.message)
            finally:
                await driver.close()
        if digest and env.image_digest != digest:
            env.image_digest = digest
        await audit(db, organization_id, "sandbox_environment.image_pulled", None, "sandbox_environment", env.id,
                    {"title": env.name, "image": env.image, "digest": digest, "computes": outcomes})
        await db.commit()
    return outcomes
