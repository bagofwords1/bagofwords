import logging
from typing import AsyncIterator, Dict, Any, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.read_mcp_resource import ReadMcpResourceInput, ReadMcpResourceOutput
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ai.tools.implementations._mcp_resource_helpers import (
    format_resource_contents,
    select_mcp_connection,
)
from app.ee.audit.tool_audit import log_tool_audit

logger = logging.getLogger(__name__)


class ReadMcpResourceTool(Tool):
    """Read the content of an MCP *resource* by URI."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_mcp_resource",
            description="""
            Purpose:
Read the content of a resource exposed by a connected MCP server, by its URI
(e.g. 'pulse://rules'). Use list_mcp_resources first to discover URIs, or build one from a
URI template. Returns the resource content (text; binary is summarized, not inlined).

MCP resources often hold business rules, definitions, and schema docs — if a relevant
resource exists, read it BEFORE querying so you apply the right definitions.

Do not use for:
    - MCP tools (use execute_mcp) or indexed dbt/LookML/docs metadata (use read_resources).
            """,
            category="research",
            version="1.0.0",
            input_schema=ReadMcpResourceInput.model_json_schema(),
            output_schema=ReadMcpResourceOutput.model_json_schema(),
            tags=["mcp", "resources", "read", "research"],
            timeout_seconds=30,
            idempotent=True,
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadMcpResourceInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadMcpResourceOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = ReadMcpResourceInput(**tool_input)
        organization_settings = runtime_ctx.get("settings")

        # Feature gate — same flag that governs search_mcps/execute_mcp.
        if organization_settings:
            enable_mcp = organization_settings.get_config("enable_mcp_tools")
            if enable_mcp and not enable_mcp.value:
                yield self._end(False, error="MCP tools are disabled for this organization.")
                return

        yield ToolStartEvent(type="tool.start", payload={"title": f"Reading {data.uri}", "uri": data.uri})

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        organization = runtime_ctx.get("organization")
        current_user = runtime_ctx.get("user") or runtime_ctx.get("current_user")

        if not db or not report or not organization:
            yield self._end(False, uri=data.uri, error="Missing database session, report, or organization context.")
            return

        # Resolve MCP connections linked to this report (explicit join — see
        # search_mcps for why lazy-loading is unreliable in async context).
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "resolving_connection"})
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
                Connection.organization_id == str(organization.id),
                Connection.type == "mcp",
            )
        )
        mcp_connections = list({str(c.id): c for c in conn_result.scalars().all()}.values())

        connection = select_mcp_connection(mcp_connections, data.connection_id)
        if isinstance(connection, str):
            # select_mcp_connection returns an error message string when ambiguous/not found.
            yield self._end(False, uri=data.uri, error=connection)
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "reading", "connection_name": connection.name})

        from app.services.connection_service import ConnectionService
        try:
            client = await ConnectionService().construct_client(db, connection, current_user)
            result = await client.aread_resource(data.uri)
        except NotImplementedError:
            yield self._end(False, uri=data.uri, connection_name=connection.name,
                            error=f"Connection '{connection.name}' does not support resources.")
            return
        except BaseException as e:
            logger.error(f"read_mcp_resource: read failed for {data.uri}: {e}")
            yield self._end(False, uri=data.uri, connection_name=connection.name, error=str(e))
            return

        if not result.get("success"):
            # Bad/unknown URI etc. — surface as a tool error, not a crash.
            yield self._end(False, uri=data.uri, connection_name=connection.name,
                            error=result.get("error") or "Failed to read resource.")
            return

        contents = result.get("contents") or []
        content, mime_type, truncated = format_resource_contents(contents)

        # Binary resources (xlsx, pdf, exported Google Sheet/Doc, …) carry their
        # bytes as base64; materialize them into a session File so the analysis
        # stack (inspect_data / create_data / read_excel_as_csv) can use them,
        # exactly like an uploaded file. Returns a session_file_id the agent can
        # pass downstream. Non-attachable binaries are left as a summary only.
        session_file_id = None
        import base64
        from ._file_tool_common import attach_drive_file_to_session, ext_for_mime
        for c in contents:
            if c.get("type") != "binary" or not c.get("blob_b64"):
                continue
            cmime = c.get("mime_type")
            if not ext_for_mime(cmime):
                continue  # not something the analysis tools understand
            try:
                raw = base64.b64decode(c["blob_b64"], validate=False)
            except Exception as e:
                logger.warning(f"read_mcp_resource: blob decode failed for {data.uri}: {e}")
                continue
            name = (str(c.get("uri") or data.uri).rstrip("/").split("/")[-1]) or "resource"
            session_file_id = await attach_drive_file_to_session(
                runtime_ctx, filename=name, content_bytes=raw, mime_type=cmime,
            )
            if session_file_id:
                break  # one materialized file per read is enough for analysis

        # Audit — record metadata only; NEVER the content (it can carry sensitive data).
        await log_tool_audit(
            runtime_ctx,
            action="tool.mcp_resource_read",
            resource_type="report",
            resource_id=str(report.id) if report else None,
            details={
                "tool": "read_mcp_resource",
                "connection_id": str(connection.id),
                "uri": data.uri,
                "content_length": len(content),
                "truncated": truncated,
            },
        )

        yield self._end(
            True, uri=data.uri, connection_name=connection.name,
            content=content, mime_type=mime_type, truncated=truncated,
            session_file_id=session_file_id,
        )

    @staticmethod
    def _end(success: bool, *, uri=None, connection_name=None, content=None, mime_type=None,
             truncated=False, error=None, session_file_id=None) -> ToolEndEvent:
        output = {
            "success": success,
            "content": content,
            "mime_type": mime_type,
            "uri": uri,
            "connection_name": connection_name,
            "truncated": truncated,
            "error_message": error,
            "session_file_id": session_file_id,
        }
        if success:
            summary = f"Read resource {uri}"
            if connection_name:
                summary += f" from {connection_name}"
            summary += f" ({len(content or '')} chars{', truncated' if truncated else ''})."
            if session_file_id:
                summary += f" Materialized as session file {session_file_id} — pass it to inspect_data / create_data to analyze."
            observation = {"summary": summary, "content": content, "uri": uri, "success": True}
        else:
            observation = {"summary": f"Failed to read resource {uri or ''}: {error}", "success": False}
        return ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})
