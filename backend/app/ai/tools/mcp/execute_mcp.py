"""MCP Tool: execute_mcp — gateway to an agent's MCP / custom-API tools.

External twin of the internal ``execute_mcp`` planner tool. Lets an external MCP
client (Claude, Cursor) trigger a tool exposed by one of an agent's ``mcp`` /
``custom_api`` connections. BOW acts as the gateway: it resolves the connection,
enforces the per-agent tool policy, calls the provider, and returns the result
inline.
"""

import logging
from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.models.organization import Organization
from app.models.user import User
from app.schemas.mcp import MCPExecuteToolInput, MCPExecuteToolOutput
from app.services.connection_tool_gateway import ConnectionToolGateway

logger = logging.getLogger(__name__)

_PREVIEW_ROWS = 25
_TEXT_PREVIEW_CHARS = 8000


class ExecuteMCPMCPTool(MCPTool):
    """Run a tool exposed by an agent's MCP server or custom API.

    Use list_agent_tools (or get_context) first to discover available tools and
    their input schemas. Only tools whose policy is 'allow' can be invoked here.
    """

    name = "execute_mcp"
    description = (
        "Execute a tool exposed by an agent's connected MCP server or custom API "
        "(Notion, Jira, Datadog, internal REST APIs, etc.). Provide the agent's "
        "data_source_id, the tool_name, and arguments matching its input schema "
        "(fetch it via list_agent_tools first). Returns the tool's output inline. "
        "Use create_data/inspect_data for SQL databases — this is for non-SQL tools."
    )

    async def _enable_mcp_tools(self, db: AsyncSession, organization: Organization) -> bool:
        """Respect the org-level ``enable_mcp_tools`` kill switch (default on)."""
        try:
            org_settings = await organization.get_settings(db)
            cfg = org_settings.get_config("enable_mcp_tools")
            return bool(cfg.value) if cfg is not None else True
        except Exception:
            return True

    async def _user_can_access(
        self, db: AsyncSession, user: User, organization: Organization, data_source_id: str
    ) -> bool:
        from sqlalchemy import select
        from app.models.data_source import DataSource
        from app.services.data_source_service import DataSourceService

        ds = (
            await db.execute(
                select(DataSource).where(
                    DataSource.id == str(data_source_id),
                    DataSource.organization_id == str(organization.id),
                )
            )
        ).scalars().first()
        if not ds:
            return False
        visible = await DataSourceService().filter_user_visible_data_sources(
            db, [ds], user, organization
        )
        return len(visible) > 0

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPExecuteToolInput.model_json_schema()

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        input_data = MCPExecuteToolInput(**args)

        if not await self._enable_mcp_tools(db, organization):
            return MCPExecuteToolOutput(
                success=False,
                error_message="MCP tools are disabled for this organization.",
            ).model_dump()

        if not await self._user_can_access(db, user, organization, input_data.data_source_id):
            return MCPExecuteToolOutput(
                success=False,
                error_message=f"Agent '{input_data.data_source_id}' not found or not accessible.",
            ).model_dump()

        result = await ConnectionToolGateway().execute(
            db,
            organization,
            data_source_id=input_data.data_source_id,
            tool_name=input_data.tool_name,
            arguments=input_data.arguments,
            connection_id=input_data.connection_id,
            current_user=user,
        )

        if not result.success:
            return MCPExecuteToolOutput(
                success=False,
                content_type=result.content_type,
                connection_name=result.connection_name,
                error_message=result.error,
                input_schema=result.input_schema,
            ).model_dump()

        # Shape the result for inline return: cap tabular rows / text length so a
        # large payload doesn't blow up the MCP response. The full payload is the
        # tool's; clients that need everything can narrow their arguments.
        content_type = result.content_type
        data = result.data
        row_count = None
        truncated = False
        preview: Any = data

        if content_type == "tabular" and isinstance(data, list):
            row_count = len(data)
            if row_count > _PREVIEW_ROWS:
                preview = data[:_PREVIEW_ROWS]
                truncated = True
        elif content_type == "text" and isinstance(data, str):
            if len(data) > _TEXT_PREVIEW_CHARS:
                preview = data[:_TEXT_PREVIEW_CHARS]
                truncated = True
        else:
            import json
            try:
                blob = json.dumps(data, default=str)
                if len(blob) > _TEXT_PREVIEW_CHARS:
                    preview = blob[:_TEXT_PREVIEW_CHARS]
                    truncated = True
            except Exception:
                preview = str(data)[:_TEXT_PREVIEW_CHARS]
                truncated = True

        return MCPExecuteToolOutput(
            success=True,
            content_type=content_type,
            connection_name=result.connection_name,
            row_count=row_count,
            result=preview,
            truncated=truncated,
        ).model_dump()
