"""Who may use agent sandboxes.

Three permissions, fitting the existing model in ``core/permissions_registry``:

``use_agent_sandbox``  organization permission. The sandbox tools are offered
                       to this user's agent. Not granted to members by default.
``manage_sandboxes``   organization permission. Computes, environments,
                       settings and the fleet view. Checked on the routes.
``use``                per-environment grant (resource type
                       ``sandbox_environment``), to a user, group or role.

A sandbox belongs to its report and is shared by everyone who can work in
that report. Every action is attributed to, and checked against, the user who
triggered it: that user needs ``use_agent_sandbox`` and ``use`` on the
sandbox's environment. Access to the report itself is established by the
caller before it gets here (the completion route for the planner tools, the
owner check for the MCP tools); scheduled runs act as the report owner.

Full admins hold every permission, including ``use`` on every environment.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sandbox import SandboxCompute, SandboxEnvironment
from app.services.sandbox import errors
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.feature import SANDBOX_ENVIRONMENT_RESOURCE

USE_AGENT_SANDBOX = "use_agent_sandbox"
MANAGE_SANDBOXES = "manage_sandboxes"
USE = "use"


class SandboxAccess:
    async def _resolved(self, db: AsyncSession, organization_id: str, user_id: Optional[str]):
        if not user_id:
            return None
        from app.core.permission_resolver import resolve_permissions

        return await resolve_permissions(db, str(user_id), str(organization_id))

    async def can_use_sandboxes(self, db: AsyncSession, organization_id: str, user_id: Optional[str]) -> bool:
        resolved = await self._resolved(db, organization_id, user_id)
        return bool(resolved and resolved.has_org_permission(USE_AGENT_SANDBOX))

    async def require_use(self, db: AsyncSession, organization_id: str, user_id: Optional[str], report=None) -> None:
        """The acting user holds ``use_agent_sandbox`` in the report's organization."""
        if report is not None and str(report.organization_id) != str(organization_id):
            raise SandboxError(errors.FORBIDDEN, "This report belongs to another organization.", status=403)
        if not await self.can_use_sandboxes(db, organization_id, user_id):
            raise SandboxError(
                errors.FORBIDDEN, "You do not have permission to use agent sandboxes.", status=403,
            )

    async def can_use_environment(self, db: AsyncSession, organization_id: str, user_id: Optional[str],
                                  environment_id: Optional[str]) -> bool:
        if not environment_id:
            return False
        resolved = await self._resolved(db, organization_id, user_id)
        return bool(resolved and resolved.has_resource_permission(SANDBOX_ENVIRONMENT_RESOURCE, str(environment_id), USE))

    async def require_environment(self, db: AsyncSession, organization_id: str, user_id: Optional[str],
                                  environment_id: Optional[str]) -> None:
        if not await self.can_use_environment(db, organization_id, user_id, environment_id):
            raise SandboxError(
                errors.ENVIRONMENT_NOT_ALLOWED,
                "You do not have permission to use this report's sandbox environment.", status=403,
            )

    async def usable_environments(self, db: AsyncSession, organization_id: str,
                                  user_id: Optional[str]) -> List[SandboxEnvironment]:
        """The enabled environments this user holds ``use`` on."""
        resolved = await self._resolved(db, organization_id, user_id)
        if resolved is None:
            return []
        rows = await db.execute(
            select(SandboxEnvironment).where(
                SandboxEnvironment.organization_id == str(organization_id),
                SandboxEnvironment.deleted_at.is_(None), SandboxEnvironment.enabled.is_(True),
            ).order_by(SandboxEnvironment.name)
        )
        return [
            env for env in rows.scalars().all()
            if resolved.has_resource_permission(SANDBOX_ENVIRONMENT_RESOURCE, env.id, USE)
        ]

    async def tools_offered(self, db: AsyncSession, organization_id: str, user_id: Optional[str]) -> bool:
        """Whether the sandbox tools should be offered to this user's agent:
        the permission, an environment they may use, and a compute to run it on.
        (The deployment flag and the organization switch are checked by the caller.)"""
        if not await self.can_use_sandboxes(db, organization_id, user_id):
            return False
        environments = await self.usable_environments(db, organization_id, user_id)
        if not environments:
            return False
        from app.services.sandbox.environments import compute_fits

        rows = await db.execute(
            select(SandboxCompute).where(
                SandboxCompute.organization_id == str(organization_id), SandboxCompute.deleted_at.is_(None),
                SandboxCompute.enabled.is_(True), SandboxCompute.bootstrap_status == "ready",
            )
        )
        computes = list(rows.scalars().all())
        return any(compute_fits(b, env) for b in computes for env in environments)


# ── The per-environment ``use`` grant ────────────────────────────────────


async def list_environment_grants(db: AsyncSession, organization_id: str, environment_id: str) -> List[dict]:
    from app.models.resource_grant import ResourceGrant

    rows = await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.organization_id == str(organization_id),
            ResourceGrant.resource_type == SANDBOX_ENVIRONMENT_RESOURCE,
            ResourceGrant.resource_id == str(environment_id), ResourceGrant.deleted_at.is_(None),
        )
    )
    return [
        {"principal_type": g.principal_type, "principal_id": g.principal_id}
        for g in rows.scalars().all() if USE in (g.permissions or [])
    ]


async def set_environment_grants(db: AsyncSession, organization_id: str, actor_id: Optional[str],
                                 environment: SandboxEnvironment, grants: Sequence[dict]) -> List[dict]:
    """Replace who may use an environment. ``grants``: [{principal_type, principal_id}]."""
    from app.core.permission_resolver import invalidate_rbac_memo
    from app.models.resource_grant import ResourceGrant
    from app.services.sandbox.audit import audit

    wanted = {(g["principal_type"], str(g["principal_id"])) for g in grants}
    for principal_type, _ in wanted:
        if principal_type not in ("user", "group", "role"):
            raise SandboxError(errors.INVALID, f"Unknown principal type '{principal_type}'")
    await _require_principals_in_org(db, organization_id, wanted)

    rows = await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.resource_type == SANDBOX_ENVIRONMENT_RESOURCE,
            ResourceGrant.resource_id == str(environment.id),
        )
    )
    existing = {(g.principal_type, g.principal_id): g for g in rows.scalars().all()}
    added, removed = [], []
    for key, grant in existing.items():
        if key not in wanted:
            await db.delete(grant)  # the unique constraint covers deleted rows, so remove it
            removed.append(key)
    for key in wanted:
        if key in existing:
            if existing[key].deleted_at is not None or USE not in (existing[key].permissions or []):
                existing[key].deleted_at = None
                existing[key].permissions = [USE]
                added.append(key)
        else:
            db.add(ResourceGrant(
                organization_id=str(organization_id), resource_type=SANDBOX_ENVIRONMENT_RESOURCE,
                resource_id=str(environment.id), principal_type=key[0], principal_id=key[1], permissions=[USE],
            ))
            added.append(key)
    if added or removed:
        await audit(db, organization_id, "sandbox_environment.access_changed", actor_id, "sandbox_environment",
                    environment.id, {
                        "title": environment.name,
                        "granted": [{"principal_type": t, "principal_id": i} for t, i in sorted(added)],
                        "revoked": [{"principal_type": t, "principal_id": i} for t, i in sorted(removed)],
                    })
    await db.commit()
    try:
        memo_users = [i for t, i in wanted | set(removed) if t == "user"]
        for user_id in memo_users:
            invalidate_rbac_memo(db, user_id, str(organization_id))
    except Exception:  # noqa: BLE001 - the memo is per session; a stale entry dies with it
        pass
    return await list_environment_grants(db, organization_id, environment.id)


async def _require_principals_in_org(db: AsyncSession, organization_id: str, principals: set) -> None:
    """Every principal named in a grant exists in this organization."""
    from app.models.group import Group
    from app.models.membership import Membership
    from app.models.role import Role

    for principal_type, principal_id in principals:
        if principal_type == "user":
            found = (await db.execute(
                select(Membership.id).where(
                    Membership.user_id == principal_id, Membership.organization_id == str(organization_id),
                )
            )).first()
        elif principal_type == "group":
            found = (await db.execute(
                select(Group.id).where(Group.id == principal_id, Group.organization_id == str(organization_id))
            )).first()
        else:
            found = (await db.execute(select(Role).where(Role.id == principal_id))).scalar_one_or_none()
            if found is not None and getattr(found, "organization_id", None) not in (None, str(organization_id)):
                found = None
        if not found:
            raise SandboxError(errors.INVALID, f"Unknown {principal_type} '{principal_id}' in this organization", status=404)
