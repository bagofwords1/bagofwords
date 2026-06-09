from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.search_mcps import SearchMCPsInput, SearchMCPsOutput
from app.ai.tools.schemas import ToolEvent, ToolStartEvent, ToolEndEvent


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

        # Query enabled tools
        stmt = select(ConnectionTool).where(
            ConnectionTool.connection_id.in_(list(mcp_connection_ids)),
            ConnectionTool.is_enabled == True,
        )
        result = await db.execute(stmt)
        tools = result.scalars().all()

        # Filter by query if provided.
        #
        # The query is a relevance hint, not a hard filter. A naive full-string
        # substring match (`q in name or q in description`) returns ZERO tools
        # for any natural-language or multi-word query (e.g. "contacts for a
        # company", or an id), which silently defeats discovery — the agent
        # gets no schemas and falls back to guessing argument shapes. Instead:
        #   - tokenize the query and rank tools by how many tokens they match,
        #   - keep only tools that match at least one token,
        #   - but if nothing matches (over-specific/NL query), fall back to
        #     returning all tools so discovery never yields an empty result
        #     when tools actually exist.
        if data.query:
            import re
            tokens = [tok for tok in re.split(r"[^a-z0-9_]+", data.query.lower()) if len(tok) >= 3]
            if tokens:
                def _score(t) -> int:
                    hay = f"{t.name or ''} {t.description or ''}".lower()
                    return sum(1 for tok in tokens if tok in hay)
                scored = sorted(((_score(t), t) for t in tools), key=lambda x: -x[0])
                matched = [t for s, t in scored if s > 0]
                # Fall back to all tools when no token matched — better to return
                # every schema than none.
                tools = matched or tools

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
