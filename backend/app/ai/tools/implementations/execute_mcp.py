import logging
import time
from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.execute_mcp import ExecuteMCPInput, ExecuteMCPOutput
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ee.audit.tool_audit import log_tool_audit

logger = logging.getLogger(__name__)


class ExecuteMCPTool(Tool):
    """Execute a tool on an MCP server or custom API endpoint."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="execute_mcp",
            description="""
            Purpose:
Execute a tool on a connected MCP server or custom API endpoint.
Returns the tool's output. Tabular results are automatically saved as CSV files
that can be loaded by create_data for visualization.

Use when:
    - You need to fetch data from an external tool (Notion, Jira, Datadog, etc.)
    - You need to invoke an API endpoint to retrieve or submit data
    - Use search_mcps first to discover available tools and their input schemas

Do not use when:
    - You need to query a SQL database (use create_data instead)
    - You need to read uploaded files (use inspect_data instead)
            """,
            category="both",
            version="1.0.0",
            input_schema=ExecuteMCPInput.model_json_schema(),
            output_schema=ExecuteMCPOutput.model_json_schema(),
            tags=["mcp", "tools", "api", "execution"],
            timeout_seconds=60,
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ExecuteMCPInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ExecuteMCPOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = ExecuteMCPInput(**tool_input)
        organization_settings = runtime_ctx.get("settings")

        # Feature gate check
        if organization_settings:
            enable_mcp = organization_settings.get_config("enable_mcp_tools")
            if enable_mcp and not enable_mcp.value:
                await log_tool_audit(
                    runtime_ctx,
                    action="tool.access_blocked_by_policy",
                    resource_type="report",
                    resource_id=str(runtime_ctx.get("report").id) if runtime_ctx.get("report") else None,
                    details={"tool": "execute_mcp", "policy": "enable_mcp_tools"},
                )
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": {"success": False, "error_message": "MCP tools are disabled for this organization."},
                        "observation": {"summary": "execute_mcp blocked: enable_mcp_tools is disabled", "success": False},
                    },
                )
                return

        yield ToolStartEvent(type="tool.start", payload={
            "title": f"Executing {data.tool_name}",
            "connection_id": data.connection_id,
        })

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        organization = runtime_ctx.get("organization")
        if not db or not organization:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error_message": "Missing database session or organization context."},
                    "observation": {"summary": "Missing context", "success": False},
                },
            )
            return

        # Validate connection belongs to org and tool is enabled
        from sqlalchemy import select
        from app.models.connection import Connection
        from app.models.connection_tool import ConnectionTool
        from app.services.connection_service import ConnectionService

        # Resolve connection — only allow MCP/API connections linked to this report's data sources
        from sqlalchemy import or_
        report = runtime_ctx.get("report")
        allowed_conn_ids = set()
        if report:
            for ds in (report.data_sources or []):
                for conn in (ds.connections or []):
                    if conn.type in ("mcp", "custom_api"):
                        allowed_conn_ids.add(str(conn.id))

        conn_result = await db.execute(
            select(Connection).where(
                or_(
                    Connection.id == data.connection_id,
                    Connection.name == data.connection_id,
                ),
                Connection.organization_id == str(organization.id),
                Connection.type.in_(["mcp", "custom_api"]),
            )
        )
        connection = conn_result.scalars().first()

        # Verify connection is linked to this report's data sources
        if connection and allowed_conn_ids and str(connection.id) not in allowed_conn_ids:
            connection = None
        if not connection:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error_message": f"Connection '{data.connection_id}' not found."},
                    "observation": {"summary": "Connection not found", "success": False},
                },
            )
            return

        # Emit connection name so the UI can show it during streaming
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "connection_resolved", "connection_name": connection.name})

        # Check tool is enabled
        tool_result = await db.execute(
            select(ConnectionTool).where(
                ConnectionTool.connection_id == str(connection.id),
                ConnectionTool.name == data.tool_name,
            )
        )
        tool_record = tool_result.scalar_one_or_none()
        if tool_record and not tool_record.is_enabled:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error_message": f"Tool '{data.tool_name}' is disabled."},
                    "observation": {"summary": f"Tool '{data.tool_name}' is disabled by admin", "success": False},
                },
            )
            return

        # Construct client and call tool
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "calling_tool"})

        try:
            # Try in-process MCP tool first (avoids HTTP self-call which deadlocks on SQLite)
            result = await self._try_inprocess_mcp(
                db, data.tool_name, data.arguments, runtime_ctx, organization
            )

            if result is None:
                # Not an internal tool — call via MCP protocol over HTTP
                service = ConnectionService()
                client = await service.construct_client(db, connection)
                logger.info(f"execute_mcp: Calling remote MCP: {getattr(client, 'server_url', '?')}")
                result = await client.acall_tool(data.tool_name, data.arguments)
                logger.info(f"execute_mcp: Remote call returned success={result.get('success')}, error={result.get('error')}")
        except BaseException as e:
            logger.error(f"execute_mcp: Tool call failed: {e}", exc_info=True)
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error_message": str(e)},
                    "observation": {"summary": f"Tool call failed: {e}", "success": False},
                },
            )
            return

        if not result.get("success"):
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error_message": result.get("error", "Unknown error")},
                    "observation": {"summary": f"Tool returned error: {result.get('error')}", "success": False},
                },
            )
            return

        # Handle result based on content type
        content_type = result.get("content_type", "json")
        result_data = result.get("data")

        output = {
            "success": True,
            "content_type": content_type,
            "connection_name": connection.name,
            "file_id": None,
            "file_name": None,
            "row_count": None,
            "preview": None,
            "error_message": None,
        }

        if content_type == "tabular" and isinstance(result_data, list):
            # Auto-materialize tabular data to CSV
            yield ToolProgressEvent(type="tool.progress", payload={"stage": "materializing_csv"})
            try:
                file_record = await self._materialize_to_csv(
                    result_data, data.tool_name, runtime_ctx
                )
                output["file_id"] = str(file_record.id)
                output["file_name"] = file_record.filename
                output["row_count"] = len(result_data)
                output["preview"] = result_data[:3] if len(result_data) > 3 else result_data
            except Exception as e:
                logger.warning(f"execute_mcp: CSV materialization failed, returning inline: {e}")
                output["preview"] = result_data[:10] if len(result_data) > 10 else result_data
                output["row_count"] = len(result_data)
        elif content_type == "text":
            # Truncate for observation
            text = str(result_data)
            output["preview"] = text[:3000] if len(text) > 3000 else text
        else:
            # JSON or other
            import json
            try:
                preview_str = json.dumps(result_data, default=str)
                if len(preview_str) < 3000:
                    output["preview"] = result_data
                else:
                    # Truncated preview so the model can see the structure
                    output["preview"] = preview_str[:3000] + f"… [truncated, {len(preview_str)} total chars]"
                    # Materialize full JSON to a file for downstream use (e.g. write_csv)
                    yield ToolProgressEvent(type="tool.progress", payload={"stage": "materializing_json"})
                    try:
                        file_record = await self._materialize_to_json(
                            result_data, data.tool_name, runtime_ctx
                        )
                        output["file_id"] = str(file_record.id)
                        output["file_name"] = file_record.filename
                    except Exception as e:
                        logger.warning(f"execute_mcp: JSON materialization failed: {e}")
            except Exception:
                output["preview"] = str(result_data)[:3000]

        # Audit
        await log_tool_audit(
            runtime_ctx,
            action="tool.mcp_executed",
            resource_type="report",
            resource_id=str(report.id) if report else None,
            details={
                "tool": "execute_mcp",
                "connection_id": data.connection_id,
                "tool_name": data.tool_name,
                "content_type": content_type,
                "file_id": output.get("file_id"),
            },
        )

        summary = f"Executed '{data.tool_name}'"
        if output.get("file_id") and content_type == "tabular":
            summary += f" → materialized to CSV ({output['row_count']} rows)"
        elif output.get("file_id"):
            summary += f" → saved as {output['file_name']} (use write_csv to extract tabular data)"
        elif output.get("row_count"):
            summary += f" → {output['row_count']} rows (inline)"
        else:
            summary += f" → {content_type} result"

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": {
                    "summary": summary,
                    "content_type": content_type,
                    "file_id": output.get("file_id"),
                    "preview": output.get("preview"),
                    "row_count": output.get("row_count"),
                    "success": True,
                },
            },
        )

    async def _materialize_to_csv(self, data: list, tool_name: str, runtime_ctx: dict):
        """Save tabular data as a CSV file, create a File record, and link to report."""
        import pandas as pd
        import aiofiles
        from uuid import uuid4
        from app.models.file import File
        from app.services.file_preview import generate_csv_preview

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("current_user")

        df = pd.DataFrame(data)
        safe_name = tool_name.replace("/", "_").replace(" ", "_")
        unique_name = f"{uuid4()}_{safe_name}.csv"
        path = f"uploads/files/{unique_name}"

        # Write CSV
        df.to_csv(path, index=False)

        # Generate preview
        preview = None
        try:
            preview = generate_csv_preview(path)
        except Exception:
            pass

        # Create File record
        file = File(
            filename=f"{safe_name}.csv",
            path=path,
            content_type="text/csv",
            preview=preview,
            user_id=str(user.id) if user else None,
            organization_id=str(organization.id) if organization else None,
        )
        db.add(file)

        # Link to report if available
        if report:
            from app.models.report_file_association import report_file_association
            from sqlalchemy import insert
            await db.execute(
                insert(report_file_association).values(
                    report_id=str(report.id),
                    file_id=str(file.id),
                )
            )

        await db.flush()
        return file

    async def _materialize_to_json(self, data: Any, tool_name: str, runtime_ctx: dict):
        """Save large JSON result as a file so write_csv can process it."""
        import json
        from uuid import uuid4
        from app.models.file import File

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("current_user")

        safe_name = tool_name.replace("/", "_").replace(" ", "_")
        unique_name = f"{uuid4()}_{safe_name}.json"
        path = f"uploads/files/{unique_name}"

        with open(path, "w") as f:
            json.dump(data, f, default=str)

        file = File(
            filename=f"{safe_name}.json",
            path=path,
            content_type="application/json",
            user_id=str(user.id) if user else None,
            organization_id=str(organization.id) if organization else None,
        )
        db.add(file)

        if report:
            from app.models.report_file_association import report_file_association
            from sqlalchemy import insert
            await db.execute(
                insert(report_file_association).values(
                    report_id=str(report.id),
                    file_id=str(file.id),
                )
            )

        await db.flush()
        return file

    async def _try_inprocess_mcp(
        self,
        db,
        tool_name: str,
        arguments: dict,
        runtime_ctx: dict,
        organization,
    ) -> dict | None:
        """
        Try to execute the tool in-process using the internal MCP tool registry.
        Returns a result dict if the tool exists internally, or None to fall back to HTTP.

        This avoids the HTTP self-call which deadlocks on SQLite (database is locked)
        and is faster even on PostgreSQL since it skips auth + HTTP overhead.
        """
        from app.ai.tools.mcp import get_mcp_tool

        tool_class = get_mcp_tool(tool_name)
        if not tool_class:
            return None  # Not an internal tool — caller should use HTTP

        try:
            tool = tool_class()
            user = runtime_ctx.get("user") or runtime_ctx.get("current_user")
            logger.info(f"execute_mcp: Calling internal MCP tool '{tool_name}' in-process, user={getattr(user, 'id', None)}")
            data = await tool.execute(arguments, db, user, organization)

            # Detect content type
            content_type = "json"
            if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                content_type = "tabular"
            elif isinstance(data, str):
                content_type = "text"

            return {
                "success": True,
                "data": data,
                "content_type": content_type,
                "error": None,
            }
        except Exception as e:
            logger.error(f"execute_mcp: Internal MCP tool '{tool_name}' failed: {e}", exc_info=True)
            return {
                "success": False,
                "data": None,
                "content_type": "text",
                "error": str(e),
            }
