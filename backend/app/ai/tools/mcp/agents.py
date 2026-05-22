"""MCP tools: agent catalog (list / get / create).

External clients (Claude Code, Cursor, etc.) hit these via the MCP
JSON-RPC endpoint at ``/api/mcp``. Each tool is a thin wrapper around
``AgentCatalogService`` (reads) or ``AgentYamlService.apply`` (write).

Permission gating:
- ``list_agents`` / ``get_agent`` — no explicit gate; service-side
  visibility filter does the work (a member only sees agents they
  have a grant on or that are public).
- ``create_agent`` — ``required_org_permission='create_data_source'``
  hides the write tool from non-admins entirely. The in-body call to
  ``AgentYamlService.apply`` is the actual enforcement: it returns
  ``permission_denied`` in the ApplyResult envelope when the caller
  lacks ``create_data_source`` (create path) or ``manage`` on the
  target agent (update path).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.ai.tools.schemas.create_agent import (
    CreateAgentInput,
    CreateAgentOutput,
)
from app.ai.tools.schemas.get_agent import GetAgentInput, GetAgentOutput
from app.ai.tools.schemas.list_agents import (
    ListAgentsInput,
    ListAgentsOutput,
)
from app.models.organization import Organization
from app.models.user import User


logger = logging.getLogger(__name__)


class ListAgentsMCPTool(MCPTool):
    """List agents in the organization that the caller can see."""

    name = "list_agents"
    description = (
        "List agents (data sources) in this organization that the caller can see. "
        "Returns id, name, description, primary connection type, and counts per agent. "
        "Use this to discover available agents before fetching one with get_agent or "
        "creating a new one. Filters: name_search (substring), type (e.g. 'postgresql', "
        "'mcp'), page / page_size."
    )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return ListAgentsInput.model_json_schema()

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        from app.services.agent_catalog_service import AgentCatalogService

        try:
            data = ListAgentsInput(**args)
        except Exception as e:
            return ListAgentsOutput(
                success=False,
                error_message=f"Invalid input: {e}",
            ).model_dump()

        result = await AgentCatalogService().list_agents(
            db,
            organization,
            user,
            name_search=data.name_search,
            type_filter=data.type,
            page=data.page,
            page_size=data.page_size,
        )
        return result.model_dump()


class GetAgentMCPTool(MCPTool):
    """Fetch full structured detail for one agent by name."""

    name = "get_agent"
    description = (
        "Fetch full detail for one agent by name. Returns connections, top-N active tables "
        "(default 50, ranked by relevance), per-agent tool overlay, conversation starters, "
        "and members. Use this after list_agents to inspect a candidate agent before "
        "editing via create_agent. The caller must have view permission on the agent "
        "(public agents are always visible to org members)."
    )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return GetAgentInput.model_json_schema()

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        from app.services.agent_catalog_service import AgentCatalogService

        try:
            data = GetAgentInput(**args)
        except Exception as e:
            return GetAgentOutput(
                success=False, error_message=f"Invalid input: {e}"
            ).model_dump()

        result = await AgentCatalogService().get_agent(
            db,
            organization,
            user,
            name=data.name,
            table_limit=data.table_limit,
        )
        return result.model_dump()


class CreateAgentMCPTool(MCPTool):
    """Create or update an agent from structured input (upsert)."""

    name = "create_agent"
    description = (
        "Create or update an agent (data source) from structured input. Upsert semantics: "
        "if an agent with the given name exists and you have manage permission, it's "
        "updated; otherwise it's created (requires create_data_source on the org). "
        "Defaults to private (is_public=false). "
        "\n\nList tables explicitly in tables_include. Call get_connection first to "
        "discover what's on a connection, then curate the list. Calls with no table filter "
        "are refused (code=tables_unconfirmed) unless confirm_empty_tables=true — to "
        "prevent accidentally opening every table on every connection. "
        "\n\nReturns a structured envelope: status (created/updated/unchanged/dry_run/"
        "error), diff, warnings, and errors with did-you-mean suggestions for typo'd "
        "connection/group/user/tool names. Use dry_run=true to validate without writing."
    )

    @property
    def required_org_permission(self) -> Optional[str]:
        # Hides the tool from tools/list for non-admins. AgentYamlService.apply
        # still does the per-call check (create_data_source for create,
        # manage on the agent for update).
        return "create_data_source"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return CreateAgentInput.model_json_schema()

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        try:
            data = CreateAgentInput(**args)
        except Exception as e:
            return CreateAgentOutput(
                success=False,
                status="error",
                errors=[
                    {
                        "loc": [],
                        "code": "schema_invalid",
                        "message": str(e),
                    }
                ],
                message=f"Invalid input: {e}",
            ).model_dump()

        # Empty-tables guardrail — shared with the planner version.
        if (
            (data.tables_include is None or len(data.tables_include) == 0)
            and not data.confirm_empty_tables
            and data.connection_names
            and not data.dry_run
        ):
            try:
                from app.services.agent_catalog_service import AgentCatalogService

                indexed = await AgentCatalogService().count_indexed_tables_for_connections(
                    db, organization, connection_names=data.connection_names
                )
                if indexed > 0:
                    msg = (
                        f"Refusing to create '{data.name}' with no table filter — "
                        f"the linked connection(s) have {indexed} indexed table(s). "
                        "Call get_connection to list them and pass tables_include "
                        "explicitly, or pass confirm_empty_tables=true after "
                        "confirming with the user."
                    )
                    return CreateAgentOutput(
                        success=False,
                        status="error",
                        name=data.name,
                        errors=[
                            {
                                "loc": ["tables_include"],
                                "code": "tables_unconfirmed",
                                "message": msg,
                                "value": None,
                                "suggestion": (
                                    "set tables_include explicitly OR pass "
                                    "confirm_empty_tables=true after confirming with the user"
                                ),
                            }
                        ],
                        message=msg,
                    ).model_dump()
            except Exception:
                logger.exception("create_agent (mcp): empty-tables probe failed")

        try:
            from app.schemas.agent_manifest_schema import (
                AgentManifest,
                MemberRef,
                TableRules,
                ToolsOverlay,
            )
            from app.services.agent_yaml_service import AgentYamlService
            import yaml as _yaml

            tables_rules = None
            if (
                data.tables_include is not None
                or (data.tables_exclude and len(data.tables_exclude) > 0)
            ):
                tables_rules = TableRules(
                    include=data.tables_include,
                    exclude=list(data.tables_exclude or []),
                )
            tools_map = {
                tp.connection_name: ToolsOverlay(
                    allow=list(tp.allow),
                    confirm=list(tp.confirm),
                    deny=list(tp.deny),
                )
                for tp in data.tool_policies
            }
            members = [
                MemberRef(
                    user=m.user,
                    group=m.group,
                    permissions=list(m.permissions) if m.permissions else None,
                )
                for m in data.members
            ]
            manifest = AgentManifest(
                name=data.name,
                description=data.description,
                context=data.context,
                is_public=data.is_public,
                use_llm_sync=data.use_llm_sync,
                connections=list(data.connection_names),
                tables=tables_rules,
                tools=tools_map,
                conversation_starters=list(data.conversation_starters),
                members=members,
            )
            yaml_text = _yaml.safe_dump(
                manifest.model_dump(mode="json", exclude_none=False),
                sort_keys=False,
                allow_unicode=True,
            )

            result = await AgentYamlService().apply(
                db, organization, user, yaml_text, dry_run=data.dry_run
            )
            status_str = (
                result.status.value if hasattr(result.status, "value") else str(result.status)
            )
            success = status_str in ("created", "updated", "unchanged", "dry_run")

            return CreateAgentOutput(
                success=success,
                status=status_str,
                id=result.id,
                name=result.name or data.name,
                diff=result.diff,
                warnings=[w.model_dump(mode="json") for w in (result.warnings or [])],
                errors=[e.model_dump(mode="json") for e in (result.errors or [])],
                message=(
                    f"Agent '{result.name or data.name}' {status_str}."
                    if success
                    else f"Agent '{data.name}' rejected ({len(result.errors or [])} error(s))."
                ),
            ).model_dump()
        except Exception as e:
            logger.exception(f"create_agent (mcp) failed: {e}")
            return CreateAgentOutput(
                success=False,
                status="error",
                name=data.name,
                errors=[
                    {"loc": [], "code": "execution_failed", "message": str(e)}
                ],
                message=f"Internal error: {e}",
            ).model_dump()
