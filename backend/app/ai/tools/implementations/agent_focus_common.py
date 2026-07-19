"""Shared helpers for the agent focus tools (search_agents / set_report_agents).

"Agent" == ``DataSource``. The candidate set and the permission gate differ by
mode:

  - **training**: the actor is configuring agents they OWN, so candidates are the
    agents they can MANAGE (org-level ``manage_instructions`` → all agents, or a
    per-agent ``manage`` grant which implies ``manage_instructions``). This lets
    them pull a managed agent that isn't yet on the report into focus.
  - **chat / deep (and everything else)**: candidates are the agents already
    attached to the report (which the actor can, by construction, query).
"""
from __future__ import annotations

from typing import Any, List, Tuple

from sqlalchemy import select


# The capability that means "manages this agent" — mirrors report_service's
# training-mode gate. A per-agent `manage` grant implies it; org-level holders
# manage every agent.
MANAGE_PERMISSION = "manage_instructions"


async def resolve_candidate_agents(
    db, organization: Any, user: Any, report: Any, mode: str
) -> Tuple[List[Any], str]:
    """Return ``(agents, scope_label)`` — the agents this actor may search/focus
    in the current mode. ``scope_label`` is "managed" (training) or "attached".
    """
    if mode == "training":
        from sqlalchemy.orm import selectinload
        from app.core.permission_resolver import get_ds_ids_with_permission
        from app.models.data_source import DataSource

        is_admin, ds_ids = await get_ds_ids_with_permission(
            db, str(user.id) if user else "", str(organization.id), MANAGE_PERMISSION
        )
        stmt = (
            select(DataSource)
            .options(selectinload(DataSource.connections))
            .where(DataSource.organization_id == str(organization.id))
        )
        if not is_admin:
            if not ds_ids:
                return [], "managed"
            stmt = stmt.where(DataSource.id.in_([str(x) for x in ds_ids]))
        rows = (await db.execute(stmt)).scalars().all()
        return list(rows), "managed"

    # chat / deep / other: the report's attached agents.
    return list(getattr(report, "data_sources", None) or []), "attached"


async def user_can_focus_agent(
    db, organization: Any, user: Any, ds_id: str, mode: str
) -> bool:
    """Whether the actor may focus/attach the given agent id in this mode."""
    if mode == "training":
        from app.core.permission_resolver import resolve_permissions

        resolved = await resolve_permissions(
            db, str(user.id) if user else "", str(organization.id)
        )
        return resolved.has_org_permission(MANAGE_PERMISSION) or resolved.has_resource_permission(
            "data_source", str(ds_id), MANAGE_PERMISSION
        )

    from app.core.permission_resolver import user_can_access_data_source

    return await user_can_access_data_source(
        db, str(user.id) if user else "", str(organization.id), None, ds_id=str(ds_id)
    )
