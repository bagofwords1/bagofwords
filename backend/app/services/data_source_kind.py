"""Agent vs connector — what a data source is to the people using it.

* **agent** — something the org curates for analysis: databases, BI, shared
  file catalogs (SharePoint, a Drive service account, network shares), the
  browser, custom APIs. Auto picks among agents; they render as schemas.
* **connector** — a live line into each person's own account or tools: Gmail,
  Outlook, Teams, personal Drive/OneDrive, MCP presets (Notion, Linear, …).
  A connector is available whenever the person is connected, is never picked
  by Auto, and always runs as that person.

An explicit ``DataSource.kind`` (set by the create flow or an admin) wins.
Otherwise it is derived from the linked connections: a connector when EVERY
connection is a connector type, an agent as soon as one connection is
something to analyse (Snowflake + Gmail is an agent with Gmail attached).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, FrozenSet

AGENT = "agent"
CONNECTOR = "connector"
KINDS = (AGENT, CONNECTOR)


@lru_cache(maxsize=1)
def connector_types() -> FrozenSet[str]:
    """Connection types that are connectors by nature: MCP servers, and the
    sign-in integrations whose content is each user's own (per-user catalog).

    OneNote is an integration too, but its catalog is shared (a team
    notebook indexed once for everyone), so it stays an agent.
    """
    from app.schemas.data_source_registry import REGISTRY

    out = set()
    for type_, entry in REGISTRY.items():
        if entry.ui_form == "mcp":
            out.add(type_)
        elif entry.ui_form == "integration" and entry.catalog_ownership == "per_user":
            out.add(type_)
    return frozenset(out)


def connection_is_connector(conn: Any) -> bool:
    ctype = getattr(conn, "type", None)
    if ctype not in connector_types():
        return False
    # A per-user integration configured with SHARED credentials (a Drive
    # service account, an Entra app-only mailbox) is the org's curated source,
    # not each person's account — an agent. MCP stays a connector either way:
    # an org-wide key still exposes a tool provider, shown as "Shared".
    if ctype != "mcp" and getattr(conn, "auth_policy", None) == "system_only":
        return False
    return True


def derived_kind(ds: Any) -> str:
    conns = list(getattr(ds, "connections", None) or [])
    if conns and all(connection_is_connector(c) for c in conns):
        return CONNECTOR
    return AGENT


def data_source_kind(ds: Any) -> str:
    explicit = getattr(ds, "kind", None)
    if explicit in KINDS:
        return explicit
    return derived_kind(ds)


def is_connector(ds: Any) -> bool:
    return data_source_kind(ds) == CONNECTOR
