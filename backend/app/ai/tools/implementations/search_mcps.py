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

Use when:
    - You need to discover what external tools are available (Notion, Jira, Datadog, etc.)
    - You need the full input schema for a tool before calling execute_mcp
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
        from sqlalchemy.orm import selectinload
        from app.models.connection_tool import ConnectionTool
        from app.models.connection import Connection
        from app.models.data_source import DataSource

        # Get data sources for this report
        data_sources = report.data_sources or []
        if not data_sources:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"tools": [], "total_count": 0},
                    "observation": {"summary": "No data sources linked to this report.", "success": False},
                },
            )
            return

        # Collect all MCP/API connection IDs from linked data sources
        mcp_connection_ids = set()
        conn_info = {}  # connection_id -> {name, type}
        for ds in data_sources:
            for conn in (ds.connections or []):
                if conn.type in ("mcp", "custom_api"):
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

        # Filter by query if provided
        if data.query:
            q = data.query.lower()
            tools = [
                t for t in tools
                if q in (t.name or "").lower() or q in (t.description or "").lower()
            ]

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
