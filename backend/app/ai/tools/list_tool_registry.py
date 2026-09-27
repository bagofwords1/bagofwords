"""Native ``submit_<list>`` tools for the Agent Lists on a report's agents.

Mirrors ``mcp_tool_registry.build_native_mcp_tools``: one planner tool per
list, whose input schema is the list's compiled schema, plus a routing table
AgentV2 uses to rewrite the call into the ``submit_list`` gateway.

Prompt-cache rules (docs/design/structured-extraction.md §7.2):
- built once per run, before the loop — never mutated mid-run;
- deterministic order: sorted by (data_source_id, slug), appended after the
  static + MCP tools; schema construction is deterministic.
"""
import logging
from typing import Any, Dict, List, Tuple

from sqlalchemy import select

logger = logging.getLogger(__name__)


async def build_list_tools(db, report, user, organization) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, str]]]:
    from app.models.data_source import DataSource
    from app.models.report_data_source_association import report_data_source_association
    from app.services.agent_lists.access import viewable_agent_ids
    from app.services.agent_lists.compiler import compile_list_schema, tool_description
    from app.services.agent_lists.naming import agent_slug, tool_name_for
    from app.services.agent_lists.service import live_lists_for_agents

    if report is None or user is None or organization is None:
        return [], {}
    ds_ids = [str(r[0]) for r in (await db.execute(
        select(report_data_source_association.c.data_source_id)
        .where(report_data_source_association.c.report_id == str(report.id))
    )).all()]
    if not ds_ids:
        return [], {}
    allowed = await viewable_agent_ids(db, user, organization, ds_ids)
    lists = [l for l in await live_lists_for_agents(db, sorted(allowed))]
    if not lists:
        return [], {}
    agents = {str(d.id): d for d in (await db.execute(
        select(DataSource).where(DataSource.id.in_(list(allowed)))
    )).scalars().all()}

    lists.sort(key=lambda l: (str(l.data_source_id), l.slug))
    slug_counts: Dict[str, int] = {}
    for l in lists:
        slug_counts[l.slug] = slug_counts.get(l.slug, 0) + 1

    descriptors: List[Dict[str, Any]] = []
    routing: Dict[str, Dict[str, str]] = {}
    for l in lists:
        ds = agents.get(str(l.data_source_id))
        agent_name = ds.name if ds else "agent"
        qualifier = agent_slug(agent_name, str(l.data_source_id)) if slug_counts[l.slug] > 1 else None
        name = tool_name_for(l.slug, agent_slug_value=qualifier, list_id=str(l.id))
        if name in routing:  # hash-suffixed fallback for pathological collisions
            name = tool_name_for(l.slug + "_" + str(l.id)[:6], list_id=str(l.id))
        key_name = next((f["name"] for f in (l.fields or []) if f["id"] == l.key_field_id), None)
        descriptors.append({
            "name": name,
            "description": tool_description(l.name, l.description or "", agent_name, key_name),
            "schema": compile_list_schema(l.fields or [], l.description or ""),
            "category": "both",
            "research_accessible": True,
            "is_active": True,
        })
        routing[name] = {"list_id": str(l.id), "list_name": l.name, "data_source_id": str(l.data_source_id)}
    return descriptors, routing
