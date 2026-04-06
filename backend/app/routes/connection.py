"""
Connection Routes - Admin-only CRUD for database connections.
Connections are the underlying database connections that Domains (DataSources) link to.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import List

from app.dependencies import get_async_db
from app.models.user import User
from app.core.auth import current_user
from app.models.organization import Organization
from app.models.datasource_table import DataSourceTable
from app.models.connection_table import ConnectionTable
from app.dependencies import get_current_organization
from app.services.connection_service import ConnectionService
from app.core.permissions_decorator import requires_permission
from app.core.permission_resolver import resolve_permissions, FULL_ADMIN
from app.models.membership import Membership, ROLES_PERMISSIONS
from app.schemas.connection_schema import (
    ConnectionCreate,
    ConnectionUpdate,
    ConnectionSchema,
    ConnectionDetailSchema,
    ConnectionTableSchema,
    ConnectionTestOverride,
    ConnectionTestResult,
)
from app.schemas.connection_tool_schema import (
    ConnectionToolSchema,
    ConnectionToolUpdate,
    BatchToolUpdate,
)


router = APIRouter(prefix="/connections", tags=["connections"])
connection_service = ConnectionService()


async def _is_org_admin(db: AsyncSession, user: User, organization: Organization) -> bool:
    """Return True if user has admin-level data source permissions in the org."""
    result = await db.execute(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.organization_id == organization.id,
        )
    )
    membership = result.scalar_one_or_none()
    if not membership:
        return False
    return "update_data_source" in ROLES_PERMISSIONS.get(membership.role, set())


async def _user_can_access_connection(
    db: AsyncSession, user: User, connection
) -> bool:
    """Non-admin accessibility check: user must have access to at least one linked data source."""
    for ds in (connection.data_sources or []):
        if getattr(ds, "is_public", False):
            return True
        if await ds.has_membership_async(user.id, db):
            return True
    return False


# ==================== Routes ====================

@router.get("", response_model=List[ConnectionSchema])
@requires_permission('view_connections')
async def list_connections(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """List connections the user has access to.

    Admins (manage_connections or full_admin_access) see all connections.
    Other users only see connections they have resource grants on.
    """
    connections = await connection_service.get_connections(db, organization)

    # Filter by user access unless admin
    resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
    is_admin = FULL_ADMIN in resolved.org_permissions or resolved.has_org_permission("manage_connections")

    if not is_admin:
        granted_conn_ids = {
            rid for (rtype, rid) in resolved.resource_permissions
            if rtype == "connection"
        }
        connections = [c for c in connections if str(c.id) in granted_conn_ids]

    result = []
    for conn in connections:
        # Count tables from ConnectionTable (all available tables in the database)
        count_result = await db.execute(
            select(func.count(ConnectionTable.id))
            .where(ConnectionTable.connection_id == str(conn.id))
        )
        table_count = count_result.scalar() or 0

        # Fallback for legacy connections: if ConnectionTable is empty,
        # count from DataSourceTable (existing domains using this connection)
        if table_count == 0 and conn.data_sources:
            ds_ids = [str(ds.id) for ds in conn.data_sources]
            if ds_ids:
                fallback_result = await db.execute(
                    select(func.count(DataSourceTable.id))
                    .where(DataSourceTable.datasource_id.in_(ds_ids))
                )
                table_count = fallback_result.scalar() or 0

        result.append(ConnectionSchema(
            id=str(conn.id),
            name=conn.name,
            type=conn.type,
            is_active=conn.is_active,
            auth_policy=conn.auth_policy,
            last_synced_at=conn.last_synced_at.isoformat() if conn.last_synced_at else None,
            organization_id=str(conn.organization_id),
            table_count=table_count,
            domain_count=len(conn.data_sources) if conn.data_sources else 0,
            domain_names=[ds.name for ds in conn.data_sources] if conn.data_sources else [],
        ))
    return result


@router.post("", response_model=ConnectionSchema)
@requires_permission('create_data_source')  # Admin-only
async def create_connection(
    data: ConnectionCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Create a new database connection."""
    connection = await connection_service.create_connection(
        db=db,
        organization=organization,
        current_user=current_user,
        name=data.name,
        type=data.type,
        config=data.config,
        credentials=data.credentials,
        auth_policy=data.auth_policy,
        allowed_user_auth_modes=data.allowed_user_auth_modes,
    )
    
    return ConnectionSchema(
        id=str(connection.id),
        name=connection.name,
        type=connection.type,
        is_active=connection.is_active,
        auth_policy=connection.auth_policy,
        last_synced_at=connection.last_synced_at.isoformat() if connection.last_synced_at else None,
        organization_id=str(connection.organization_id),
        table_count=len(connection.connection_tables) if connection.connection_tables else 0,
        domain_count=len(connection.data_sources) if connection.data_sources else 0,
    )


@router.get("/{connection_id}", response_model=ConnectionDetailSchema)
@requires_permission('view_data_source')
async def get_connection(
    connection_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get connection details. Non-admins get a redacted view (no config/credentials) and must have access to at least one linked data source."""
    connection = await connection_service.get_connection(db, connection_id, organization)

    is_admin = await _is_org_admin(db, current_user, organization)
    if not is_admin:
        if not await _user_can_access_connection(db, current_user, connection):
            raise HTTPException(status_code=403, detail="Access denied to this connection")

    # Parse config if it's a string
    import json
    config = connection.config
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except:
            config = {}

    # Strip sensitive fields for non-admins
    if not is_admin:
        config = {}
        allowed_user_auth_modes = []
        has_credentials = False
    else:
        allowed_user_auth_modes = connection.allowed_user_auth_modes
        has_credentials = bool(connection.credentials)

    return ConnectionDetailSchema(
        id=str(connection.id),
        name=connection.name,
        type=connection.type,
        is_active=connection.is_active,
        auth_policy=connection.auth_policy,
        allowed_user_auth_modes=allowed_user_auth_modes,
        config=config or {},
        last_synced_at=connection.last_synced_at.isoformat() if connection.last_synced_at else None,
        organization_id=str(connection.organization_id),
        table_count=len(connection.connection_tables) if connection.connection_tables else 0,
        domain_count=len(connection.data_sources) if connection.data_sources else 0,
        domain_names=[ds.name for ds in connection.data_sources] if connection.data_sources else [],
        has_credentials=has_credentials,
    )


@router.put("/{connection_id}", response_model=ConnectionSchema)
@requires_permission('update_data_source')  # Admin-only
async def update_connection(
    connection_id: str,
    data: ConnectionUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Update a connection."""
    updates = data.dict(exclude_unset=True)
    connection = await connection_service.update_connection(
        db=db,
        connection_id=connection_id,
        organization=organization,
        current_user=current_user,
        **updates,
    )
    
    return ConnectionSchema(
        id=str(connection.id),
        name=connection.name,
        type=connection.type,
        is_active=connection.is_active,
        auth_policy=connection.auth_policy,
        last_synced_at=connection.last_synced_at.isoformat() if connection.last_synced_at else None,
        organization_id=str(connection.organization_id),
        table_count=len(connection.connection_tables) if connection.connection_tables else 0,
        domain_count=len(connection.data_sources) if connection.data_sources else 0,
    )


@router.delete("/{connection_id}")
@requires_permission('delete_data_source')  # Admin-only
async def delete_connection(
    connection_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Delete a connection. Fails if connection is linked to any domains."""
    return await connection_service.delete_connection(
        db=db,
        connection_id=connection_id,
        organization=organization,
        current_user=current_user,
    )


@router.post("/test-params")
@requires_permission('update_data_source')
async def test_connection_params(
    data: ConnectionCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Test connection parameters before saving. Works for all types including MCP/API."""
    result = await connection_service.test_connection_params(
        data_source_type=data.type,
        config=data.config,
        credentials=data.credentials,
    )
    return result


@router.post("/{connection_id}/test", response_model=ConnectionTestResult)
@requires_permission('update_data_source')  # Admin-only
async def test_connection(
    connection_id: str,
    overrides: ConnectionTestOverride = None,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Test a connection, optionally with override credentials/config."""
    result = await connection_service.test_connection(
        db=db,
        connection_id=connection_id,
        organization=organization,
        current_user=current_user,
        config_overrides=overrides.config if overrides else None,
        credential_overrides=overrides.credentials if overrides else None,
    )
    
    return ConnectionTestResult(
        success=result.get("success", False),
        message=result.get("message", ""),
        connectivity=result.get("connectivity", result.get("success", False)),
        schema_access=result.get("schema_access", False),
        table_count=result.get("table_count", 0),
    )


@router.post("/{connection_id}/refresh")
@requires_permission('update_data_source')  # Admin-only
async def refresh_connection_schema(
    connection_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Refresh connection schema (discover tables)."""
    connection = await connection_service.get_connection(db, connection_id, organization)
    tables = await connection_service.refresh_schema(db, connection, current_user)
    
    return {
        "message": f"Refreshed schema. Found {len(tables)} tables.",
        "table_count": len(tables),
    }


@router.get("/{connection_id}/tables", response_model=List[ConnectionTableSchema])
@requires_permission('update_data_source')  # Admin-only
async def get_connection_tables(
    connection_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get tables for a connection."""
    connection = await connection_service.get_connection(db, connection_id, organization)
    
    result = []
    for table in (connection.connection_tables or []):
        result.append(ConnectionTableSchema(
            id=str(table.id),
            name=table.name,
            column_count=len(table.columns) if table.columns else 0,
        ))
    return result


# ==================== Tool Management Routes (MCP / Custom API) ====================

@router.post("/{connection_id}/refresh-tools", response_model=List[ConnectionToolSchema])
@requires_permission('update_data_source')
async def refresh_connection_tools(
    connection_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Refresh/discover tools for an MCP or Custom API connection."""
    connection = await connection_service.get_connection(db, connection_id, organization)
    tools = await connection_service.refresh_tools(db, connection, current_user)
    return [
        ConnectionToolSchema(
            id=str(t.id),
            name=t.name,
            description=t.description,
            is_enabled=t.is_enabled,
            policy=t.policy,
            connection_id=str(t.connection_id),
            input_schema=t.input_schema,
            output_schema=t.output_schema,
        )
        for t in tools
    ]


@router.get("/{connection_id}/tools", response_model=List[ConnectionToolSchema])
@requires_permission('update_data_source')
async def get_connection_tools_list(
    connection_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Get all tools for a connection."""
    # Verify connection belongs to org
    await connection_service.get_connection(db, connection_id, organization)
    tools = await connection_service.get_connection_tools(db, connection_id)
    return [
        ConnectionToolSchema(
            id=str(t.id),
            name=t.name,
            description=t.description,
            is_enabled=t.is_enabled,
            policy=t.policy,
            connection_id=str(t.connection_id),
            input_schema=t.input_schema,
            output_schema=t.output_schema,
        )
        for t in tools
    ]


@router.put("/{connection_id}/tools/batch", response_model=List[ConnectionToolSchema])
@requires_permission('update_data_source')
async def batch_update_connection_tools(
    connection_id: str,
    data: BatchToolUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Batch enable/disable tools."""
    await connection_service.get_connection(db, connection_id, organization)
    tools = await connection_service.batch_update_tools(db, data.tool_ids, data.is_enabled)
    return [
        ConnectionToolSchema(
            id=str(t.id),
            name=t.name,
            description=t.description,
            is_enabled=t.is_enabled,
            policy=t.policy,
            connection_id=str(t.connection_id),
            input_schema=t.input_schema,
            output_schema=t.output_schema,
        )
        for t in tools
    ]


@router.put("/{connection_id}/tools/{tool_id}", response_model=ConnectionToolSchema)
@requires_permission('update_data_source')
async def update_tool(
    connection_id: str,
    tool_id: str,
    data: ConnectionToolUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Enable/disable a tool or update its policy."""
    await connection_service.get_connection(db, connection_id, organization)
    tool = await connection_service.update_connection_tool(
        db, tool_id, is_enabled=data.is_enabled, policy=data.policy
    )
    return ConnectionToolSchema(
        id=str(tool.id),
        name=tool.name,
        description=tool.description,
        is_enabled=tool.is_enabled,
        policy=tool.policy,
        connection_id=str(tool.connection_id),
        input_schema=tool.input_schema,
        output_schema=tool.output_schema,
    )

