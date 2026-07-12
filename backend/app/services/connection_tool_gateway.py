"""ConnectionToolGateway — shared resolution/policy/execution for agent tools.

An *agent* (formerly "data source") can have ``mcp`` and ``custom_api``
connections attached. Each such connection exposes a set of tools recorded as
``ConnectionTool`` rows, with org-wide ``is_enabled``/``policy`` defaults that a
per-agent ``DataSourceConnectionTool`` overlay may override.

This service centralizes three things that previously lived inline in the
internal ``execute_mcp`` planner tool, so the external MCP gateway and the
internal planner share one code path:

  1. discovery  — list the effective (overlay-resolved) tools for a set of agents
  2. policy     — resolve effective ``is_enabled`` / ``policy`` per (tool, agent)
  3. execution  — construct the provider client and invoke a tool, enforcing policy

The gateway is transport-agnostic: callers (internal planner, MCP server tools)
own feature-gating, tracking, and result materialization.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.connection import Connection
from app.models.connection_tool import ConnectionTool
from app.models.data_source import DataSource
from app.models.data_source_connection_tool import DataSourceConnectionTool
from app.models.domain_connection import domain_connection
from app.models.organization import Organization
from app.services.tool_policy_service import (
    ToolPolicyService,
    TOOL_POLICY_ALLOW,
    TOOL_POLICY_ASK,
    TOOL_POLICY_AUTO,
    TOOL_POLICY_DENY,
    normalize_tool_policy,
    resolve_effective_policy,
)

logger = logging.getLogger(__name__)

TOOL_CONNECTION_TYPES = ("mcp", "custom_api")


@dataclass
class GatewayTool:
    """An effective, overlay-resolved tool exposed by an agent's connection."""

    name: str
    description: Optional[str]
    connection_id: str
    connection_name: Optional[str]
    connection_type: Optional[str]
    data_source_id: str
    data_source_name: Optional[str]
    input_schema: Optional[Dict[str, Any]]
    is_enabled: bool
    policy: str  # admin policy: allow | ask | deny | auto
    connection_tool_id: Optional[str] = None
    # Requesting user's own preference and the policy that applies to them
    # (only differs from ``policy`` when list_tools was given a user).
    user_policy: Optional[str] = None
    effective_policy: Optional[str] = None

    def to_summary(self) -> Dict[str, Any]:
        """Compact form (no input_schema) — mirrors the internal <mcp_tools> block."""
        return {
            "name": self.name,
            "description": self.description or "",
            "connection_id": self.connection_id,
            "connection_name": self.connection_name or "",
            "connection_type": self.connection_type or "",
            "data_source_id": self.data_source_id,
            "policy": self.effective_policy or self.policy,
        }

    def to_full(self) -> Dict[str, Any]:
        """Full form with input_schema — for the discovery tool."""
        d = self.to_summary()
        d["input_schema"] = self.input_schema or {}
        return d


@dataclass
class GatewayResult:
    """Outcome of an ``execute`` call."""

    success: bool
    content_type: str = "json"  # tabular | text | json | binary
    data: Any = None
    connection_name: Optional[str] = None
    error: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None
    blocked_by_policy: Optional[str] = None  # set to the policy value when blocked

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "content_type": self.content_type,
            "data": self.data,
            "connection_name": self.connection_name,
            "error": self.error,
            "input_schema": self.input_schema,
            "blocked_by_policy": self.blocked_by_policy,
        }


class ConnectionToolGateway:
    """Resolve and execute an agent's MCP / custom-API tools."""

    async def _load_tool_connection_ids(
        self, db: AsyncSession, data_source_ids: List[str]
    ) -> Dict[str, List[Connection]]:
        """Map each data_source_id -> its mcp/custom_api Connection objects.

        Queried via the association table directly so it works in async context
        without relying on lazy relationship loading.
        """
        if not data_source_ids:
            return {}
        rows = await db.execute(
            select(domain_connection.c.data_source_id, Connection)
            .join(Connection, Connection.id == domain_connection.c.connection_id)
            .where(
                domain_connection.c.data_source_id.in_([str(i) for i in data_source_ids]),
                Connection.type.in_(TOOL_CONNECTION_TYPES),
            )
        )
        out: Dict[str, List[Connection]] = {}
        for ds_id, conn in rows.all():
            out.setdefault(str(ds_id), []).append(conn)
        return out

    async def _load_overlays(
        self, db: AsyncSession, data_source_ids: List[str]
    ) -> Dict[tuple, DataSourceConnectionTool]:
        """Map (data_source_id, connection_tool_id) -> overlay row."""
        if not data_source_ids:
            return {}
        rows = await db.execute(
            select(DataSourceConnectionTool).where(
                DataSourceConnectionTool.data_source_id.in_([str(i) for i in data_source_ids])
            )
        )
        return {
            (o.data_source_id, o.connection_tool_id): o for o in rows.scalars().all()
        }

    async def list_tools(
        self,
        db: AsyncSession,
        organization: Organization,
        *,
        data_source_ids: List[str],
        include_disabled: bool = False,
        current_user=None,
    ) -> List[GatewayTool]:
        """List effective tools across the given agents.

        Effective ``is_enabled`` / ``policy`` come from the per-agent overlay
        when present, otherwise the ConnectionTool default. Disabled tools are
        excluded unless ``include_disabled`` is set. When ``current_user`` is
        given, each tool also carries that user's own preference and the
        user-resolved ``effective_policy``.
        """
        data_source_ids = [str(i) for i in (data_source_ids or [])]
        if not data_source_ids:
            return []

        conns_by_ds = await self._load_tool_connection_ids(db, data_source_ids)
        if not conns_by_ds:
            return []

        overlays = await self._load_overlays(db, data_source_ids)

        # Resolve agent names for labeling.
        ds_rows = await db.execute(
            select(DataSource.id, DataSource.name).where(
                DataSource.id.in_(list(conns_by_ds.keys()))
            )
        )
        ds_names = {str(i): n for i, n in ds_rows.all()}

        # All relevant connection ids -> their ConnectionTool rows in one query.
        all_conn_ids = {
            str(c.id) for conns in conns_by_ds.values() for c in conns
        }
        ct_rows = await db.execute(
            select(ConnectionTool).where(
                ConnectionTool.connection_id.in_(list(all_conn_ids))
            )
        )
        tools_by_conn: Dict[str, List[ConnectionTool]] = {}
        all_tool_ids: List[str] = []
        for ct in ct_rows.scalars().all():
            tools_by_conn.setdefault(str(ct.connection_id), []).append(ct)
            all_tool_ids.append(str(ct.id))

        user_prefs: Dict[str, str] = {}
        if current_user is not None and all_tool_ids:
            user_prefs = await ToolPolicyService().get_user_preferences(
                db, str(current_user.id), all_tool_ids
            )

        result: List[GatewayTool] = []
        for ds_id, conns in conns_by_ds.items():
            for conn in conns:
                for ct in tools_by_conn.get(str(conn.id), []):
                    overlay = overlays.get((ds_id, str(ct.id)))
                    is_enabled = overlay.is_enabled if overlay else ct.is_enabled
                    policy = normalize_tool_policy(overlay.policy if overlay else ct.policy)
                    if not is_enabled and not include_disabled:
                        continue
                    user_policy = user_prefs.get(str(ct.id))
                    result.append(
                        GatewayTool(
                            name=ct.name,
                            description=ct.description,
                            connection_id=str(conn.id),
                            connection_name=conn.name,
                            connection_type=conn.type,
                            data_source_id=ds_id,
                            data_source_name=ds_names.get(ds_id),
                            input_schema=ct.input_schema,
                            is_enabled=is_enabled,
                            policy=policy,
                            connection_tool_id=str(ct.id),
                            user_policy=user_policy,
                            effective_policy=resolve_effective_policy(policy, user_policy),
                        )
                    )
        return result

    async def execute(
        self,
        db: AsyncSession,
        organization: Organization,
        *,
        data_source_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        connection_id: Optional[str] = None,
        current_user=None,
        allow_confirm: bool = False,
    ) -> GatewayResult:
        """Invoke ``tool_name`` on one of ``data_source_id``'s tool connections.

        Enforces effective enablement + policy for ``current_user`` (their
        preference stacks on the admin layers; admin deny is absolute). Only
        ``allow`` tools run by default; ``ask`` is blocked unless
        ``allow_confirm`` is set (there is no interactive prompt over the MCP
        gateway), ``deny`` is always blocked, and ``auto`` delegates the
        decision to the small-model tool-call judge.
        """
        tools = await self.list_tools(
            db, organization, data_source_ids=[str(data_source_id)],
            include_disabled=True, current_user=current_user,
        )
        candidates = [t for t in tools if t.name == tool_name]
        if connection_id:
            candidates = [t for t in candidates if t.connection_id == str(connection_id)]

        if not candidates:
            return GatewayResult(
                success=False,
                content_type="text",
                error=f"Tool '{tool_name}' not found on agent '{data_source_id}'.",
            )
        tool = candidates[0]

        if not tool.is_enabled:
            return GatewayResult(
                success=False,
                content_type="text",
                error=f"Tool '{tool_name}' is disabled for this agent.",
                input_schema=tool.input_schema,
                blocked_by_policy="disabled",
            )

        effective = tool.effective_policy or tool.policy
        if effective == TOOL_POLICY_AUTO:
            verdict = await self._auto_judge(
                db, organization, current_user, tool, arguments or {}
            )
            if not verdict.approve:
                return GatewayResult(
                    success=False,
                    content_type="text",
                    error=(
                        f"Tool '{tool_name}' was declined by automatic policy review: "
                        f"{verdict.reason or 'not approved'}."
                    ),
                    input_schema=tool.input_schema,
                    blocked_by_policy=TOOL_POLICY_AUTO,
                )
        elif effective != TOOL_POLICY_ALLOW and not (
            effective == TOOL_POLICY_ASK and allow_confirm
        ):
            return GatewayResult(
                success=False,
                content_type="text",
                error=(
                    f"Tool '{tool_name}' is blocked by policy '{effective}' and "
                    f"cannot be invoked through the MCP gateway."
                ),
                input_schema=tool.input_schema,
                blocked_by_policy=effective,
            )

        # Resolve the Connection ORM object and construct a provider client.
        conn = (
            await db.execute(
                select(Connection).where(
                    Connection.id == tool.connection_id,
                    Connection.organization_id == str(organization.id),
                )
            )
        ).scalars().first()
        if not conn:
            return GatewayResult(
                success=False,
                content_type="text",
                error=f"Connection '{tool.connection_id}' not found in organization.",
                input_schema=tool.input_schema,
            )

        from app.services.connection_service import ConnectionService

        try:
            client = await ConnectionService().construct_client(db, conn, current_user)
            raw = await client.acall_tool(tool_name, arguments or {})
        except BaseException as e:  # noqa: BLE001 — surface provider errors as data
            logger.error("ConnectionToolGateway.execute failed: %s", e, exc_info=True)
            return GatewayResult(
                success=False,
                content_type="text",
                connection_name=conn.name,
                error=str(e),
                input_schema=tool.input_schema,
            )

        if not raw.get("success"):
            return GatewayResult(
                success=False,
                content_type=raw.get("content_type", "text"),
                connection_name=conn.name,
                error=raw.get("error", "Unknown error"),
                input_schema=tool.input_schema,
            )

        return GatewayResult(
            success=True,
            content_type=raw.get("content_type", "json"),
            data=raw.get("data"),
            connection_name=conn.name,
            input_schema=tool.input_schema,
        )

    async def _auto_judge(self, db, organization, current_user, tool: GatewayTool, arguments: Dict[str, Any]):
        """Small-model review of an 'auto' policy call on the gateway path."""
        from app.ai.classifiers.tool_call_judge import ToolCallJudge, ToolCallVerdict

        try:
            from app.services.llm_service import LLMService

            small_model = await LLMService().get_default_model(
                db, organization, current_user, is_small=True
            )
        except Exception as e:
            logger.error("ConnectionToolGateway: failed to resolve small model: %s", e)
            small_model = None
        if small_model is None:
            return ToolCallVerdict(
                approve=False, confidence=0.0,
                reason="no LLM model available for automatic policy review",
            )
        return await ToolCallJudge(small_model).judge(
            tool_name=tool.name,
            tool_description=tool.description,
            connection_name=tool.connection_name,
            arguments=arguments,
        )
