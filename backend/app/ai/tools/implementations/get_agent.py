"""get_agent — training-mode agent detail.

Returns a structured ``AgentDetail`` for a single agent by name:
description, connections, top-N active tables (ranked by
``centrality_score`` desc, then name asc), per-agent tool overlay,
conversation starters, and members. Instructions are intentionally
omitted (separate entity).
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Type

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import (
    ToolEndEvent,
    ToolErrorEvent,
    ToolEvent,
    ToolStartEvent,
)
from app.ai.tools.schemas.get_agent import (
    AgentDetail,
    ConnectionRef,
    GetAgentInput,
    GetAgentOutput,
    MemberEntry,
    TableEntry,
    ToolEntry,
)


logger = logging.getLogger(__name__)


class GetAgentTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="get_agent",
            description=(
                "Fetch full structured detail for one agent by name. Returns connections, "
                "top-N active tables (default 50, ranked by relevance), per-agent tool "
                "overlay (mcp/custom_api connections only), conversation starters, and "
                "members. Use this after list_agents to inspect a candidate agent before "
                "editing via create_agent. The caller must have view permission on the "
                "agent (public agents are always visible to org members)."
            ),
            category="research",
            version="1.0.0",
            input_schema=GetAgentInput.model_json_schema(),
            output_schema=GetAgentOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=[],
            tags=["agent", "detail", "training"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {"name": "revenue-analyst"},
                    "description": "Show full detail for the 'revenue-analyst' agent.",
                },
                {
                    "input": {"name": "revenue-analyst", "table_limit": 20},
                    "description": "Same, but only the top 20 active tables.",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return GetAgentInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return GetAgentOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = GetAgentInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={"name": data.name, "table_limit": data.table_limit},
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
            from app.core.permission_resolver import user_can_access_data_source
            from app.models.connection_table import ConnectionTable
            from app.models.data_source import DataSource
            from app.models.data_source_connection_tool import DataSourceConnectionTool
            from app.models.data_source_membership import (
                DataSourceMembership,
                PRINCIPAL_TYPE_USER,
            )
            from app.models.datasource_table import DataSourceTable
            from app.models.group import Group
            from app.models.connection_tool import ConnectionTool
            from app.models.connection import Connection
            from app.models.resource_grant import ResourceGrant
            from app.models.user import User

            ds_q = await db.execute(
                select(DataSource)
                .options(selectinload(DataSource.connections))
                .where(
                    DataSource.organization_id == organization.id,
                    DataSource.name == data.name,
                )
            )
            ds = ds_q.scalar_one_or_none()
            if ds is None:
                output = GetAgentOutput(
                    success=False, error_message=f"Agent '{data.name}' not found."
                )
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": output.model_dump(),
                        "observation": {"summary": f"Agent '{data.name}' not found."},
                    },
                )
                return

            allowed = await user_can_access_data_source(
                db, str(user.id), str(organization.id), ds, str(ds.id)
            )
            if not allowed:
                yield ToolErrorEvent(
                    type="tool.error",
                    payload={
                        "error": f"Access denied to agent '{data.name}'.",
                        "code": "FORBIDDEN",
                    },
                )
                return

            # ----- connections (with quick per-connection counts) -----
            connections_out: List[ConnectionRef] = []
            conn_table_count_by_id: Dict[str, int] = {}
            conn_tool_count_by_id: Dict[str, int] = {}
            for conn in ds.connections or []:
                ct_rows = await db.execute(
                    select(ConnectionTable.id).where(
                        ConnectionTable.connection_id == str(conn.id)
                    )
                )
                ct_ids = [r[0] for r in ct_rows.all()]
                conn_table_count_by_id[str(conn.id)] = len(ct_ids)

                tl_rows = await db.execute(
                    select(ConnectionTool.id).where(
                        ConnectionTool.connection_id == str(conn.id)
                    )
                )
                conn_tool_count_by_id[str(conn.id)] = len(tl_rows.all())

                connections_out.append(
                    ConnectionRef(
                        id=str(conn.id),
                        name=conn.name,
                        type=conn.type,
                        auth_policy=getattr(conn, "auth_policy", "system_only"),
                        is_indexed=conn_table_count_by_id[str(conn.id)] > 0,
                        is_active=bool(getattr(conn, "is_active", True)),
                        table_count=conn_table_count_by_id[str(conn.id)],
                        tool_count=conn_tool_count_by_id[str(conn.id)],
                    )
                )

            # ----- tables (top-N active, ranked by centrality desc, name asc) -----
            tables_q = await db.execute(
                select(DataSourceTable)
                .where(
                    DataSourceTable.datasource_id == str(ds.id),
                    DataSourceTable.is_active == True,  # noqa: E712
                )
            )
            all_active = list(tables_q.scalars().all())
            tables_total = len(all_active)

            # Connection table -> connection name lookup (for FQN-ish display)
            conn_by_id = {str(c.id): c for c in (ds.connections or [])}
            ct_to_conn: Dict[str, str] = {}
            if all_active:
                conn_table_ids = [
                    str(t.connection_table_id)
                    for t in all_active
                    if t.connection_table_id
                ]
                if conn_table_ids:
                    ct_rows = await db.execute(
                        select(ConnectionTable.id, ConnectionTable.connection_id).where(
                            ConnectionTable.id.in_(conn_table_ids)
                        )
                    )
                    for ct_id, conn_id in ct_rows.all():
                        c = conn_by_id.get(str(conn_id))
                        if c is not None:
                            ct_to_conn[str(ct_id)] = c.name

            def _sort_key(t: DataSourceTable):
                cs = t.centrality_score if t.centrality_score is not None else float("-inf")
                # Negate so DESC; secondary key is name ASC
                return (-cs, (t.name or "").lower())

            all_active.sort(key=_sort_key)
            top = all_active[: data.table_limit]
            table_entries: List[TableEntry] = []
            for t in top:
                cols = []
                raw_cols = t.columns or []
                if isinstance(raw_cols, list):
                    cols = [
                        {
                            "name": c.get("name") if isinstance(c, dict) else str(c),
                            "dtype": c.get("dtype") if isinstance(c, dict) else None,
                        }
                        for c in raw_cols[:8]
                    ]
                conn_name = ct_to_conn.get(str(t.connection_table_id) or "", "")
                if not conn_name and ds.connections:
                    conn_name = ds.connections[0].name
                table_entries.append(
                    TableEntry(
                        name=t.name,
                        connection_name=conn_name,
                        columns_preview=cols,
                        no_rows=t.no_rows,
                        centrality_score=t.centrality_score,
                    )
                )

            # ----- tools overlay -----
            tools_out: List[ToolEntry] = []
            if ds.connections:
                conn_ids = [str(c.id) for c in ds.connections]
                tool_q = await db.execute(
                    select(ConnectionTool, Connection)
                    .join(Connection, Connection.id == ConnectionTool.connection_id)
                    .where(ConnectionTool.connection_id.in_(conn_ids))
                )
                tool_rows = list(tool_q.all())
                overlay_q = await db.execute(
                    select(DataSourceConnectionTool).where(
                        DataSourceConnectionTool.data_source_id == str(ds.id)
                    )
                )
                overlay = {
                    str(o.connection_tool_id): o for o in overlay_q.scalars().all()
                }
                for ctool, conn in tool_rows:
                    ov = overlay.get(str(ctool.id))
                    if ov is not None:
                        tools_out.append(
                            ToolEntry(
                                connection_name=conn.name,
                                tool_name=ctool.name,
                                is_enabled=bool(ov.is_enabled),
                                policy=ov.policy,
                                has_overlay=True,
                            )
                        )
                    else:
                        tools_out.append(
                            ToolEntry(
                                connection_name=conn.name,
                                tool_name=ctool.name,
                                is_enabled=bool(getattr(ctool, "is_enabled", True)),
                                policy=getattr(ctool, "policy", "allow"),
                                has_overlay=False,
                            )
                        )

            # ----- members -----
            mem_q = await db.execute(
                select(DataSourceMembership).where(
                    DataSourceMembership.data_source_id == str(ds.id)
                )
            )
            memberships = list(mem_q.scalars().all())
            grants_q = await db.execute(
                select(ResourceGrant).where(
                    ResourceGrant.resource_type == "data_source",
                    ResourceGrant.resource_id == str(ds.id),
                    ResourceGrant.deleted_at.is_(None),
                )
            )
            grants = {
                (g.principal_type, g.principal_id): list(g.permissions or [])
                for g in grants_q.scalars().all()
            }
            members_out: List[MemberEntry] = []
            for m in memberships:
                perms = grants.get((m.principal_type, m.principal_id), [])
                if m.principal_type == PRINCIPAL_TYPE_USER:
                    u_row = await db.execute(
                        select(User).where(User.id == m.principal_id)
                    )
                    u = u_row.scalar_one_or_none()
                    if u is None:
                        continue
                    members_out.append(
                        MemberEntry(
                            principal_type="user",
                            name_or_email=u.email,
                            permissions=perms,
                        )
                    )
                else:
                    g_row = await db.execute(
                        select(Group).where(Group.id == m.principal_id)
                    )
                    g = g_row.scalar_one_or_none()
                    if g is None:
                        continue
                    members_out.append(
                        MemberEntry(
                            principal_type="group",
                            name_or_email=g.name,
                            permissions=perms,
                        )
                    )

            detail = AgentDetail(
                id=str(ds.id),
                name=ds.name,
                description=ds.description,
                context=ds.context,
                is_public=bool(ds.is_public),
                use_llm_sync=bool(ds.use_llm_sync),
                owner_user_id=str(ds.owner_user_id) if ds.owner_user_id else None,
                created_at=(
                    ds.created_at.isoformat()
                    if getattr(ds, "created_at", None) and hasattr(ds.created_at, "isoformat")
                    else None
                ),
                updated_at=(
                    ds.updated_at.isoformat()
                    if getattr(ds, "updated_at", None) and hasattr(ds.updated_at, "isoformat")
                    else None
                ),
                connections=connections_out,
                conversation_starters=list(ds.conversation_starters or []),
                tables=table_entries,
                tables_truncated=tables_total > len(table_entries),
                tables_total=tables_total,
                tools=tools_out,
                members=members_out,
            )

            output = GetAgentOutput(success=True, agent=detail)
            summary = (
                f"Loaded agent '{ds.name}' "
                f"({len(connections_out)} conn / {tables_total} tables / "
                f"{len(tools_out)} tools / {len(members_out)} members)."
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {"summary": summary},
                },
            )
        except Exception as e:
            logger.exception(f"get_agent failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": str(e), "code": "QUERY_FAILED"},
            )
