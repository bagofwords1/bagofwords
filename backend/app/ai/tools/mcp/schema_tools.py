"""MCP Tools: agent schema — read the tables an agent uses, and choose them.

Same gates as the web app's data-source routes:

- ``describe_tables`` ⇔ ``GET /data_sources/{id}/full_schema`` — needs
  ``view_schema`` (implicit on any grant on the agent, and on public agents).
  Readers see only the agent's selected tables with hidden columns stripped;
  agent managers also see deselected tables.
- ``edit_schema`` ⇔ ``PUT /data_sources/{id}/update_tables_status`` — needs
  ``manage`` on the agent.

A foreign, missing or invisible agent id gets one answer, so ids can't be probed.
"""

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.core.permission_resolver import resolve_permissions
from app.models.data_source import DataSource
from app.models.organization import Organization
from app.models.user import User
from app.schemas.mcp import (
    MCPDescribeTablesInput,
    MCPDescribeTablesOutput,
    MCPEditSchemaInput,
    MCPEditSchemaOutput,
    MCPTableColumn,
    MCPTableItem,
)

logger = logging.getLogger(__name__)

AGENT_NOT_FOUND = "Agent not found, or you don't have access to it."


async def _agent_with_permission(
    db: AsyncSession, user: User, organization: Organization, data_source_id: str, permission: str,
):
    """(agent, resolved perms) if the caller holds ``permission`` on it, else (None, resolved).

    Mirrors ``requires_resource_permission``: full admin / org-level perm /
    per-agent grant (with ``manage`` implications), and public agents are
    readable by every member.
    """
    resolved = await resolve_permissions(db, str(user.id), str(organization.id))
    from app.services.connected_application_service import agent_ceiling
    ceiling = await agent_ceiling(db, organization.id)
    if ceiling is not None and str(data_source_id) not in ceiling:
        return None, resolved
    ds = (await db.execute(
        select(DataSource).where(
            DataSource.id == str(data_source_id),
            DataSource.organization_id == str(organization.id),
        )
    )).scalar_one_or_none()
    if ds is None:
        return None, resolved
    if resolved.has_org_permission(permission) or resolved.has_resource_permission(
        "data_source", str(ds.id), permission
    ):
        return ds, resolved
    if permission in ("view", "view_schema") and getattr(ds, "is_public", False):
        return ds, resolved
    return None, resolved


def _columns(raw) -> list[MCPTableColumn]:
    out = []
    for c in raw or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        out.append(MCPTableColumn(
            name=str(c["name"]),
            dtype=c.get("dtype") or c.get("type"),
            description=c.get("description") or None,
        ))
    return out


class DescribeTablesMCPTool(MCPTool):
    """List an agent's tables and columns."""

    name = "describe_tables"
    description = (
        "List the tables an agent (data source) works with, with columns and types. "
        "Agent managers also see tables that are deselected (is_active=false) and can change the "
        "selection with edit_schema; everyone else sees only the tables the agent uses."
    )

    @property
    def input_schema(self) -> dict[str, Any]:
        return MCPDescribeTablesInput.model_json_schema()

    async def execute(self, args, db, user, organization):
        data = MCPDescribeTablesInput(**args)
        ds, resolved = await _agent_with_permission(db, user, organization, data.data_source_id, "view_schema")
        if ds is None:
            return MCPDescribeTablesOutput(success=False, error_message=AGENT_NOT_FOUND).model_dump()
        can_manage = resolved.has_resource_permission("data_source", str(ds.id), "manage")

        from app.services.data_source_service import DataSourceService
        page = await DataSourceService().get_data_source_schema_paginated(
            db=db,
            data_source_id=str(ds.id),
            organization=organization,
            page=data.page,
            page_size=data.page_size,
            search=data.search,
            sort_by="name",
            sort_dir="asc",
            include_inactive=True,
            current_user=user,
            exclude_file_source_types=True,
            restrict_to_active=not can_manage,
        )
        tables = [
            MCPTableItem(
                id=t.id,
                name=t.name,
                is_active=bool(t.is_active),
                rows=t.no_rows,
                connection_name=t.connection_name,
                columns=_columns(t.columns) if data.include_columns else None,
                excluded_columns=t.excluded_columns if can_manage else None,
            )
            for t in page.tables
        ]
        return MCPDescribeTablesOutput(
            success=True,
            data_source_id=str(ds.id),
            can_edit_schema=can_manage,
            tables=tables,
            total=page.total,
            selected_count=page.selected_count,
            page=page.page,
            has_more=page.has_more,
        ).model_dump()


class EditSchemaMCPTool(MCPTool):
    """Choose which tables (and columns) an agent uses."""

    name = "edit_schema"
    description = (
        "Change which tables an agent (data source) uses: `activate` / `deactivate` take table names "
        "(or ids) from describe_tables. `excluded_columns` maps a table id to the COMPLETE list of "
        "columns to hide from the agent ([] shows all). Needs manage rights on the agent."
    )

    @property
    def required_ds_permission(self):
        return "manage"

    @property
    def input_schema(self) -> dict[str, Any]:
        return MCPEditSchemaInput.model_json_schema()

    async def execute(self, args, db, user, organization):
        data = MCPEditSchemaInput(**args)
        if not (data.activate or data.deactivate or data.excluded_columns):
            return MCPEditSchemaOutput(success=False, error_message="Nothing to change.").model_dump()
        overlap = set(data.activate) & set(data.deactivate)
        if overlap:
            return MCPEditSchemaOutput(
                success=False, error_message=f"Tables in both activate and deactivate: {sorted(overlap)}",
            ).model_dump()

        ds, _ = await _agent_with_permission(db, user, organization, data.data_source_id, "manage")
        if ds is None:
            return MCPEditSchemaOutput(success=False, error_message=AGENT_NOT_FOUND).model_dump()

        from app.services.data_source_service import DataSourceService
        try:
            result = await DataSourceService().update_tables_status_delta(
                db=db,
                data_source_id=str(ds.id),
                organization=organization,
                activate=data.activate,
                deactivate=data.deactivate,
                current_user=user,
                excluded_columns=data.excluded_columns,
            )
        except HTTPException as e:
            return MCPEditSchemaOutput(success=False, error_message=str(e.detail)[:500]).model_dump()

        try:
            from app.ee.audit.service import audit_service
            await audit_service.log(
                db=db, organization_id=organization.id, action="data_source.tables_updated",
                user_id=user.id, resource_type="data_source", resource_id=str(ds.id),
                details={"activated": len(data.activate), "deactivated": len(data.deactivate),
                         "columns_updated": result.columns_updated_count, "via": "mcp"},
            )
        except Exception:
            pass

        logger.info("edit_schema: agent %s by user %s +%d -%d", ds.id, user.id,
                    result.activated_count, result.deactivated_count)
        return MCPEditSchemaOutput(
            success=True,
            activated_count=result.activated_count,
            deactivated_count=result.deactivated_count,
            columns_updated_count=result.columns_updated_count,
            selected_count=result.total_selected,
        ).model_dump()
