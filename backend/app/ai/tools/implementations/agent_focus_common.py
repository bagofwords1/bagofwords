"""Shared helpers for the agent focus tools (search_agents / set_report_agents).

"Agent" == ``DataSource``. Selection semantics:

  - ``report.data_sources`` EMPTY = **Auto**: the user delegated scoping, so the
    working set is every agent they can access and the tools act freely.
  - Non-empty = **manual**: the user picked agents. Search may still look across
    accessible agents (to propose one), but expanding the report beyond the
    user's selection requires their approval (see set_report_agents).
  - **training**: the actor curates agents they MANAGE (org-level
    ``manage_instructions`` → all agents, or a per-agent ``manage`` grant).
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from sqlalchemy import select
from sqlalchemy.orm import selectinload


# The capability that means "manages this agent" — mirrors report_service's
# training-mode gate. A per-agent `manage` grant implies it; org-level holders
# manage every agent.
MANAGE_PERMISSION = "manage_instructions"


async def accessible_agents(db, organization: Any, user: Any) -> List[Any]:
    """All live agents this user can use: org-scoped, active, not disabled,
    public OR granted (full admin sees all). Connections eager-loaded."""
    from app.core.permission_resolver import get_accessible_data_source_ids
    from app.models.data_source import DataSource

    stmt = (
        select(DataSource)
        .options(selectinload(DataSource.connections))
        .where(
            DataSource.organization_id == str(organization.id),
            DataSource.is_active == True,  # noqa: E712
            DataSource.publish_status != "disabled",
        )
        .order_by(DataSource.name)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    if user is None:
        return [ds for ds in rows if getattr(ds, "is_public", False)]
    is_admin, member_ids = await get_accessible_data_source_ids(
        db, str(user.id), str(organization.id)
    )
    if is_admin:
        return rows
    allowed = set(str(x) for x in (member_ids or []))
    return [ds for ds in rows if getattr(ds, "is_public", False) or str(ds.id) in allowed]


def report_selection_is_auto(report: Any) -> bool:
    """Empty attached roster = Auto (the user never pinned a selection)."""
    return not list(getattr(report, "data_sources", None) or [])


async def resolve_candidate_agents(
    db, organization: Any, user: Any, report: Any, mode: str
) -> Tuple[List[Any], str, set]:
    """Return ``(agents, scope_label, attached_ids)`` for search/focus.

    ``attached_ids`` is the raw manual selection (empty under Auto) — an agent
    outside it needs user approval to be attached in manual mode.
    """
    if mode == "training":
        from app.core.permission_resolver import get_ds_ids_with_permission
        from app.models.data_source import DataSource

        attached_ids = {str(ds.id) for ds in (getattr(report, "data_sources", None) or [])}
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
                return [], "managed", attached_ids
            stmt = stmt.where(DataSource.id.in_([str(x) for x in ds_ids]))
        rows = (await db.execute(stmt)).scalars().all()
        return list(rows), "managed", attached_ids

    attached = list(getattr(report, "data_sources", None) or [])
    attached_ids = {str(ds.id) for ds in attached}
    if not attached:
        # Auto: the working set is everything accessible; nothing needs attach.
        agents = await accessible_agents(db, organization, user)
        return agents, "accessible", {str(a.id) for a in agents}

    # Manual: the user's selection first, plus other accessible agents the
    # model may PROPOSE (attaching one requires the user's approval).
    extras = [
        ds for ds in await accessible_agents(db, organization, user)
        if str(ds.id) not in attached_ids
    ]
    return attached + extras, "attached+accessible", attached_ids


async def render_agents_full(db, organization: Any, report: Any, user: Any, data_sources: List[Any]) -> str:
    """Full tables/tools schema + always-on instructions for the given agents —
    the same shape an attached agent's eager context has. Used by search_agents
    results AND set_report_agents success observations, so the planner can act
    on the schema immediately even if the rendered context lags a turn."""
    import logging
    logger = logging.getLogger(__name__)
    if not data_sources:
        return ""
    parts: List[str] = []
    ds_ids = [str(ds.id) for ds in data_sources]
    try:
        from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
        builder = SchemaContextBuilder(db, data_sources, organization, report, user=user)
        ctx = await builder.build(with_stats=True, data_source_ids=ds_ids)
        schema_xml = ctx.render_combined(top_k_per_ds=10, index_limit=1000)
        if schema_xml:
            parts.append(schema_xml)
    except Exception:
        logger.exception("render_agents_full: schema render failed")
    try:
        from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder
        ib = InstructionContextBuilder(db, organization, current_user=user, data_source_ids=ds_ids)
        isec = await ib.build(query=None, data_source_ids=ds_ids)
        inst_xml = isec.render(include_catalog=False)
        if inst_xml and inst_xml.strip():
            parts.append(inst_xml)
    except Exception:
        logger.exception("render_agents_full: instruction render failed")
    return "\n".join(parts)


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
