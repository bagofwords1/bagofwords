"""create_agent — training-mode upsert.

Builds an ``AgentManifest`` from structured planner input and delegates
to ``AgentYamlService.apply``. Upsert semantics: existing agent with the
same name + caller has ``manage`` → update; otherwise → create (requires
org ``create_data_source``). All license / RBAC / structured-error
handling is reused from the YAML path.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.create_agent import (
    CreateAgentInput,
    CreateAgentOutput,
)
from app.ai.tools.schemas.events import (
    ToolEndEvent,
    ToolErrorEvent,
    ToolEvent,
    ToolStartEvent,
)


logger = logging.getLogger(__name__)


class CreateAgentTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_agent",
            description=(
                "Create or update an agent (data source) from structured input. Upsert "
                "semantics: if an agent with the given name exists and you have manage "
                "permission, it's updated; otherwise it's created (requires create_data_source). "
                "Defaults to private (is_public=false). Returns the same structured envelope "
                "the YAML apply endpoint uses, so connection-not-found / tool-not-found "
                "errors come back with 'did-you-mean' suggestions you can act on directly. "
                "Use dry_run=true to validate without writing."
            ),
            category="action",
            version="1.0.0",
            input_schema=CreateAgentInput.model_json_schema(),
            output_schema=CreateAgentOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=60,
            idempotent=True,  # re-call with identical input → unchanged
            required_permissions=["create_data_source"],
            tags=["agent", "create", "training"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {
                        "name": "revenue-analyst",
                        "description": "Helps GTM analyze pipeline and ARR",
                        "connection_names": ["postgres-prod"],
                        "conversation_starters": ["Q3 pipeline coverage", "Top churned accounts"],
                    },
                    "description": "Minimal private agent on one Postgres connection.",
                },
                {
                    "input": {
                        "name": "support-analyst",
                        "connection_names": ["postgres-prod", "hubspot-mcp"],
                        "tables_include": ["postgres-prod.public.tickets"],
                        "tool_policies": [
                            {
                                "connection_name": "hubspot-mcp",
                                "allow": ["search_contacts", "get_deal"],
                                "confirm": ["post_message"],
                            }
                        ],
                        "members": [
                            {"group": "support-team", "permissions": ["manage"]}
                        ],
                    },
                    "description": "Agent with table filter, MCP tool overlay, and group member.",
                },
                {
                    "input": {"name": "revenue-analyst", "dry_run": True},
                    "description": "Dry-run to see if the agent exists and what would change.",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateAgentInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateAgentOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = CreateAgentInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "name": data.name,
                "dry_run": data.dry_run,
                "connection_count": len(data.connection_names),
                "is_public": data.is_public,
            },
        )

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        if not all([db, organization, user]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": "Missing runtime context.", "code": "MISSING_CONTEXT"},
            )
            return

        try:
            from app.schemas.agent_manifest_schema import (
                AgentManifest,
                MemberRef,
                TableRules,
                ToolsOverlay,
            )
            from app.services.agent_yaml_service import AgentYamlService

            # Build the manifest from structured input.
            tables_rules = None
            if (
                data.tables_include is not None
                or (data.tables_exclude and len(data.tables_exclude) > 0)
            ):
                tables_rules = TableRules(
                    include=data.tables_include,
                    exclude=list(data.tables_exclude or []),
                )

            tools_map: Dict[str, ToolsOverlay] = {}
            for tp in data.tool_policies:
                tools_map[tp.connection_name] = ToolsOverlay(
                    allow=list(tp.allow),
                    confirm=list(tp.confirm),
                    deny=list(tp.deny),
                )

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

            # AgentYamlService.apply re-validates the manifest from raw YAML;
            # serialise + hand off so we go through exactly the same path as
            # POST /api/agents/apply.
            import yaml

            yaml_text = yaml.safe_dump(
                manifest.model_dump(mode="json", exclude_none=False),
                sort_keys=False,
                allow_unicode=True,
            )

            service = AgentYamlService()
            result = await service.apply(
                db, organization, user, yaml_text, dry_run=data.dry_run
            )

            status_str = result.status.value if hasattr(result.status, "value") else str(result.status)
            success = status_str in ("created", "updated", "unchanged", "dry_run")

            output = CreateAgentOutput(
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
            )

            if not success:
                # Surface as ToolEnd (not ToolError) so the planner sees the
                # structured errors and can self-correct. Same convention as
                # the YAML apply HTTP endpoint.
                summary = output.message or f"Agent '{data.name}' rejected."
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": output.model_dump(),
                        "observation": {
                            "summary": summary,
                            "errors": output.errors,
                        },
                    },
                )
                return

            summary = output.message or f"Agent '{result.name}' {status_str}."
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": summary,
                        "artifacts": [
                            {
                                "type": "agent",
                                "id": result.id,
                                "name": result.name,
                                "status": status_str,
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"create_agent failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": str(e), "code": "EXECUTION_FAILED"},
            )
