"""Native registration of MCP / custom-API tools as first-class planner tools.

Default path today is the ``execute_mcp`` gateway: one tool whose ``arguments``
field is an unconstrained object, so the provider never sees the real schema and
cannot constrain decoding against it. The agent compensates by calling
``search_mcps`` to fetch the schema, which costs a planner turn and a tool round
trip per call.

This module builds the alternative: one ``ToolDescriptor`` per allowed
``ConnectionTool``, each carrying the server's own JSON Schema. The schema then
travels in the request's ``tools`` array, which means it is present at every
generation, sits in the cacheable prefix, and constrains sampling. It is the
pattern used by other MCP clients in the ecosystem.

Dispatch stays on the existing path: a native call is rewritten into the same
``execute_mcp`` arguments, so tool policy, per-user identity forwarding, CSV/JSON
materialization and audit are unchanged.

Gated by ``BOW_MCP_NATIVE_TOOLS`` (default off) and capped by
``BOW_MCP_NATIVE_TOOLS_MAX`` — above the cap the gateway path is kept, because a
very large catalog costs more as permanent tool definitions than it saves in
discovery round trips.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Anthropic and OpenAI both cap tool names at 64 characters.
_MAX_TOOL_NAME = 64
_PREFIX = "mcp__"

_DEFAULT_MAX_NATIVE_TOOLS = 60


def native_tools_enabled() -> bool:
    return os.environ.get("BOW_MCP_NATIVE_TOOLS", "").strip().lower() in ("1", "true", "yes", "on")


def native_tools_budget() -> int:
    try:
        return int(os.environ.get("BOW_MCP_NATIVE_TOOLS_MAX", _DEFAULT_MAX_NATIVE_TOOLS))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_NATIVE_TOOLS


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


async def build_native_mcp_tools(
    db, report, user=None
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, str]]]:
    """Build native tool descriptors for a report's MCP / custom-API tools.

    Returns ``(descriptor_dicts, routing)`` where routing maps the generated tool
    name to ``{"connection_id", "tool_name"}`` for dispatch. Mirrors the
    discovery, policy and locked-field filtering that ``search_mcps`` performs,
    so a tool hidden there is hidden here too.

    Returns empty on any failure — the gateway path remains available, so a
    problem here degrades to today's behavior rather than breaking the run.
    """
    from sqlalchemy import select
    from app.models.connection import Connection
    from app.models.connection_tool import ConnectionTool
    from app.models.data_source_connection_tool import DataSourceConnectionTool
    from app.models.domain_connection import domain_connection
    from app.models.report_data_source_association import report_data_source_association
    from app.services.mcp_context_injection import filter_locked_from_schema
    from app.services.tool_policy_service import ToolPolicyService, resolve_effective_policy

    try:
        conns = (await db.execute(
            select(Connection)
            .join(domain_connection, domain_connection.c.connection_id == Connection.id)
            .join(
                report_data_source_association,
                report_data_source_association.c.data_source_id == domain_connection.c.data_source_id,
            )
            .where(
                report_data_source_association.c.report_id == str(report.id),
                Connection.type.in_(["mcp", "custom_api"]),
            )
        )).scalars().all()
        if not conns:
            return [], {}

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
            return [], {}

        ds_ids = [str(r[0]) for r in (await db.execute(
            select(domain_connection.c.data_source_id)
            .join(
                report_data_source_association,
                report_data_source_association.c.data_source_id == domain_connection.c.data_source_id,
            )
            .where(
                report_data_source_association.c.report_id == str(report.id),
                domain_connection.c.connection_id.in_(list(conn_by_id.keys())),
            )
        )).all()]

        overlays = {}
        if ds_ids:
            rows = (await db.execute(
                select(DataSourceConnectionTool).where(
                    DataSourceConnectionTool.data_source_id.in_(ds_ids),
                    DataSourceConnectionTool.connection_tool_id.in_([str(t.id) for t in tools]),
                )
            )).scalars().all()
            overlays = {str(o.connection_tool_id): o for o in rows}

        user_prefs = {}
        if user is not None:
            user_prefs = await ToolPolicyService().get_user_preferences(
                db, str(user.id), [str(t.id) for t in tools]
            )

        descriptors: List[Dict[str, Any]] = []
        routing: Dict[str, Dict[str, str]] = {}
        taken: set = set()
        budget = native_tools_budget()

        for t in tools:
            overlay = overlays.get(str(t.id))
            if not (overlay.is_enabled if overlay else t.is_enabled):
                continue
            admin_policy = overlay.policy if overlay else t.policy
            effective = resolve_effective_policy(admin_policy, user_prefs.get(str(t.id)))
            if effective == "deny":
                continue

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

            descriptors.append({
                "name": name,
                "description": desc,
                "schema": normalize_schema(visible),
                "category": "both",
                "research_accessible": True,
                "is_active": True,
            })
            routing[name] = {"connection_id": cid, "tool_name": t.name}

            if len(descriptors) >= budget:
                logger.info(
                    "native mcp tools: budget %d reached; %d tool(s) remain on the "
                    "execute_mcp gateway path", budget, len(tools) - len(descriptors),
                )
                break

        return descriptors, routing

    except Exception as e:
        logger.warning("native mcp tool registration failed, falling back to gateway: %s", e)
        return [], {}
