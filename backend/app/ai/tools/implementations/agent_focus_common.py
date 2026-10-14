"""Shared helpers for the agent focus tools (search_agents / set_report_agents).

"Agent" == ``DataSource``. Selection semantics:

  - ``report.data_sources`` EMPTY = **Auto**: the user delegated scoping, so the
    working set is every agent they can access and the tools act freely.
  - Non-empty = **manual**: the user picked agents, and that selection is a HARD
    scope — search and focus operate on the selected agents only; other
    accessible agents are never searched, loaded, or proposed. The user changes
    the scope themselves from the agent selector.
  - **training**: the actor curates agents they MANAGE (org-level
    ``manage_instructions`` → all agents, or a per-agent ``manage`` grant).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from sqlalchemy import select
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)


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


async def signin_required_ids(db, agents: List[Any], user: Any) -> set:
    """Ids of agents the user can SEE but cannot USE yet — user_required auth
    with no personal credentials and no service-account fallback (e.g. PowerBI
    OBO before sign-in). Callers surface these as "sign-in required" instead of
    attaching them and failing mid-query."""
    if not agents:
        return set()
    try:
        from app.services.data_source_service import DataSourceService
        usable, _skipped = await DataSourceService().filter_user_usable_data_sources(
            db, list(agents), user
        )
        usable_ids = {str(d.id) for d in usable}
        return {str(d.id) for d in agents} - usable_ids
    except Exception:
        return set()


def report_selection_is_auto(report: Any) -> bool:
    """Empty attached roster = Auto (the user never pinned a selection)."""
    return not list(getattr(report, "data_sources", None) or [])


async def resolve_run_agents(db, organization: Any, user: Any, report: Any) -> List[Any]:
    """The agents a run actually executes against — the working set.

    Auto is a MODE, not a set: "every agent I can access", resolved here at run
    time against the running user rather than frozen into the report. So an
    unattached report resolves to that user's accessible agents, and one that
    gains access to a new agent picks it up on its next run. A manual selection
    is the report's own snapshot and is used verbatim.

    Only live agents come back: an agent disabled or deactivated after being
    attached must not contribute a client or a schema (see AgentV2.__init__).
    Callers build clients over this list and hand the same list to AgentV2 as
    ``data_sources``, so context, roster and clients all describe one set.
    """
    from app.services.data_source_service import DataSourceService

    if report is None:
        # No report is not Auto — there is no scope to resolve. Returning the
        # whole roster here would hand a caller that never had a report every
        # agent the user can reach.
        return []
    attached = list(getattr(report, "data_sources", None) or [])
    # Artifact-chat threads are never Auto: their roster is the owner-allowed ∩
    # viewer-accessible intersection synced per message, and empty means
    # "dashboard data only" — widening to everything the viewer can access
    # would hand a shared-dashboard chat far more than the owner shared.
    if not attached and getattr(report, "report_type", "regular") == "artifact_chat":
        return []
    agents = attached if attached else await accessible_agents(db, organization, user)
    return [ds for ds in agents if DataSourceService.is_execution_live(ds)]


async def prepare_run_agents(db, organization: Any, user: Any, report: Any,
                             data_source_service: Any = None) -> Tuple[List[Any], Dict[str, Any]]:
    """``(agents, clients)`` for a run — the working set and its query clients.

    An agent the user cannot reach 403s inside ``construct_clients``; it is
    skipped rather than failing the run, and AgentV2 then drops it from the
    context so nothing unqueryable is ever advertised to the model.
    """
    from app.services.data_source_service import DataSourceService
    from fastapi import HTTPException

    svc = data_source_service or DataSourceService()
    agents = await resolve_run_agents(db, organization, user, report)
    clients: Dict[str, Any] = {}
    for ds in agents:
        try:
            clients.update(await svc.construct_clients(db, ds, user) or {})
        except HTTPException as e:
            if e.status_code != 403:
                raise
            logger.warning("run agents: skipping %s — %s", getattr(ds, "name", "?"), e.detail)
        except Exception:
            logger.exception("run agents: client construction failed for %s", getattr(ds, "name", "?"))
    return agents, clients


async def resolve_candidate_agents(
    db, organization: Any, user: Any, report: Any, mode: str
) -> Tuple[List[Any], str, set]:
    """Return ``(agents, scope_label, attached_ids)`` for search/focus.

    ``attached_ids`` is the raw manual selection (empty under Auto). In manual
    mode the selection is the whole candidate pool — agents outside it are out
    of scope entirely.
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
        # Artifact-chat threads never widen to Auto (see resolve_run_agents);
        # their focus tools are filtered out anyway — defense in depth.
        if getattr(report, "report_type", "regular") == "artifact_chat":
            return [], "attached", set()
        # Auto: the working set is everything accessible; nothing needs attach.
        agents = await accessible_agents(db, organization, user)
        return agents, "accessible", {str(a.id) for a in agents}

    # Manual: the user's selection is a hard scope — no other agents are
    # searched or proposed. The user widens it from the agent selector.
    return attached, "attached", attached_ids


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
