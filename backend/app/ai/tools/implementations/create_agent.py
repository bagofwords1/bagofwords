"""Create Agent Tool - Create a new agent (data source) on existing connections (training mode).

One-prompt agent building: create the agent, optionally select its active
tables (schemas / table globs) or enabled tools (tool globs) in the same call,
and attach it to the current training session so instruction/eval/prompt tools
immediately scope to it.

Safety model:
- Link-existing-connections ONLY. No credentials, config, or connection
  creation ever flow through this tool (Mode 1 of create_data_source is not
  exposed).
- Permission pair enforced in-tool (route decorators don't cover the AI tool
  path): org `create_data_source` + per-connection `create_data_sources` —
  exactly what POST /data_sources requires.
- Selection only ever targets the agent this call just created.
"""

from typing import AsyncIterator, Dict, Any, List, Optional, Type
from pydantic import BaseModel
import logging

from fastapi import HTTPException

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.create_agent import CreateAgentInput, CreateAgentOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.ai.tools.implementations.connection_catalog_common import (
    FILE_SOURCE_TYPES,
    can_create_agents,
    can_create_on_connection,
    compile_patterns,
    conn_data_shape,
    table_schema_of,
    table_selection_groups,
    tool_selection_groups,
)

logger = logging.getLogger(__name__)

ACTIVE_SAMPLE_LIMIT = 20
# Above these catalog sizes an unselected create is rejected with a coverage
# menu (needs_selection) instead of silently producing a near-empty agent —
# the auto-select cap for agents on existing connections is 0.
TABLES_MENU_THRESHOLD = 25
TOOLS_MENU_THRESHOLD = 15


class CreateAgentTool(Tool):
    """Create an agent on existing connection(s) with optional inline table/tool selection."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_agent",
            description=(
                "ACTION: Create a NEW agent (data source) on one or more EXISTING connections "
                "and attach it to this training session. Optionally select its catalog in the "
                "same call: `schemas` (activate whole database schemas) and/or `tables` "
                "(names or globs like 'sales.*') for table connections, or `tools` (names or "
                "globs) for MCP/API connections — anything else stays inactive/disabled. "
                "Run list_connections / get_connection FIRST to pick the connection and "
                "selection. On a large catalog with NO selection this rejects with "
                "`needs_selection` + coverage groups — ask the user via clarify (clickable "
                "options) and retry; pass use_defaults=true only when they explicitly choose "
                "'everything'. Cannot create connections or accept credentials. Requires "
                "create-agent access on every referenced connection. Querying the new agent's "
                "data starts on the NEXT message; instructions/prompts scope to it immediately."
            ),
            category="action",
            version="1.0.0",
            input_schema=CreateAgentInput.model_json_schema(),
            output_schema=CreateAgentOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=60,
            idempotent=False,
            required_permissions=["create_data_source"],
            tags=["training", "connection", "agent-building"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {
                        "name": "Sales Analytics",
                        "connection_ids": ["<uuid>"],
                        "description": "Revenue and orders reporting on the warehouse.",
                        "schemas": ["sales"],
                    },
                    "description": "Agent on a warehouse connection with only the sales schema active.",
                },
                {
                    "input": {
                        "name": "Monday Reader",
                        "connection_ids": ["<uuid>"],
                        "tools": ["get_*", "list_*", "search_*"],
                    },
                    "description": "MCP agent with only the read tools enabled.",
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
            data = CreateAgentInput(**(tool_input or {}))
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
                "connection_ids": data.connection_ids,
                "schemas": data.schemas,
                "tables": data.tables,
                "tools": data.tools,
                "is_public": data.is_public,
            },
        )

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        report = runtime_ctx.get("report")

        if not all([db, organization, user]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": "Missing required runtime context (db, organization, user)",
                    "code": "MISSING_CONTEXT",
                },
            )
            return

        try:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            from app.core.permission_resolver import resolve_permissions
            from app.models.connection import Connection
            from app.models.connection_tool import ConnectionTool
            from app.models.data_source import DataSource
            from app.models.data_source_connection_tool import DataSourceConnectionTool
            from app.models.datasource_table import DataSourceTable
            from app.schemas.data_source_schema import DataSourceCreate
            from app.services.data_source_service import DataSourceService

            # ---- Permission pair (mirrors POST /data_sources) ----
            resolved = await resolve_permissions(db, str(user.id), str(organization.id))
            if not can_create_agents(resolved):
                yield self._reject(
                    "You don't hold the create_data_source permission required to create agents.",
                    "permission_denied",
                )
                return

            conn_q = await db.execute(
                select(Connection).where(
                    Connection.id.in_([str(c) for c in data.connection_ids]),
                    Connection.organization_id == str(organization.id),
                )
            )
            conns_by_id = {str(c.id): c for c in conn_q.scalars().all()}
            for cid in data.connection_ids:
                conn = conns_by_id.get(str(cid))
                if conn is None:
                    yield self._reject(
                        f"Connection {cid} was not found in this organization.",
                        "connection_not_found",
                    )
                    return
                if not can_create_on_connection(resolved, str(cid)):
                    yield self._reject(
                        f"You don't have create-agent access on connection '{conn.name}'.",
                        "permission_denied",
                    )
                    return

            # ---- Interview guardrail: no silent near-empty agents ----
            # On a large catalog with no selection (and no explicit
            # use_defaults), reject with a coverage menu the planner can turn
            # into a clickable clarify question. Nothing is created here.
            if not data.use_defaults:
                conns = [conns_by_id[str(c)] for c in data.connection_ids]
                tables_conn_ids = [
                    str(c.id) for c in conns
                    if conn_data_shape(c.type) in ("tables", "objects") and c.type not in FILE_SOURCE_TYPES
                ]
                tools_conn_ids = [str(c.id) for c in conns if conn_data_shape(c.type) == "tools"]

                menu: List[dict] = []
                if tables_conn_ids and not (data.schemas or data.tables):
                    from app.models.connection_table import ConnectionTable
                    from sqlalchemy import func as _f
                    n = (await db.execute(
                        select(_f.count(ConnectionTable.id)).where(
                            ConnectionTable.connection_id.in_(tables_conn_ids)
                        )
                    )).scalar() or 0
                    if n > TABLES_MENU_THRESHOLD:
                        menu.extend(await table_selection_groups(db, tables_conn_ids))
                if tools_conn_ids and not data.tools:
                    from app.models.connection_tool import ConnectionTool as _CT
                    from sqlalchemy import func as _f2
                    n = (await db.execute(
                        select(_f2.count(_CT.id)).where(_CT.connection_id.in_(tools_conn_ids))
                    )).scalar() or 0
                    if n > TOOLS_MENU_THRESHOLD:
                        menu.extend(await tool_selection_groups(db, tools_conn_ids))

                if menu:
                    menu_txt = ", ".join(f"{g['label']} ({g['count']})" for g in menu)
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": CreateAgentOutput(
                                success=False,
                                name=data.name,
                                message=(
                                    "This connection has a large catalog and no selection was given. "
                                    f"Coverage groups: {menu_txt}. Ask the user which to include "
                                    "(clarify with these as clickable options plus 'Everything'), "
                                    "then retry with schemas/tables/tools — or use_defaults=true if "
                                    "they choose everything."
                                ),
                                rejected_reason="needs_selection",
                                selection_groups=menu,
                            ).model_dump(),
                            "observation": {
                                "summary": (
                                    f"create_agent needs a selection for '{data.name}': offer the user "
                                    f"these coverage groups via clarify (clickable options): {menu_txt}, "
                                    "plus 'Everything'. Then retry create_agent with their choice."
                                ),
                                "artifacts": [
                                    {"type": "agent_selection_menu", "groups": menu, "name": data.name}
                                ],
                            },
                        },
                    )
                    return

            # ---- Create (link-existing mode ONLY — no credentials path) ----
            service = DataSourceService()
            payload = DataSourceCreate(
                name=data.name.strip(),
                connection_ids=[str(c) for c in data.connection_ids],
                is_public=bool(data.is_public),
                use_llm_sync=False,
            )
            try:
                created = await service.create_data_source(db, organization, user, payload)
            except HTTPException as he:
                reason = {403: "permission_denied", 404: "connection_not_found",
                          409: "name_taken", 402: "limit_reached"}.get(he.status_code, "rejected")
                yield self._reject(str(he.detail), reason)
                return

            # The service granted the creator `manage` on the new agent; drop the
            # per-session RBAC memo so later tool calls in this run (e.g.
            # create_instruction) resolve against the fresh grant.
            from app.core.permission_resolver import invalidate_rbac_memo
            invalidate_rbac_memo(db, str(user.id), str(organization.id))

            ds_q = await db.execute(
                select(DataSource)
                .options(selectinload(DataSource.connections))
                .where(DataSource.id == str(created.id))
            )
            ds = ds_q.scalar_one()

            if data.description and data.description.strip():
                ds.description = data.description.strip()
                db.add(ds)
                await db.commit()

            unresolved: List[str] = []

            # ---- Inline table selection ----
            t_rows = (
                await db.execute(
                    select(DataSourceTable).where(DataSourceTable.datasource_id == str(ds.id))
                )
            ).scalars().all()

            wants_table_selection = bool(data.schemas) or bool(data.tables)
            if wants_table_selection and t_rows:
                wanted_schemas = {s.strip().lower() for s in (data.schemas or []) if s and s.strip()}
                compiled = compile_patterns(data.tables)

                def _candidates(row) -> List[str]:
                    name = row.name or ""
                    cands = [name]
                    if "." in name:
                        cands.append(name.rsplit(".", 1)[-1])
                    schema = table_schema_of(row)
                    if schema and "." not in name:
                        cands.append(f"{schema}.{name}")
                    return cands

                matched_ids, matched_names = set(), []
                hit_schemas, hit_patterns = set(), set()
                for row in t_rows:
                    schema = (table_schema_of(row) or "").lower()
                    row_hit = False
                    if schema and schema in wanted_schemas:
                        hit_schemas.add(schema)
                        row_hit = True
                    cands = _candidates(row)
                    for original, rx in compiled:
                        if any(rx.match(c) for c in cands):
                            hit_patterns.add(original)
                            row_hit = True
                    if row_hit:
                        matched_ids.add(str(row.id))
                        matched_names.append(row.name)

                unresolved.extend(sorted(s for s in wanted_schemas if s not in hit_schemas))
                unresolved.extend(p for p, _ in compiled if p not in hit_patterns)

                if matched_ids:
                    deactivate_ids = [str(r.id) for r in t_rows if str(r.id) not in matched_ids]
                    await service.update_tables_status_delta(
                        db,
                        str(ds.id),
                        organization,
                        activate=sorted(matched_ids),
                        deactivate=deactivate_ids,
                        current_user=user,
                    )
                # No matches at all → keep the seeded defaults instead of
                # leaving the agent with zero tables; unresolved says why.
            elif wants_table_selection and not t_rows:
                unresolved.extend([s for s in (data.schemas or [])])
                unresolved.extend([t for t in (data.tables or [])])

            # ---- Inline tool selection (per-agent overlay) ----
            conn_ids = [str(c.id) for c in (ds.connections or [])]
            all_tools = []
            if conn_ids:
                all_tools = (
                    await db.execute(
                        select(ConnectionTool).where(ConnectionTool.connection_id.in_(conn_ids))
                    )
                ).scalars().all()

            if data.tools:
                compiled_tools = compile_patterns(data.tools)
                matched_tool_ids, hit_tool_patterns = set(), set()
                for t in all_tools:
                    for original, rx in compiled_tools:
                        if rx.match(t.name or ""):
                            hit_tool_patterns.add(original)
                            matched_tool_ids.add(str(t.id))
                unresolved.extend(p for p, _ in compiled_tools if p not in hit_tool_patterns)

                if matched_tool_ids:
                    for t in all_tools:
                        enabled = str(t.id) in matched_tool_ids
                        db.add(
                            DataSourceConnectionTool(
                                data_source_id=str(ds.id),
                                connection_tool_id=str(t.id),
                                is_enabled=enabled,
                                policy=t.policy,
                            )
                        )
                    await db.commit()

            # ---- Attach to the current training session's report ----
            attached = False
            if report is not None:
                try:
                    existing_ids = {str(d.id) for d in (report.data_sources or [])}
                    if str(ds.id) not in existing_ids:
                        report.data_sources.append(ds)
                        db.add(report)
                        await db.commit()
                    attached = True
                except Exception:
                    logger.exception("create_agent: failed to attach the new agent to the report")
                    await db.rollback()

            # ---- Final state for the observation/card ----
            t_rows = (
                await db.execute(
                    select(DataSourceTable).where(DataSourceTable.datasource_id == str(ds.id))
                )
            ).scalars().all()
            active_rows = [r for r in t_rows if getattr(r, "is_active", False)]

            overlay_rows = (
                await db.execute(
                    select(DataSourceConnectionTool).where(
                        DataSourceConnectionTool.data_source_id == str(ds.id)
                    )
                )
            ).scalars().all()
            overlay_by_tool = {str(o.connection_tool_id): o for o in overlay_rows}
            tools_enabled = sum(
                1
                for t in all_tools
                if (
                    overlay_by_tool[str(t.id)].is_enabled
                    if str(t.id) in overlay_by_tool
                    else bool(t.is_enabled)
                )
            )

            requires_user_connect = any(
                getattr(c, "auth_policy", None) == "user_required" for c in (ds.connections or [])
            )

            message = (
                f"Agent '{ds.name}' created on {len(conn_ids)} connection(s): "
                f"{len(active_rows)}/{len(t_rows)} tables active, "
                f"{tools_enabled}/{len(all_tools)} tools enabled."
            )
            if unresolved:
                message += f" Unmatched selection: {', '.join(unresolved)}."
            if attached:
                message += " Attached to this session — instructions/prompts scope to it now; querying its data starts next message."
            if requires_user_connect:
                message += " Connection requires per-user sign-in: each user must Connect before tools run."

            output = CreateAgentOutput(
                success=True,
                data_source_id=str(ds.id),
                name=ds.name,
                description=ds.description,
                is_public=bool(ds.is_public),
                connections=[c.name for c in (ds.connections or [])],
                tables_total=len(t_rows),
                tables_active=len(active_rows),
                active_table_sample=[r.name for r in active_rows[:ACTIVE_SAMPLE_LIMIT]],
                tools_total=len(all_tools),
                tools_enabled=tools_enabled,
                unresolved=unresolved,
                attached_to_report=attached,
                requires_user_connect=requires_user_connect,
                message=message,
            )

            logger.info(
                f"Training mode created agent {ds.id} '{ds.name}' "
                f"(connections={conn_ids}, tables={len(active_rows)}/{len(t_rows)}, "
                f"tools={tools_enabled}/{len(all_tools)}, attached={attached})"
            )

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": message,
                        "artifacts": [
                            {
                                "type": "agent",
                                "id": str(ds.id),
                                "name": ds.name,
                                "description": ds.description,
                                "is_public": bool(ds.is_public),
                                "connections": output.connections,
                                "tables_total": output.tables_total,
                                "tables_active": output.tables_active,
                                "active_table_sample": output.active_table_sample,
                                "tools_total": output.tools_total,
                                "tools_enabled": output.tools_enabled,
                                "unresolved": unresolved,
                                "attached_to_report": attached,
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"create_agent failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Failed to create agent: {e}", "code": "CREATE_FAILED"},
            )

    @staticmethod
    def _reject(message: str, reason: str) -> ToolEndEvent:
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": CreateAgentOutput(
                    success=False, message=message, rejected_reason=reason
                ).model_dump(),
                "observation": {"summary": f"Agent creation rejected: {message}", "artifacts": []},
            },
        )
