"""Does a chat session have connection tools that can change something?

The prompt box shows the tool-permission chip (Ask / Auto / Allow all) only
when it can matter: when the run would be offered at least one connection tool
that is not read-only. A session whose agents carry no tools, or only
read-only ones, has nothing for the mode to decide, so the chip stays hidden.

"Offered" is the run's own catalog (``build_mcp_tool_catalog``): the same
agents (``resolve_run_agents``, so Auto and the user's personal connectors are
included), the same enabled / deny / per-agent / personal rules. A tool is
read-only when its ``effect`` is ``read``, or, for legacy rows without an
effect, when its hints say so (``is_read_only_tool``).
"""

import logging
from typing import Any, Iterable, List, Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)

# ConnectionTool.effect values that change something outside the session.
NON_READ_EFFECTS = frozenset({"modify", "external", "destructive"})


def tool_is_read_only(tool: Any) -> bool:
    """True when calling ``tool`` (a ConnectionTool) only reads."""
    effect = getattr(tool, "effect", None)
    if effect:
        return effect not in NON_READ_EFFECTS
    from app.data_sources.clients.mcp_client import is_read_only_tool
    return is_read_only_tool(getattr(tool, "metadata_json", None))


async def _run_agents(db, organization, user, report, data_source_ids: Optional[Iterable[str]]) -> List[Any]:
    from app.ai.tools.implementations.agent_focus_common import (
        accessible_agents, resolve_run_agents, _is_personal_connector,
    )
    ids = [str(i) for i in data_source_ids] if data_source_ids is not None else None
    if report is not None:
        return await resolve_run_agents(db, organization, user, report, agent_ids=ids or None)
    # No report yet (the home page): Auto is every agent the user can access;
    # a pinned selection is those agents plus the user's personal connectors,
    # the same set a report created from it would resolve to.
    reachable = await accessible_agents(db, organization, user)
    if not ids:
        return reachable
    wanted = set(ids)
    return [a for a in reachable if str(a.id) in wanted or _is_personal_connector(a)]


async def has_non_read_tools(
    db, organization, user, *, report=None, data_source_ids: Optional[Iterable[str]] = None,
    organization_settings=None,
) -> Optional[bool]:
    """True / False, or None when the catalog could not be built (unknown:
    the caller should keep the chip, the safe side)."""
    from app.ai.tools.mcp_tool_registry import build_mcp_tool_catalog
    from app.models.connection_tool import ConnectionTool

    if organization_settings is not None:
        try:
            mcp_on = getattr(organization_settings.get_config("enable_mcp_tools"), "value", True)
        except Exception:
            mcp_on = True
        if mcp_on is False:
            return False

    agents = await _run_agents(db, organization, user, report, data_source_ids)
    agent_ids = [str(a.id) for a in agents if getattr(a, "id", None)]
    if not agent_ids:
        return False
    catalog = await build_mcp_tool_catalog(
        db, report, user, organization_settings=organization_settings, data_source_ids=agent_ids,
    )
    if catalog.failed:
        return None
    keys = set(catalog.policies.keys())  # (connection_id, tool_name) of every offered tool
    if not keys:
        return False
    tools = (await db.execute(
        select(ConnectionTool).where(ConnectionTool.connection_id.in_(sorted({cid for cid, _ in keys})))
    )).scalars().all()
    return any(
        (str(t.connection_id), t.name) in keys and not tool_is_read_only(t)
        for t in tools
    )
