"""The agent-sandbox feature gate.

Three levels, each off by default:

1. the deployment flag ``BOW_AGENT_SANDBOXES_ENABLED`` — whether the feature
   exists at all for this deployment (:func:`sandbox_feature_enabled`);
2. the organization switch "Enable agent sandboxes" on Settings → Sandbox —
   whether sandboxes can be created (:func:`sandboxes_enabled_for_org`);
3. the ``use_agent_sandbox`` permission and the per-environment ``use`` grant
   — whether a given user's agent is offered the tools.

:func:`sandbox_feature_enabled` is the only place the deployment flag is read
on the compute. Every connection point into existing code calls it.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from app.errors import AppError, ErrorCode
from app.settings.config import settings

# Key inside ``OrganizationSettings.config`` that holds the Sandbox settings
# tab. A plain dict (not a ``FeatureConfig``) so the generic settings pages do
# not list it, and a protected key so only the sandbox routes — gated by
# ``manage_sandboxes`` — can write it.
ORG_SETTINGS_KEY = "agent_sandboxes"

# Org permissions that only make sense while the feature exists. They are
# registered either way (permissions_registry.AGENT_SANDBOX_PERMISSION_CATEGORIES),
# so stored grants remain valid across a flag flip, but the role editor only
# offers them while the flag is on.
SANDBOX_ORG_PERMISSIONS = frozenset({"use_agent_sandbox", "manage_sandboxes"})
# Resource type of the per-environment `use` grant.
SANDBOX_ENVIRONMENT_RESOURCE = "sandbox_environment"
SANDBOX_RESOURCE_TYPES = frozenset({SANDBOX_ENVIRONMENT_RESOURCE})

# Planner tool names owned by this feature (hidden as a block when it is off).
SANDBOX_TOOL_NAMES = frozenset({"create_sandbox", "run_in_sandbox", "sandbox_status", "delete_sandbox"})


def sandbox_feature_enabled() -> bool:
    """Level 1: does the feature exist for this deployment?"""
    cfg = getattr(settings.bow_config, "agent_sandboxes", None)
    return bool(getattr(cfg, "enabled", False))


async def require_sandbox_feature() -> None:
    """Router dependency: every sandbox route answers "feature disabled" when the flag is off."""
    if not sandbox_feature_enabled():
        raise AppError.not_found(
            ErrorCode.AGENT_SANDBOXES_DISABLED,
            "Agent sandboxes are turned off on this server (BOW_AGENT_SANDBOXES_ENABLED)",
        )


def org_sandbox_config(organization_settings: Any) -> dict:
    """The raw Sandbox settings dict stored for an organization (may be empty)."""
    config = getattr(organization_settings, "config", None) or {}
    raw = config.get(ORG_SETTINGS_KEY) if isinstance(config, dict) else None
    return raw if isinstance(raw, dict) else {}


def sandboxes_enabled_for_org(organization_settings: Any) -> bool:
    """Levels 1 and 2: the deployment flag and the organization switch."""
    if not sandbox_feature_enabled():
        return False
    return bool(org_sandbox_config(organization_settings).get("enabled", False))


def extend_role_editor_payload(payload: dict) -> dict:
    """Add the sandbox permissions to the role editor payload while the flag is on.

    ``payload`` is the ``/permissions/registry`` response built from the
    always-on registry. With the flag off it is returned untouched, so the
    editor is exactly what it was before the feature existed.
    """
    if not sandbox_feature_enabled():
        return payload
    from app.core.permissions_registry import (
        AGENT_SANDBOX_PERMISSION_CATEGORIES,
        AGENT_SANDBOX_RESOURCE_PERMISSIONS,
    )

    out = dict(payload)
    categories = {name: list(perms) for name, perms in (payload.get("categories") or {}).items()}
    for name, perms in AGENT_SANDBOX_PERMISSION_CATEGORIES.items():
        bucket = categories.setdefault(name, [])
        bucket.extend(p for p in perms if p not in bucket)
    out["categories"] = categories
    out["resource_permissions"] = {
        **(payload.get("resource_permissions") or {}),
        **AGENT_SANDBOX_RESOURCE_PERMISSIONS,
    }
    return out


def visible_tool_names(names: Iterable[str], *, offered: bool = False) -> list:
    """Drop the sandbox tools from a tool-name list unless they are ``offered``.

    ``offered`` is the caller's verdict that all gates hold for this user
    (flag, org switch, a usable compute and environment, permission). With the
    flag off it is ignored: the tools are never listed.
    """
    if offered and sandbox_feature_enabled():
        return list(names)
    return [n for n in names if n not in SANDBOX_TOOL_NAMES]


def stored_instance_id(organization_settings: Any) -> Optional[str]:
    """The stable id of this BoW deployment, once created (see service.ensure_instance_id)."""
    value = org_sandbox_config(organization_settings).get("instance_id")
    return value if isinstance(value, str) and value else None
