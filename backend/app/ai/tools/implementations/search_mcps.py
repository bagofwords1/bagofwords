from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.search_mcps import SearchMCPsInput, SearchMCPsOutput
from app.ai.tools.schemas import ToolEvent, ToolStartEvent, ToolEndEvent
from app.ai.tools.implementations._search_mcps_query import filter_tools_by_query


class SearchMCPsTool(Tool):
    """Research tool to discover available MCP/API tools and their schemas."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_mcps",
            description="""
            Purpose:
Search for available MCP and custom API tools connected to the current data sources.
Returns full tool descriptions and input schemas so you can understand how to call them.

IMPORTANT: Call this BEFORE execute_mcp to get a tool's exact input schema (the precise
argument names and types). The <mcp_tools> context lists only tool names and descriptions,
not their argument schemas — do not guess argument names. Calling a tool with the wrong
argument shape wastes a turn; fetch the schema here first.

Query (all optional — the query is a relevance hint, never a hard filter):
    - Omit query entirely to list ALL available tools with their schemas.
    - Plain text is matched fuzzily by word: "contacts" or "search contacts" both
      surface contact-related tools, ranked by relevance.
    - Wildcards are supported: "search_*" matches every tool whose name starts with
      "search_"; "*contact*" matches any tool mentioning "contact".
    - If a query matches nothing, ALL tools are returned rather than an empty list —
      so you always get schemas to work with. Prefer a short query (or none) over an
      over-specific natural-language phrase.

Use when:
    - You need to discover what external tools are available (Notion, Jira, Datadog, etc.)
    - You need the full input schema for a tool before calling execute_mcp (do this first)
    - You want to understand what capabilities are available beyond SQL queries
            """,
            category="research",
            version="1.0.0",
            input_schema=SearchMCPsInput.model_json_schema(),
            output_schema=SearchMCPsOutput.model_json_schema(),
            tags=["mcp", "tools", "discovery", "research"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SearchMCPsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SearchMCPsOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = SearchMCPsInput(**tool_input)
        organization_settings = runtime_ctx.get("settings")

        # Feature gate check
        if organization_settings:
            enable_mcp = organization_settings.get_config("enable_mcp_tools")
            if enable_mcp and not enable_mcp.value:
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": {"tools": [], "total_count": 0},
                        "observation": {
                            "summary": "MCP tools are disabled for this organization.",
                            "success": False,
                        },
                    },
                )
                return

        yield ToolStartEvent(type="tool.start", payload={"title": "Searching MCP/API tools"})

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")

        if not db or not report:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"tools": [], "total_count": 0},
                    "observation": {"summary": "No database session or report available.", "success": False},
                },
            )
            return

        from sqlalchemy import select
        from app.models.connection_tool import ConnectionTool
        from app.models.connection import Connection
        from app.models.domain_connection import domain_connection
        from app.models.report_data_source_association import report_data_source_association

        # Query MCP/API connections linked to this report's data sources directly
        # (avoids lazy-loading report.data_sources → ds.connections which silently returns empty in async context)
        conn_result = await db.execute(
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
        )
        mcp_connections = conn_result.scalars().all()

        mcp_connection_ids = set()
        conn_info = {}  # connection_id -> {name, type}
        for conn in mcp_connections:
            cid = str(conn.id)
            mcp_connection_ids.add(cid)
            conn_info[cid] = {"name": conn.name, "type": conn.type}

        if data.connection_ids:
            mcp_connection_ids = mcp_connection_ids.intersection(set(data.connection_ids))

        if not mcp_connection_ids:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"tools": [], "total_count": 0},
                    "observation": {"summary": "No MCP or custom API connections found on linked data sources.", "success": False},
                },
            )
            return

        # Query tools and resolve their effective policy for this run's user
        # (agent overlay > connection default, then user preference). Tools
        # that are disabled at either admin layer, or effectively denied, are
        # hidden from discovery — the agent must not plan around them.
        from app.models.data_source_connection_tool import DataSourceConnectionTool
        from app.services.tool_policy_service import (
            ToolPolicyService, resolve_effective_policy,
        )

        stmt = select(ConnectionTool).where(
            ConnectionTool.connection_id.in_(list(mcp_connection_ids)),
        )
        result = await db.execute(stmt)
        all_tools = result.scalars().all()

        # Overlays from this report's agents only — another agent's overlay on a
        # shared connection must not leak into this run.
        ds_ids_result = await db.execute(
            select(domain_connection.c.data_source_id)
            .join(
                report_data_source_association,
                report_data_source_association.c.data_source_id == domain_connection.c.data_source_id,
            )
            .where(
                report_data_source_association.c.report_id == str(report.id),
                domain_connection.c.connection_id.in_(list(mcp_connection_ids)),
            )
        )
        ds_ids = [str(r[0]) for r in ds_ids_result.all()]
        overlays = {}
        if ds_ids and all_tools:
            overlay_rows = await db.execute(
                select(DataSourceConnectionTool).where(
                    DataSourceConnectionTool.data_source_id.in_(ds_ids),
                    DataSourceConnectionTool.connection_tool_id.in_(
                        [str(t.id) for t in all_tools]
                    ),
                )
            )
            overlays = {str(o.connection_tool_id): o for o in overlay_rows.scalars().all()}

        run_user = runtime_ctx.get("user") or runtime_ctx.get("current_user")
        user_prefs = {}
        if run_user is not None and all_tools:
            user_prefs = await ToolPolicyService().get_user_preferences(
                db, str(run_user.id), [str(t.id) for t in all_tools]
            )

        tools = []
        tool_policies = {}
        for t in all_tools:
            overlay = overlays.get(str(t.id))
            is_enabled = overlay.is_enabled if overlay else t.is_enabled
            if not is_enabled:
                continue
            admin_policy = overlay.policy if overlay else t.policy
            effective = resolve_effective_policy(admin_policy, user_prefs.get(str(t.id)))
            if effective == "deny":
                continue
            tool_policies[str(t.id)] = effective
            tools.append(t)

        # Filter by query if provided.
        #
        # The query is a relevance hint, not a hard filter — a naive substring
        # match returns ZERO tools for natural-language/multi-word/id queries,
        # which silently defeats discovery (the agent then guesses argument
        # shapes). filter_tools_by_query handles plain token-ranking AND glob
        # wildcards (e.g. "search_*", "*contact*"), and always falls back to
        # returning all tools when nothing matches.
        tools = filter_tools_by_query(tools, data.query)

        # Build output
        tool_previews = []
        for t in tools:
            ci = conn_info.get(str(t.connection_id), {})
            tool_previews.append({
                "name": t.name,
                "description": t.description or "",
                "connection_id": str(t.connection_id),
                "connection_name": ci.get("name", ""),
                "connection_type": ci.get("type", ""),
                "input_schema": t.input_schema,
                "policy": tool_policies.get(str(t.id), "allow"),
            })

        summary = f"Found {len(tool_previews)} tool(s) across {len(mcp_connection_ids)} MCP/API connection(s)."

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {"tools": tool_previews, "total_count": len(tool_previews)},
                "observation": {
                    "summary": summary,
                    "tools": tool_previews,
                    "success": True,
                },
            },
        )
