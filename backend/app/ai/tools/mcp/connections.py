"""MCP tools: connection catalog (list / get).

Thin wrappers over ``AgentCatalogService``. ``get_connection`` requires
``manage_connections`` (org level) since it exposes raw connection
metadata; ``list_connections`` is visibility-filtered inside.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.ai.tools.schemas.get_connection import (
    GetConnectionInput,
    GetConnectionOutput,
)
from app.ai.tools.schemas.list_connections import (
    ListConnectionsInput,
    ListConnectionsOutput,
)
from app.models.organization import Organization
from app.models.user import User


logger = logging.getLogger(__name__)


class ListConnectionsMCPTool(MCPTool):
    """List database / MCP / custom-API connections visible to the caller."""

    name = "list_connections"
    description = (
        "List database, MCP, and custom-API connections in this organization that the "
        "caller can see. Includes which agents already use each connection. Useful "
        "before creating or editing an agent. Filter by type, name_search (substring), "
        "or only_tool_providers (restricts to mcp + custom_api)."
    )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return ListConnectionsInput.model_json_schema()

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        from app.services.agent_catalog_service import AgentCatalogService

        try:
            data = ListConnectionsInput(**args)
        except Exception as e:
            return ListConnectionsOutput(
                success=False, error_message=f"Invalid input: {e}"
            ).model_dump()

        result = await AgentCatalogService().list_connections(
            db,
            organization,
            user,
            name_search=data.name_search,
            type_filter=data.type,
            only_tool_providers=data.only_tool_providers,
            page=data.page,
            page_size=data.page_size,
        )
        return result.model_dump()


class GetConnectionMCPTool(MCPTool):
    """Connection detail: indexed tables, tools, agents using it, config preview."""

    name = "get_connection"
    description = (
        "Fetch full detail for one connection by name. Returns config (credentials "
        "stripped), indexed table list (top-N, optional column previews), tools (for "
        "mcp/custom_api connections), and the agents using it. Requires "
        "manage_connections permission."
    )

    @property
    def required_org_permission(self) -> Optional[str]:
        return "manage_connections"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return GetConnectionInput.model_json_schema()

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        from app.services.agent_catalog_service import AgentCatalogService

        try:
            data = GetConnectionInput(**args)
        except Exception as e:
            return GetConnectionOutput(
                success=False, error_message=f"Invalid input: {e}"
            ).model_dump()

        result = await AgentCatalogService().get_connection(
            db,
            organization,
            user,
            name=data.name,
            table_search=data.table_search,
            table_limit=data.table_limit,
            with_columns=data.with_columns,
            with_tools=data.with_tools,
            enforce_manage_connections=True,
        )
        return result.model_dump()
