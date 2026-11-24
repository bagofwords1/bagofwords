"""The PII policy of one agent, read and set from the agent's own settings.

Agent admins (``manage`` on the agent) choose which of the org's PII policies
the agent uses, or let it inherit the org's default agent policy. The choice is
stored with the rest of the PII config
(``OrganizationSettings.config["pii_protection"]["agent_policies"]``), so the
org PII page and this endpoint edit the same data. See
``docs/design/pii-agent-policies.md``.

Turning an agent's policy off entirely (``none``) can only loosen what the org
default imposes, so it needs ``manage_settings`` on the org, not only
``manage`` on the agent.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.ai.llm.pii import policy as pii_policy
from app.ee.license import has_feature
from app.models.data_source import DataSource
from app.models.organization import Organization
from app.models.user import User

INHERIT = "inherit"


async def _agent(db: AsyncSession, organization: Organization, data_source_id: str) -> DataSource:
    agent = (await db.execute(select(DataSource).where(
        DataSource.id == data_source_id,
        DataSource.organization_id == organization.id,
        DataSource.deleted_at.is_(None),
    ))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "Data source not found")
    return agent


async def _can_manage_settings(db: AsyncSession, organization: Organization, user: User) -> bool:
    from app.core.permission_resolver import resolve_permissions
    rights = await resolve_permissions(db, str(user.id), str(organization.id))
    return rights.has_org_permission("manage_settings")


def _view(cfg: Dict[str, Any], agent_id: str, *, can_turn_off: bool) -> Dict[str, Any]:
    assigned = cfg["agent_policies"].get(agent_id)
    default_agent = cfg["default_agent_policy"]
    effective = assigned if assigned is not None else default_agent
    policies = cfg["policies"]
    return {
        "licensed": True,
        "enabled": cfg["enabled"],
        # "inherit" | "none" | <policy id>
        "policy": assigned if assigned is not None else INHERIT,
        "default_agent_policy": default_agent,
        "default_agent_policy_name": policies[default_agent]["name"] if default_agent in policies else None,
        "effective_policy": effective,
        "effective_policy_name": policies[effective]["name"] if effective in policies else None,
        "policies": [{"id": pid, "name": p["name"]} for pid, p in policies.items()],
        "can_turn_off": can_turn_off,
    }


async def get_agent_pii_policy(db: AsyncSession, organization: Organization, user: User, data_source_id: str) -> dict:
    await _agent(db, organization, data_source_id)
    if not has_feature("pii_protection"):
        return {"licensed": False}
    from app.services.organization_settings_service import OrganizationSettingsService
    settings = await OrganizationSettingsService().get_settings(db, organization, user)
    cfg = pii_policy.normalize((settings.config or {}).get("pii_protection"))
    return _view(cfg, str(data_source_id), can_turn_off=await _can_manage_settings(db, organization, user))


async def set_agent_pii_policy(
    db: AsyncSession, organization: Organization, user: User, data_source_id: str, policy: Optional[str],
) -> dict:
    """``policy``: a policy id, ``inherit`` (or None) for the org default, or
    ``none`` (org admins only)."""
    agent = await _agent(db, organization, data_source_id)
    if not has_feature("pii_protection"):
        raise HTTPException(402, "PII protection requires an enterprise license.")
    agent_id = str(agent.id)
    can_turn_off = await _can_manage_settings(db, organization, user)
    policy = policy or INHERIT
    if not isinstance(policy, str):
        raise HTTPException(400, "Invalid PII policy.")
    if policy == pii_policy.NO_AGENT_POLICY and not can_turn_off:
        raise HTTPException(403, "Only organization admins can turn PII protection off for an agent.")

    from app.ee.audit.service import audit_service
    from app.services.organization_settings_service import OrganizationSettingsService

    settings = await OrganizationSettingsService().get_settings(db, organization, user)
    config = copy.deepcopy(settings.config or {})
    old = pii_policy.normalize(config.get("pii_protection"))
    new = copy.deepcopy(old)
    if policy == INHERIT:
        new["agent_policies"].pop(agent_id, None)
    else:
        try:
            pii_policy.validate_update({"agent_policies": {agent_id: policy}}, old)
        except pii_policy.PolicyValidationError as e:
            raise HTTPException(400, str(e))
        new["agent_policies"][agent_id] = policy
    new = pii_policy.normalize(new)

    if new != old:
        from app.schemas.organization_settings_schema import PiiProtectionConfig
        config["pii_protection"] = PiiProtectionConfig(**new).model_dump()
        settings.config = config
        flag_modified(settings, "config")
        await db.commit()
        from app.ai.llm.pii.loader import invalidate
        invalidate(str(organization.id))
        for entry in pii_policy.diff_for_audit(old, new):
            try:
                await audit_service.log(
                    db=db,
                    organization_id=str(organization.id),
                    action=entry["action"],
                    user_id=str(user.id),
                    resource_type="data_source",
                    resource_id=agent_id,
                    details={**entry["details"], "source": "agent_settings"},
                )
            except Exception:
                pass
    return _view(new, agent_id, can_turn_off=can_turn_off)
