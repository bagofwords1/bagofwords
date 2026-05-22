"""get_connection — training-mode connection detail.

Requires ``manage_connections`` org permission (or ``full_admin_access``).
Returns the connection's metadata, indexed table list (top-N, optional
column previews), tool list (for mcp/custom_api), the agents using it,
and a credentials-stripped config preview.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator, Dict, List, Optional, Type

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
from app.ai.tools.schemas.get_connection import (
    ColumnPreview,
    ConnectionAgentRef,
    ConnectionDetail,
    ConnectionTableEntry,
    ConnectionToolEntry,
    GetConnectionInput,
    GetConnectionOutput,
)


logger = logging.getLogger(__name__)


TOOL_PROVIDER_TYPES = {"mcp", "custom_api"}
_SENSITIVE_KEY_RE = re.compile(
    r"(pass|secret|token|key|credential|auth)", re.IGNORECASE
)


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


class GetConnectionTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="get_connection",
            description=(
                "Fetch full detail for one connection by name. Returns the connection's "
                "config (credentials stripped), indexed table list (top-N, optional column "
                "previews), tools (for mcp/custom_api connections), and the agents using "
                "it. Use this in training mode when drafting a new agent to explore what's "
                "available on a connection before linking it. Requires manage_connections "
                "permission."
            ),
            category="research",
            version="1.0.0",
            input_schema=GetConnectionInput.model_json_schema(),
            output_schema=GetConnectionOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=True,
            required_permissions=["manage_connections"],
            tags=["connection", "detail", "training", "schema"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {"name": "postgres-prod"},
                    "description": "Connection detail with the top 50 indexed tables.",
                },
                {
                    "input": {"name": "postgres-prod", "with_columns": True, "table_limit": 20},
                    "description": "Top 20 tables with column previews.",
                },
                {
                    "input": {"name": "postgres-prod", "table_search": "invoice"},
                    "description": "Just tables matching 'invoice'.",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return GetConnectionInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return GetConnectionOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = GetConnectionInput(**tool_input)
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
                "table_limit": data.table_limit,
                "with_columns": data.with_columns,
                "with_tools": data.with_tools,
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
            from app.core.permission_resolver import FULL_ADMIN, resolve_permissions
            from app.models.connection import Connection
            from app.models.connection_indexing import ConnectionIndexing
            from app.models.connection_table import ConnectionTable
            from app.models.connection_tool import ConnectionTool

            # Permission gate — also enforced in metadata, but be defensive.
            resolved = await resolve_permissions(
                db, str(user.id), str(organization.id)
            )
            has_perm = (
                FULL_ADMIN in resolved.org_permissions
                or resolved.has_org_permission("manage_connections")
            )
            if not has_perm:
                # Per-connection grant fallback (resolved after we find the row)
                pass

            conn_q = await db.execute(
                select(Connection)
                .options(selectinload(Connection.data_sources))
                .where(
                    Connection.organization_id == str(organization.id),
                    Connection.name == data.name,
                    Connection.deleted_at.is_(None),
                )
            )
            conn = conn_q.scalar_one_or_none()
            if conn is None:
                output = GetConnectionOutput(
                    success=False,
                    error_message=f"Connection '{data.name}' not found.",
                )
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": output.model_dump(),
                        "observation": {
                            "summary": f"Connection '{data.name}' not found."
                        },
                    },
                )
                return

            # Re-check now that we know the resource id
            if not has_perm:
                has_perm = resolved.has_resource_permission(
                    "connection", str(conn.id), "manage_connections"
                ) or resolved.has_resource_permission(
                    "connection", str(conn.id), "manage"
                )
            if not has_perm:
                yield ToolErrorEvent(
                    type="tool.error",
                    payload={
                        "error": (
                            f"Access denied to connection '{data.name}'. "
                            "Requires manage_connections permission."
                        ),
                        "code": "FORBIDDEN",
                    },
                )
                return

            # Indexing state — latest run
            ix_q = await db.execute(
                select(ConnectionIndexing)
                .where(ConnectionIndexing.connection_id == str(conn.id))
                .order_by(ConnectionIndexing.created_at.desc())
                .limit(1)
            )
            ix = ix_q.scalar_one_or_none()
            indexing_status = ix.status if ix else None

            # Tables
            ct_q = await db.execute(
                select(ConnectionTable)
                .where(ConnectionTable.connection_id == str(conn.id))
                .order_by(ConnectionTable.name)
            )
            all_tables = list(ct_q.scalars().all())
            tables_total = len(all_tables)

            # Filter by search
            needle = (data.table_search or "").strip().lower()
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

            selected = all_tables[: data.table_limit]
            tables_out: List[ConnectionTableEntry] = []
            for t in selected:
                schema = None
                if t.metadata_json and isinstance(t.metadata_json, dict):
                    schema = t.metadata_json.get("schema") or t.metadata_json.get(
                        "dataset"
                    )
                raw_cols = t.columns or []
                column_count = len(raw_cols) if isinstance(raw_cols, list) else 0
                columns_preview: Optional[List[ColumnPreview]] = None
                if data.with_columns and isinstance(raw_cols, list):
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

            # Tools
            tools_out: List[ConnectionToolEntry] = []
            tools_total = 0
            if conn.type in TOOL_PROVIDER_TYPES and data.with_tools:
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

            # Agents using
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
                last_synced_at=(
                    conn.last_synced_at.isoformat()
                    if getattr(conn, "last_synced_at", None)
                    and hasattr(conn.last_synced_at, "isoformat")
                    else None
                ),
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
            output = GetConnectionOutput(success=True, connection=detail)
            summary = (
                f"Loaded connection '{conn.name}' "
                f"({conn.type}, {tables_total} tables, {tools_total} tools, "
                f"{len(agents_out)} agents)."
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {"summary": summary},
                },
            )
        except Exception as e:
            logger.exception(f"get_connection failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": str(e), "code": "QUERY_FAILED"},
            )
