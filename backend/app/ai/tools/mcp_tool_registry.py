"""MCP / custom-API tools as the model's own tools, capped by ``tools_max``.

Each allowed ``ConnectionTool`` on the run's agents becomes one planner tool
(``mcp__<connection>__<tool>``) carrying the server's own JSON Schema, so the
provider constrains decoding against it and the schema sits in the cacheable
prefix. Through the old ``execute_mcp`` gateway every call spelled out a
connection UUID, the tool name, nested arguments and a title: with a 5-tool
server that was 195 output tokens and 2.0 s per tool turn, versus 81 tokens
and 0.95 s native.

``tools_max`` (org setting, default 50; ``BOW_TOOLS_MAX`` overrides) caps how
many are registered, because a very large catalog costs more as permanent
tool definitions than it saves. Past the cap the most used tools (last 30
days) are registered and the rest are reached through ``search_tools`` +
``call_tool``. The list is sorted by connection and tool name, never by
usage, so it is byte-stable for prompt caching.

Dispatch stays on one path: a native or ``call_tool`` call is rewritten into
``execute_mcp`` arguments, so tool policy, per-user identity forwarding,
CSV/JSON materialization and audit are unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Anthropic and OpenAI both cap tool names at 64 characters.
_MAX_TOOL_NAME = 64
_PREFIX = "mcp__"

_DEFAULT_TOOLS_MAX = 50


def _setting_value(organization_settings, key, default):
    """Read one org setting, tolerating a missing/!FeatureConfig/None value."""
    if organization_settings is None:
        return default
    try:
        cfg = organization_settings.get_config(key)
    except Exception:
        return default
    value = getattr(cfg, "value", cfg)
    return default if value is None else value


def _stored(organization_settings, key):
    """The value the organization saved for ``key``, or None (schema default)."""
    cfg = getattr(organization_settings, "config", None)
    if not isinstance(cfg, dict):
        return None
    if key in cfg:
        return cfg[key]
    ai = cfg.get("ai_features")
    return ai.get(key) if isinstance(ai, dict) else None


def tools_max(organization_settings=None) -> int:
    """How many MCP tools are registered natively; the rest go behind search."""
    env = os.environ.get("BOW_TOOLS_MAX")
    if env is not None:
        try:
            return max(0, int(env))
        except (TypeError, ValueError):
            pass
    # Organizations that saved the cap under its previous name keep it until
    # they save ``tools_max``.
    if _stored(organization_settings, "tools_max") is None and _stored(organization_settings, "mcp_native_tools_max") is not None:
        raw = _setting_value(organization_settings, "mcp_native_tools_max", _DEFAULT_TOOLS_MAX)
    else:
        raw = _setting_value(organization_settings, "tools_max", _DEFAULT_TOOLS_MAX)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_TOOLS_MAX


def native_tools_enabled(organization_settings=None, tool_count: Optional[int] = None) -> bool:
    """MCP tools are always native up to ``tools_max``. Kept so the
    ``<mcp_tools>`` renderer and the planner keep asking the same question."""
    return True


def sanitize(value: str) -> str:
    """Reduce a name to the character set every provider accepts."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value or "")


def build_tool_name(connection_slug: str, tool_name: str, taken: set) -> str:
    """``mcp__<connection>__<tool>``, sanitized, unique, and within the 64-char cap.

    Truncation happens on the TOOL half rather than the connection half, so two
    tools from different connections can never collide after trimming. A short
    content hash is appended whenever the name had to be shortened or would
    otherwise duplicate one already issued — a stable suffix, so the same tool
    keeps the same name across runs (important for prompt caching).
    """
    conn = sanitize(connection_slug)[:20].strip("_") or "conn"
    tool = sanitize(tool_name)
    base = f"{_PREFIX}{conn}__{tool}"

    if len(base) <= _MAX_TOOL_NAME and base.lower() not in taken:
        taken.add(base.lower())
        return base

    digest = hashlib.sha1(f"{connection_slug}:{tool_name}".encode()).hexdigest()[:6]
    room = _MAX_TOOL_NAME - len(_PREFIX) - len(conn) - len("____") - len(digest)
    trimmed = tool[: max(room, 1)]
    name = f"{_PREFIX}{conn}__{trimmed}__{digest}"[:_MAX_TOOL_NAME]

    # A collision here would require the same connection+tool pair twice, which
    # the caller already de-duplicates; loop defensively rather than assume.
    counter = 0
    while name.lower() in taken:
        counter += 1
        suffix = f"{digest}{counter}"
        room = _MAX_TOOL_NAME - len(_PREFIX) - len(conn) - len("____") - len(suffix)
        name = f"{_PREFIX}{conn}__{tool[: max(room, 1)]}__{suffix}"[:_MAX_TOOL_NAME]
    taken.add(name.lower())
    return name


def normalize_schema(schema: Any) -> Dict[str, Any]:
    """Make an MCP input schema safe to send as a provider tool schema.

    Servers emit schemas of varying quality: missing ``type``, absent
    ``properties``, a stray ``$schema`` draft marker. Providers reject some of
    these outright. ``$ref``/``$defs`` are inlined so the shape is literal at the
    point of use, matching what the ``<mcp_tools>`` renderer already does.
    """
    from app.ai.tools.mcp_schema import resolve_refs

    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}

    try:
        resolved = resolve_refs(schema)
    except Exception:
        resolved = schema
    if not isinstance(resolved, dict):
        return {"type": "object", "properties": {}}

    out = {k: v for k, v in resolved.items() if k != "$schema"}
    out["type"] = "object"
    if not isinstance(out.get("properties"), dict):
        out["properties"] = {}
    if "required" in out and not isinstance(out["required"], list):
        out.pop("required")
    return out


def parse_native_tool_name(name: str) -> Optional[str]:
    """True-ish check: is this one of ours? Returns the raw name if so."""
    return name if isinstance(name, str) and name.startswith(_PREFIX) else None


@dataclass
class McpToolCatalog:
    """The run's MCP / custom-API tools, split by the ``tools_max`` cap.

    ``native``: descriptor dicts registered as the model's own tools, sorted by
    connection then tool name so the request's tool list is byte-stable.
    ``deferred``: the tools past the cap, reachable through ``search_tools`` +
    ``call_tool`` (each entry carries name, description, schema, connection).
    ``routing``: generated name -> ``{"connection_id", "tool_name"}`` for every
    tool, native or deferred, so dispatch resolves either the same way.
    """

    native: List[Dict[str, Any]] = field(default_factory=list)
    deferred: List[Dict[str, Any]] = field(default_factory=list)
    routing: Dict[str, Dict[str, str]] = field(default_factory=dict)


# How long a usage ranking is reused. Recomputing it at most once a day keeps
# the cap's selection (and so the tool list) stable within a conversation.
_USAGE_TTL_S = 24 * 3600
_USAGE_WINDOW_DAYS = 30
_USAGE_ROW_LIMIT = 5000
_usage_cache: Dict[str, Tuple[float, Dict[Tuple[str, str], int]]] = {}


def clear_usage_cache() -> None:
    _usage_cache.clear()


async def _usage_counts(db, organization_id: Optional[str]) -> Dict[Tuple[str, str], int]:
    """Calls per (connection_id, tool_name) over the last 30 days, per org.

    Every MCP call, native or not, is persisted as ``execute_mcp`` with its
    connection and tool in the arguments, so one query covers both.
    """
    import time
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from app.models.agent_execution import AgentExecution
    from app.models.tool_execution import ToolExecution

    if not organization_id:
        return {}
    key = str(organization_id)
    hit = _usage_cache.get(key)
    if hit and time.monotonic() - hit[0] < _USAGE_TTL_S:
        return hit[1]
    counts: Dict[Tuple[str, str], int] = {}
    try:
        cutoff = datetime.utcnow() - timedelta(days=_USAGE_WINDOW_DAYS)
        rows = (await db.execute(
            select(ToolExecution.arguments_json)
            .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
            .where(
                AgentExecution.organization_id == key,
                ToolExecution.tool_name == "execute_mcp",
                ToolExecution.created_at >= cutoff,
            )
            .order_by(ToolExecution.created_at.desc())
            .limit(_USAGE_ROW_LIMIT)
        )).scalars().all()
        for args in rows:
            if isinstance(args, dict) and args.get("connection_id") and args.get("tool_name"):
                k = (str(args["connection_id"]), str(args["tool_name"]))
                counts[k] = counts.get(k, 0) + 1
    except Exception as e:
        logger.debug("mcp tool usage ranking unavailable: %s", e)
        return {}
    _usage_cache[key] = (time.monotonic(), counts)
    return counts


async def build_mcp_tool_catalog(
    db, report, user=None, organization_settings=None, data_source_ids: Optional[List[str]] = None,
) -> McpToolCatalog:
    """The MCP / custom-API tools on the run's agents, split at ``tools_max``.

    Scope is ``data_source_ids`` (the run's agents: the pinned ones, or every
    agent the user can access in Auto); without ids, the report's attached
    agents. A tool is left out when it is disabled, or denied for this user,
    under the strictest setting across the run's agents that carry it
    (``strictest_admin_setting``, the same rule the call-time check applies).

    Which tools are native: the most used on this org's agents in the last 30
    days, then the rest alphabetically, up to the cap. The selection is then
    sorted by connection and tool name, so usage only decides what makes the
    cut, never the order.

    Returns an empty catalog on any failure; dispatch still works for a
    tool already in the conversation.
    """
    from sqlalchemy import select
    from app.models.connection import Connection
    from app.models.connection_tool import ConnectionTool
    from app.models.data_source_connection_tool import DataSourceConnectionTool
    from app.models.domain_connection import domain_connection
    from app.models.report_data_source_association import report_data_source_association
    from app.services.mcp_context_injection import filter_locked_from_schema
    from app.services.tool_policy_service import (
        ToolPolicyService, resolve_effective_policy, strictest_admin_setting,
    )

    catalog = McpToolCatalog()
    try:
        if data_source_ids is not None:
            scope_ds_ids = [str(i) for i in data_source_ids if i]
        else:
            scope_ds_ids = [str(r[0]) for r in (await db.execute(
                select(report_data_source_association.c.data_source_id)
                .where(report_data_source_association.c.report_id == str(report.id))
            )).all()]
        if not scope_ds_ids:
            return catalog

        # IN (subquery), not JOIN + DISTINCT: Postgres has no equality
        # operator for the JSON config column, so DISTINCT on it errors.
        conns = (await db.execute(
            select(Connection)
            .where(
                Connection.id.in_(
                    select(domain_connection.c.connection_id)
                    .where(domain_connection.c.data_source_id.in_(scope_ds_ids))
                ),
                Connection.type.in_(["mcp", "custom_api"]),
            )
        )).scalars().all()
        if not conns:
            return catalog

        import json as _json
        conn_by_id = {}
        for c in conns:
            cfg = c.config
            if isinstance(cfg, str):
                try:
                    cfg = _json.loads(cfg)
                except Exception:
                    cfg = {}
            conn_by_id[str(c.id)] = {"name": c.name, "config": cfg or {}}

        tools = (await db.execute(
            select(ConnectionTool).where(
                ConnectionTool.connection_id.in_(list(conn_by_id.keys()))
            )
        )).scalars().all()
        if not tools:
            return catalog

        # Which of the run's agents carry each connection.
        agents_by_conn: Dict[str, set] = {}
        for ds_id, conn_id in (await db.execute(
            select(domain_connection.c.data_source_id, domain_connection.c.connection_id)
            .where(
                domain_connection.c.data_source_id.in_(scope_ds_ids),
                domain_connection.c.connection_id.in_(list(conn_by_id.keys())),
            )
        )).all():
            agents_by_conn.setdefault(str(conn_id), set()).add(str(ds_id))

        overlays_by_tool: Dict[str, list] = {}
        all_agents = sorted({a for ids in agents_by_conn.values() for a in ids})
        if all_agents:
            for o in (await db.execute(
                select(DataSourceConnectionTool).where(
                    DataSourceConnectionTool.data_source_id.in_(all_agents),
                    DataSourceConnectionTool.connection_tool_id.in_([str(t.id) for t in tools]),
                )
            )).scalars().all():
                overlays_by_tool.setdefault(str(o.connection_tool_id), []).append(o)

        user_prefs = {}
        if user is not None:
            user_prefs = await ToolPolicyService().get_user_preferences(
                db, str(user.id), [str(t.id) for t in tools]
            )

        # Only tools the run can actually call: enabled and not denied, under
        # the strictest setting across the agents that carry them.
        eligible: List[Tuple[Any, str]] = []
        for t in tools:
            agents = agents_by_conn.get(str(t.connection_id), set())
            is_enabled, admin_policy = strictest_admin_setting(
                t, overlays_by_tool.get(str(t.id), []), len(agents) or None,
            )
            if not is_enabled:
                continue
            effective = resolve_effective_policy(admin_policy, user_prefs.get(str(t.id)))
            if effective == "deny":
                continue
            eligible.append((t, effective))
        if not eligible:
            return catalog

        def _sort_key(item):
            t = item[0]
            cname = (conn_by_id.get(str(t.connection_id)) or {}).get("name") or ""
            return (cname.lower(), str(t.connection_id), (t.name or "").lower())

        # Names are issued in the stable order, so a tool keeps its name no
        # matter which side of the cap it lands on.
        eligible.sort(key=_sort_key)
        budget = tools_max(organization_settings)
        usage = await _usage_counts(db, getattr(report, "organization_id", None)) if len(eligible) > budget else {}
        ranked = sorted(
            eligible,
            key=lambda it: (-usage.get((str(it[0].connection_id), it[0].name), 0), _sort_key(it)),
        )
        native_ids = {str(t.id) for t, _ in ranked[:budget]}

        taken: set = set()
        for t, effective in eligible:
            cid = str(t.connection_id)
            info = conn_by_id.get(cid) or {}
            try:
                visible = filter_locked_from_schema(t.input_schema, info.get("config", {}))
            except Exception:
                visible = t.input_schema

            name = build_tool_name(info.get("name") or cid, t.name, taken)
            desc = (t.description or f"Tool '{t.name}' on {info.get('name') or 'connection'}.").strip()
            if effective in ("ask", "auto"):
                desc += (
                    f"\n\n(Policy: {effective} — this call is reviewed before it runs "
                    "and may be declined.)"
                )
            schema = normalize_schema(visible)
            catalog.routing[name] = {"connection_id": cid, "tool_name": t.name}
            if str(t.id) in native_ids:
                catalog.native.append({
                    "name": name,
                    "description": desc,
                    "schema": schema,
                    "category": "both",
                    "research_accessible": True,
                    "is_active": True,
                })
            else:
                catalog.deferred.append({
                    "name": name,
                    "description": desc,
                    "input_schema": schema,
                    "connection_id": cid,
                    "connection_name": info.get("name") or "",
                    "tool_name": t.name,
                    "policy": effective,
                })
        if catalog.deferred:
            logger.info(
                "mcp tools: %d native (tools_max=%d), %d behind search_tools",
                len(catalog.native), budget, len(catalog.deferred),
            )
        return catalog

    except Exception as e:
        logger.warning("mcp tool catalog failed; no MCP tools registered: %s", e)
        return McpToolCatalog()


async def build_native_mcp_tools(
    db, report, user=None, organization_settings=None, data_source_ids: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, str]]]:
    """``(native descriptors, routing)`` — the native half of the catalog."""
    catalog = await build_mcp_tool_catalog(
        db, report, user, organization_settings=organization_settings, data_source_ids=data_source_ids,
    )
    return catalog.native, catalog.routing
