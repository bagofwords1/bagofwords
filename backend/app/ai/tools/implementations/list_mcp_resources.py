import logging
from typing import AsyncIterator, Dict, Any, Type, List

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.list_mcp_resources import ListMcpResourcesInput, ListMcpResourcesOutput
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)

logger = logging.getLogger(__name__)

# Cap the combined list so a server with thousands of resources can't blow up
# the agent's context. Mirrors the Go bridge's maxResourcesListed.
MAX_RESOURCES_LISTED = 200


class ListMcpResourcesTool(Tool):
    """Discover MCP *resources* (data the server reads on demand) and URI templates."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_mcp_resources",
            description="""
            Purpose:
List data resources exposed by connected MCP servers (documents, rulebooks, schemas, records, etc.).
Returns resource URIs with names and descriptions, including parameterized URI templates.

MCP *resources* are different from MCP *tools* (use search_mcps for tools) and from indexed
metadata (use read_resources for dbt/LookML/docs). Call this first to discover what resources
exist, then read_mcp_resource to fetch a resource's content by URI.

Use when:
    - An MCP server may expose business rules, definitions, glossaries, or schema docs as resources.
    - You have a URI like 'pulse://rules' and want to confirm it exists / find related resources.
            """,
            category="research",
            version="1.0.0",
            input_schema=ListMcpResourcesInput.model_json_schema(),
            output_schema=ListMcpResourcesOutput.model_json_schema(),
            tags=["mcp", "resources", "discovery", "research"],
            timeout_seconds=30,
            idempotent=True,
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ListMcpResourcesInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ListMcpResourcesOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = ListMcpResourcesInput(**tool_input)
        organization_settings = runtime_ctx.get("settings")

        # Feature gate — same flag that governs search_mcps/execute_mcp.
        if organization_settings:
            enable_mcp = organization_settings.get_config("enable_mcp_tools")
            if enable_mcp and not enable_mcp.value:
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": {"resources": [], "total_count": 0, "truncated": False, "errors": []},
                        "observation": {"summary": "MCP tools are disabled for this organization.", "success": False},
                    },
                )
                return

        yield ToolStartEvent(type="tool.start", payload={"title": "Listing MCP resources"})

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        current_user = runtime_ctx.get("user") or runtime_ctx.get("current_user")

        if not db or not report:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"resources": [], "total_count": 0, "truncated": False, "errors": []},
                    "observation": {"summary": "No database session or report available.", "success": False},
                },
            )
            return

        # Resolve MCP connections linked to this report's data sources via an
        # explicit join — lazy-loading report.data_sources → ds.connections
        # silently returns empty in async context (see search_mcps). Resources
        # are an MCP-only concept, so we filter to type == "mcp".
        from sqlalchemy import select
        from app.models.connection import Connection
        from app.models.domain_connection import domain_connection
        from app.models.report_data_source_association import report_data_source_association

        conn_result = await db.execute(
            select(Connection)
            .join(domain_connection, domain_connection.c.connection_id == Connection.id)
            .join(
                report_data_source_association,
                report_data_source_association.c.data_source_id == domain_connection.c.data_source_id,
            )
            .where(
                report_data_source_association.c.report_id == str(report.id),
                Connection.type == "mcp",
            )
        )
        connections = list({str(c.id): c for c in conn_result.scalars().all()}.values())

        if data.connection_id:
            wanted = str(data.connection_id)
            connections = [c for c in connections if str(c.id) == wanted or c.name == wanted]

        if not connections:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"resources": [], "total_count": 0, "truncated": False, "errors": []},
                    "observation": {"summary": "No MCP connections found on linked data sources.", "success": False},
                },
            )
            return

        from app.services.connection_service import ConnectionService
        service = ConnectionService()

        items: List[Dict[str, Any]] = []
        errors: List[str] = []
        truncated = False

        for conn in connections:
            if len(items) >= MAX_RESOURCES_LISTED:
                truncated = True
                break
            yield ToolProgressEvent(type="tool.progress", payload={"stage": "listing", "connection_name": conn.name})
            try:
                client = await service.construct_client(db, conn, current_user)
            except Exception as e:
                errors.append(f"{conn.name}: failed to connect: {e}")
                continue

            # Concrete resources and URI templates come from two separate calls;
            # templates are optional, so a failure there is non-fatal.
            try:
                resources = await client.alist_resources()
            except Exception as e:
                errors.append(f"{conn.name}: list resources failed: {e}")
                resources = []
            try:
                templates = await client.alist_resource_templates()
            except Exception:
                # Many servers don't implement templates/list — treat as none.
                templates = []

            for r in (resources or []) + (templates or []):
                if len(items) >= MAX_RESOURCES_LISTED:
                    truncated = True
                    break
                item = dict(r)
                item.setdefault("is_template", False)
                item["connection_id"] = str(conn.id)
                item["connection_name"] = conn.name
                items.append(item)

        summary = f"Found {len(items)} resource(s)/template(s) across {len(connections)} MCP connection(s)."
        if truncated:
            summary += f" (capped at {MAX_RESOURCES_LISTED} — narrow with connection_id)"
        if errors:
            summary += f" {len(errors)} connection error(s)."

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "resources": items,
                    "total_count": len(items),
                    "truncated": truncated,
                    "errors": errors,
                },
                "observation": {
                    "summary": summary,
                    "resources": items,
                    "errors": errors,
                    "success": True,
                },
            },
        )
