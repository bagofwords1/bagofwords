"""MCP Tool: list_agent_tools — discover an agent's MCP / custom-API tools.

External twin of the internal ``search_mcps`` planner tool. ``get_context``
advertises tool *names*; this returns each tool's full ``input_schema`` so an
external client knows exactly how to call ``execute_mcp``.
"""

from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.models.organization import Organization
from app.models.user import User
from app.schemas.mcp import (
    AgentToolDetail,
    MCPListAgentToolsInput,
    MCPListAgentToolsOutput,
)
from app.services.connection_tool_gateway import ConnectionToolGateway


class ListAgentToolsMCPTool(MCPTool):
    """List the MCP / custom-API tools available on an agent (data source).

    Returns each tool's full input schema. Call this before ``execute_mcp`` so
    you pass the correct argument names and types. Scope with ``report_id``
    (all agents on the report) or ``data_source_ids`` (specific agents).
    """

    name = "list_agent_tools"
    description = (
        "Discover the external tools (MCP servers and custom APIs) an agent can run, "
        "with their full input schemas. Call this BEFORE execute_mcp to get exact "
        "argument names and types. Scope by report_id or data_source_ids. "
        "get_context lists tool names; this returns their schemas."
    )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPListAgentToolsInput.model_json_schema()

    async def _resolve_data_source_ids(
        self, db: AsyncSession, user: User, organization: Organization,
        input_data: MCPListAgentToolsInput,
    ) -> List[str]:
        """Resolve the agent IDs in scope, filtered to those the user may see."""
        if input_data.data_source_ids:
            from sqlalchemy import select
            from app.models.data_source import DataSource
            rows = await db.execute(
                select(DataSource).where(
                    DataSource.id.in_([str(i) for i in input_data.data_source_ids]),
                    DataSource.organization_id == str(organization.id),
                )
            )
            data_sources = list(rows.scalars().all())
        elif input_data.report_id:
            report = await self._load_report(db, input_data.report_id)
            data_sources = list(report.data_sources or [])
        else:
            return []

        # Honor per-user visibility — never advertise an agent the caller can't see.
        from app.services.data_source_service import DataSourceService
        visible = await DataSourceService().filter_user_visible_data_sources(
            db, data_sources, user, organization
        )
        return [str(ds.id) for ds in visible]

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        input_data = MCPListAgentToolsInput(**args)

        ds_ids = await self._resolve_data_source_ids(db, user, organization, input_data)
        if not ds_ids:
            return MCPListAgentToolsOutput(tools=[], total_count=0).model_dump()

        gateway_tools = await ConnectionToolGateway().list_tools(
            db, organization, data_source_ids=ds_ids
        )

        if input_data.query:
            q = input_data.query.lower()
            gateway_tools = [
                t for t in gateway_tools
                if q in t.name.lower() or q in (t.description or "").lower()
            ]

        tools = [
            AgentToolDetail(
                name=t.name,
                description=t.description,
                data_source_id=t.data_source_id,
                connection_id=t.connection_id,
                connection_name=t.connection_name,
                connection_type=t.connection_type,
                policy=t.policy,
                input_schema=t.input_schema or {},
            )
            for t in gateway_tools
        ]
        return MCPListAgentToolsOutput(tools=tools, total_count=len(tools)).model_dump()
