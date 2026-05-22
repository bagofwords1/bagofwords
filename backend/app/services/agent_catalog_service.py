"""Service layer for agent + connection catalog views.

Shared between the in-app planner tools (training mode) and the MCP
tools (external clients). Returns typed Pydantic output models — both
callers just shape the result for their transport.

Why a separate service rather than methods on ``DataSourceService`` /
``ConnectionService``: the assembly logic here is purpose-built for
the catalog UX (table ranking, column previews, member roll-up,
credentials-stripped config). The existing services are already large
and their methods serve the HTTP routes' shapes — duplicating that
work on top of them would drift. Single owner here.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai.tools.schemas.get_agent import (
    AgentDetail,
    ConnectionRef,
    GetAgentOutput,
    MemberEntry,
    TableEntry,
    ToolEntry,
)
from app.ai.tools.schemas.get_connection import (
    ColumnPreview,
    ConnectionAgentRef,
    ConnectionDetail,
    ConnectionTableEntry,
    ConnectionToolEntry,
    GetConnectionOutput,
)
from app.ai.tools.schemas.list_agents import (
    AgentSummary,
    ListAgentsOutput,
)
from app.ai.tools.schemas.list_connections import (
    ConnectionSummary,
    ListConnectionsOutput,
)
from app.core.permission_resolver import (
    FULL_ADMIN,
    resolve_permissions,
    user_can_access_data_source,
)
from app.models.connection import Connection
from app.models.connection_indexing import ConnectionIndexing
from app.models.connection_table import ConnectionTable
from app.models.connection_tool import ConnectionTool
from app.models.data_source import DataSource
from app.models.data_source_connection_tool import DataSourceConnectionTool
from app.models.data_source_membership import (
    DataSourceMembership,
    PRINCIPAL_TYPE_USER,
)
from app.models.datasource_table import DataSourceTable
from app.models.group import Group
from app.models.organization import Organization
from app.models.resource_grant import ResourceGrant
from app.models.user import User
from app.services.connection_service import ConnectionService
from app.services.data_source_service import DataSourceService


logger = logging.getLogger(__name__)

TOOL_PROVIDER_TYPES = {"mcp", "custom_api"}
_SENSITIVE_KEY_RE = re.compile(r"(pass|secret|token|key|credential|auth)", re.IGNORECASE)


def _strip_credentials(config: Any) -> Dict[str, Any]:
    """Best-effort: drop anything matching sensitive key patterns."""
    if config is None:
        return {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(config, dict):
        return {}
    return {k: v for k, v in config.items() if not _SENSITIVE_KEY_RE.search(str(k))}


class AgentCatalogService:
    """Single owner for catalog-shaped views of agents + connections."""

    def __init__(self) -> None:
        self._ds_service = DataSourceService()
        self._connection_service = ConnectionService()

    # ---------------------------------------------------------------------
    # list_agents
    # ---------------------------------------------------------------------

    async def list_agents(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
        *,
        name_search: Optional[str] = None,
        type_filter: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> ListAgentsOutput:
        items = await self._ds_service.get_data_sources(db, user, organization)

        needle = (name_search or "").strip().lower()
        type_lower = (type_filter or "").strip().lower()
        filtered = []
        for item in items:
            if needle and needle not in (item.name or "").lower():
                continue
            if type_lower and (item.type or "").lower() != type_lower:
                continue
            filtered.append(item)

        total = len(filtered)
        start = (page - 1) * page_size
        page_items = filtered[start : start + page_size]

        agents = []
        for it in page_items:
            connections = getattr(it, "connections", None) or []
            table_count = sum(
                int(getattr(c, "table_count", 0) or 0) for c in connections
            )
            agents.append(
                AgentSummary(
                    id=str(it.id),
                    name=it.name,
                    description=getattr(it, "description", None),
                    type=getattr(it, "type", None),
                    is_public=bool(getattr(it, "is_public", False)),
                    connection_count=len(connections),
                    table_count=table_count,
                    created_at=_iso(getattr(it, "created_at", None)),
                )
            )
        return ListAgentsOutput(success=True, total=total, agents=agents)

    # ---------------------------------------------------------------------
    # get_agent
    # ---------------------------------------------------------------------

    async def get_agent(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
        *,
        name: str,
        table_limit: int = 50,
    ) -> GetAgentOutput:
        ds_q = await db.execute(
            select(DataSource)
            .options(selectinload(DataSource.connections))
            .where(
                DataSource.organization_id == organization.id,
                DataSource.name == name,
            )
        )
        ds = ds_q.scalar_one_or_none()
        if ds is None:
            return GetAgentOutput(
                success=False,
                error_message=f"Agent '{name}' not found.",
            )

        allowed = await user_can_access_data_source(
            db, str(user.id), str(organization.id), ds, str(ds.id)
        )
        if not allowed:
            return GetAgentOutput(
                success=False,
                error_message=f"Access denied to agent '{name}'.",
            )

        # ----- connections (with quick per-connection counts) -----
        connections_out: List[ConnectionRef] = []
        for conn in ds.connections or []:
            ct_count_row = await db.execute(
                select(func.count(ConnectionTable.id)).where(
                    ConnectionTable.connection_id == str(conn.id)
                )
            )
            table_count = int(ct_count_row.scalar() or 0)
            tl_count_row = await db.execute(
                select(func.count(ConnectionTool.id)).where(
                    ConnectionTool.connection_id == str(conn.id)
                )
            )
            tool_count = int(tl_count_row.scalar() or 0)
            connections_out.append(
                ConnectionRef(
                    id=str(conn.id),
                    name=conn.name,
                    type=conn.type,
                    auth_policy=getattr(conn, "auth_policy", "system_only"),
                    is_indexed=table_count > 0 or tool_count > 0,
                    is_active=bool(getattr(conn, "is_active", True)),
                    table_count=table_count,
                    tool_count=tool_count,
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

        # ConnectionTable -> connection name lookup for FQN-ish display
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
            return (-cs, (t.name or "").lower())

        all_active.sort(key=_sort_key)
        top = all_active[:table_limit]
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
            created_at=_iso(getattr(ds, "created_at", None)),
            updated_at=_iso(getattr(ds, "updated_at", None)),
            connections=connections_out,
            conversation_starters=list(ds.conversation_starters or []),
            tables=table_entries,
            tables_truncated=tables_total > len(table_entries),
            tables_total=tables_total,
            tools=tools_out,
            members=members_out,
        )
        return GetAgentOutput(success=True, agent=detail)

    # ---------------------------------------------------------------------
    # list_connections
    # ---------------------------------------------------------------------

    async def list_connections(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
        *,
        name_search: Optional[str] = None,
        type_filter: Optional[str] = None,
        only_tool_providers: bool = False,
        page: int = 1,
        page_size: int = 50,
    ) -> ListConnectionsOutput:
        connections = await self._connection_service.get_connections(db, organization)

        resolved = await resolve_permissions(
            db, str(user.id), str(organization.id)
        )
        is_admin = (
            FULL_ADMIN in resolved.org_permissions
            or resolved.has_org_permission("manage_connections")
        )

        if not is_admin:
            granted_conn_ids = {
                rid
                for (rtype, rid) in resolved.resource_permissions
                if rtype == "connection"
            }
            granted_ds_ids = {
                rid
                for (rtype, rid) in resolved.resource_permissions
                if rtype == "data_source"
            }
            public_rows = await db.execute(
                select(DataSource.id).where(
                    DataSource.organization_id == str(organization.id),
                    DataSource.is_public.is_(True),
                )
            )
            accessible_ds_ids = granted_ds_ids | {str(r) for (r,) in public_rows.all()}

            def _visible(c) -> bool:
                if str(c.id) in granted_conn_ids:
                    return True
                if c.data_sources:
                    return any(
                        str(ds.id) in accessible_ds_ids for ds in c.data_sources
                    )
                return False

            connections = [c for c in connections if _visible(c)]

        needle = (name_search or "").strip().lower()
        type_lower = (type_filter or "").strip().lower()
        filtered = []
        for c in connections:
            if needle and needle not in (c.name or "").lower():
                continue
            if type_lower and c.type.lower() != type_lower:
                continue
            if only_tool_providers and c.type not in TOOL_PROVIDER_TYPES:
                continue
            filtered.append(c)

        total = len(filtered)
        page_items = filtered[(page - 1) * page_size : page * page_size]

        out_rows: List[ConnectionSummary] = []
        for c in page_items:
            tc = await db.execute(
                select(func.count(ConnectionTable.id)).where(
                    ConnectionTable.connection_id == str(c.id)
                )
            )
            table_count = int(tc.scalar() or 0)
            tool_count = 0
            if c.type in TOOL_PROVIDER_TYPES:
                tlc = await db.execute(
                    select(func.count(ConnectionTool.id)).where(
                        ConnectionTool.connection_id == str(c.id)
                    )
                )
                tool_count = int(tlc.scalar() or 0)

            out_rows.append(
                ConnectionSummary(
                    id=str(c.id),
                    name=c.name,
                    type=c.type,
                    auth_policy=c.auth_policy,
                    is_active=bool(c.is_active),
                    is_indexed=table_count > 0 or tool_count > 0,
                    table_count=table_count,
                    tool_count=tool_count,
                    agent_names=[ds.name for ds in (c.data_sources or [])],
                    last_synced_at=_iso(getattr(c, "last_synced_at", None)),
                )
            )

        return ListConnectionsOutput(
            success=True, total=total, connections=out_rows
        )

    # ---------------------------------------------------------------------
    # get_connection
    # ---------------------------------------------------------------------

    async def get_connection(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
        *,
        name: str,
        table_search: Optional[str] = None,
        table_limit: int = 50,
        with_columns: bool = False,
        with_tools: bool = True,
        enforce_manage_connections: bool = True,
    ) -> GetConnectionOutput:
        # Permission gate — callers should pass enforce_manage_connections=True
        # unless they've already gated upstream.
        resolved = await resolve_permissions(db, str(user.id), str(organization.id))
        has_perm_org = (
            FULL_ADMIN in resolved.org_permissions
            or resolved.has_org_permission("manage_connections")
        )

        conn_q = await db.execute(
            select(Connection)
            .options(selectinload(Connection.data_sources))
            .where(
                Connection.organization_id == str(organization.id),
                Connection.name == name,
                Connection.deleted_at.is_(None),
            )
        )
        conn = conn_q.scalar_one_or_none()
        if conn is None:
            return GetConnectionOutput(
                success=False, error_message=f"Connection '{name}' not found."
            )

        if enforce_manage_connections and not has_perm_org:
            has_resource_perm = resolved.has_resource_permission(
                "connection", str(conn.id), "manage_connections"
            ) or resolved.has_resource_permission(
                "connection", str(conn.id), "manage"
            )
            if not has_resource_perm:
                return GetConnectionOutput(
                    success=False,
                    error_message=(
                        f"Access denied to connection '{name}'. "
                        "Requires manage_connections permission."
                    ),
                )

        ix_q = await db.execute(
            select(ConnectionIndexing)
            .where(ConnectionIndexing.connection_id == str(conn.id))
            .order_by(ConnectionIndexing.created_at.desc())
            .limit(1)
        )
        ix = ix_q.scalar_one_or_none()
        indexing_status = ix.status if ix else None

        ct_q = await db.execute(
            select(ConnectionTable)
            .where(ConnectionTable.connection_id == str(conn.id))
            .order_by(ConnectionTable.name)
        )
        all_tables = list(ct_q.scalars().all())
        tables_total = len(all_tables)

        needle = (table_search or "").strip().lower()
        if needle:
            def _matches(ct: ConnectionTable) -> bool:
                if needle in (ct.name or "").lower():
                    return True
                if ct.metadata_json and isinstance(ct.metadata_json, dict):
                    s = (ct.metadata_json.get("schema") or "").lower()
                    if needle in s:
                        return True
                return False

            all_tables = [t for t in all_tables if _matches(t)]

        selected = all_tables[:table_limit]
        tables_out: List[ConnectionTableEntry] = []
        for t in selected:
            schema = None
            if t.metadata_json and isinstance(t.metadata_json, dict):
                schema = t.metadata_json.get("schema") or t.metadata_json.get("dataset")
            raw_cols = t.columns or []
            column_count = len(raw_cols) if isinstance(raw_cols, list) else 0
            columns_preview: Optional[List[ColumnPreview]] = None
            if with_columns and isinstance(raw_cols, list):
                columns_preview = []
                for c in raw_cols[:25]:
                    if isinstance(c, dict):
                        columns_preview.append(
                            ColumnPreview(
                                name=str(c.get("name", "")),
                                dtype=c.get("dtype"),
                            )
                        )
                    else:
                        columns_preview.append(ColumnPreview(name=str(c)))
            tables_out.append(
                ConnectionTableEntry(
                    name=t.name,
                    schema=schema,
                    column_count=column_count,
                    no_rows=t.no_rows,
                    columns_preview=columns_preview,
                )
            )

        tools_out: List[ConnectionToolEntry] = []
        tools_total = 0
        if conn.type in TOOL_PROVIDER_TYPES and with_tools:
            tl_q = await db.execute(
                select(ConnectionTool).where(
                    ConnectionTool.connection_id == str(conn.id)
                )
            )
            conn_tools = list(tl_q.scalars().all())
            tools_total = len(conn_tools)
            for ct in conn_tools[:100]:
                tools_out.append(
                    ConnectionToolEntry(
                        name=ct.name,
                        description=ct.description,
                        is_enabled=bool(getattr(ct, "is_enabled", True)),
                        policy=getattr(ct, "policy", "allow"),
                        input_schema=ct.input_schema,
                    )
                )

        agents_out = [
            ConnectionAgentRef(id=str(ds.id), name=ds.name)
            for ds in (conn.data_sources or [])
        ]

        detail = ConnectionDetail(
            id=str(conn.id),
            name=conn.name,
            type=conn.type,
            auth_policy=conn.auth_policy,
            is_active=bool(conn.is_active),
            last_synced_at=_iso(getattr(conn, "last_synced_at", None)),
            is_indexed=len(all_tables) > 0 or tools_total > 0,
            indexing_status=indexing_status,
            tables=tables_out,
            tables_truncated=len(tables_out) < len(all_tables),
            tables_total=tables_total,
            tools=tools_out,
            tools_total=tools_total,
            agents=agents_out,
            config_preview=_strip_credentials(conn.config),
        )
        return GetConnectionOutput(success=True, connection=detail)

    # ---------------------------------------------------------------------
    # Helper for create_agent's empty-tables guardrail
    # ---------------------------------------------------------------------

    async def count_indexed_tables_for_connections(
        self,
        db: AsyncSession,
        organization: Organization,
        *,
        connection_names: List[str],
    ) -> int:
        """How many ConnectionTable rows exist across the named connections.

        Used by the ``create_agent`` empty-tables guardrail to decide
        whether to block an unconfirmed wide-open agent. Returns 0 if no
        connections match (caller can treat 0 as "safe to create with
        empty filter").
        """
        if not connection_names:
            return 0
        conn_rows = await db.execute(
            select(Connection.id).where(
                Connection.organization_id == str(organization.id),
                Connection.name.in_(connection_names),
                Connection.deleted_at.is_(None),
            )
        )
        ids = [str(r[0]) for r in conn_rows.all()]
        if not ids:
            return 0
        count_row = await db.execute(
            select(func.count(ConnectionTable.id)).where(
                ConnectionTable.connection_id.in_(ids)
            )
        )
        return int(count_row.scalar() or 0)


def _iso(dt) -> Optional[str]:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)
