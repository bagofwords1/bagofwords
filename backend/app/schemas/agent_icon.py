"""The single answer to "what icon does this agent have".

An agent's icon used to be recomputed independently by every surface that drew
one — the agents explorer, the report agent panel, the data tools, two separate
tool-execution serializers — and they disagreed. The worst case was an agent
with more than one connection: ``type`` was taken from ``connections[0]`` while
``connector_key`` scanned for the *first connection resolving a brand*, so an
agent wired to ``[postgresql, notion-mcp]`` rendered notion's logo in the
explorer and postgres' in the tool ticker.

So resolution happens here, once, and every payload that names an agent carries
the result as ``icon_token``. The client renders the token and decides nothing.

Token vocabulary (a subset of the override format already parsed by the
frontend's ``utils/agentIcon.ts``, so an older client understands it):

``emoji:<grapheme>``
    Render the emoji.
``type:<key>``
    Render ``<key>``'s icon, where the key is either a connector brand
    (``notion``) or a connection type (``postgresql``). One kind for both
    because the client resolves a key against the brand map first and the
    type-asset map second — the same fallthrough it already applies to a
    ``type:`` override.
``None``
    Nothing to render (an agent with no connections); the client falls back to
    its own generic glyph.

Precedence: the admin's explicit override wins, then a connector brand, then the
first connection's type. Brand-over-type is a deliberate choice and not
self-evident — for a mostly-postgres agent that also has a notion connection it
shows notion — but it is what the explorer has always shown, which is the icon
users already recognize the agent by.
"""

from typing import Any, Optional, Sequence

EMOJI_PREFIX = "emoji:"
TYPE_PREFIX = "type:"


def _connector_key_from_config(cfg: Any) -> Optional[str]:
    """Preset key ('gmail', 'notion', …) for a tool-provider connection so the UI
    renders the provider's brand icon even though the connection type is 'mcp'.

    Prefer an explicit ``config.catalog_key``; otherwise match ``config.server_url``
    against the MCP presets. Mirrors ``data_source_service._conn_connector_key`` so
    report-embedded connections resolve the same key as the connections route.
    Returns None when the config isn't a known preset connector.
    """
    if isinstance(cfg, str):
        import json as _json
        try:
            cfg = _json.loads(cfg)
        except Exception:
            cfg = None
    if not isinstance(cfg, dict):
        return None
    if cfg.get("catalog_key"):
        return cfg["catalog_key"]
    server_url = cfg.get("server_url")
    if server_url:
        try:
            from app.schemas.data_source_registry import mcp_presets
            for p in mcp_presets():
                if p.get("server_url") == server_url:
                    return p["key"]
        except Exception:
            pass
    return None


def _get(obj: Any, field: str) -> Any:
    """Read a field off a connection, whether it's an ORM row, a Pydantic model
    or a plain dict — the three shapes the call sites actually pass."""
    if isinstance(obj, dict):
        return obj.get(field)
    return getattr(obj, field, None)


def connection_connector_key(conn: Any) -> Optional[str]:
    """The brand key for one connection, or None if it isn't a known connector.

    ``ConnectionEmbedded``/``ConnectionReportEmbedded`` have already derived
    ``connector_key``; ORM ``Connection`` rows have not, so fall back to reading
    their config.
    """
    key = _get(conn, "connector_key")
    if key:
        return key
    return _connector_key_from_config(_get(conn, "config"))


def _parse_override(icon: Optional[str]) -> Optional[str]:
    """The token an admin's explicit override contributes, or None when it
    contributes nothing.

    Mirrors ``parseAgentIcon``: ``emoji:`` and ``type:`` are rendered, anything
    else (``preset:``, a future kind, a garbage value) is ignored so the derived
    icon shows instead of a broken one.
    """
    if not isinstance(icon, str):
        return None
    raw = icon.strip()
    sep = raw.find(":")
    if sep == -1:
        return None
    kind, value = raw[:sep], raw[sep + 1:].strip()
    if not value:
        return None
    if kind == "emoji":
        return f"{EMOJI_PREFIX}{value}"
    if kind == "type":
        return f"{TYPE_PREFIX}{value}"
    return None


def resolve_agent_icon_token(
    icon: Optional[str],
    connections: Optional[Sequence[Any]],
) -> Optional[str]:
    """Resolve an agent's icon to a single token. See the module docstring.

    ``connections`` must be in the order the agent's relationship yields them
    (``Connection.created_at, Connection.id``) — "the first connection" is only
    a stable answer because of that ordering.
    """
    override = _parse_override(icon)
    if override:
        return override

    conns = list(connections or [])
    for conn in conns:
        key = connection_connector_key(conn)
        if key:
            return f"{TYPE_PREFIX}{key}"

    for conn in conns:
        conn_type = _get(conn, "type")
        if conn_type:
            return f"{TYPE_PREFIX}{conn_type}"

    return None
