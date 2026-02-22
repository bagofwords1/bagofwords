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
from app.schemas.connection_schema import (
    ConnectionCreate,
    ConnectionUpdate,
    ConnectionSchema,
    ConnectionDetailSchema,
    ConnectionTableSchema,
    ConnectionTestOverride,
    ConnectionTestResult,
)


router = APIRouter(prefix="/connections", tags=["connections"])
connection_service = ConnectionService()


# ==================== Routes ====================

@router.get("", response_model=List[ConnectionSchema])
@requires_permission('update_data_source')  # Admin-only
async def list_connections(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """List all connections for the organization."""
    connections = await connection_service.get_connections(db, organization)
    
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
@requires_permission('update_data_source')  # Admin-only
async def get_connection(
    connection_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get connection details including config for editing."""
    connection = await connection_service.get_connection(db, connection_id, organization)
    
    # Parse config if it's a string
    import json
    config = connection.config
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except:
            config = {}
    
    return ConnectionDetailSchema(
        id=str(connection.id),
        name=connection.name,
        type=connection.type,
        is_active=connection.is_active,
        auth_policy=connection.auth_policy,
        allowed_user_auth_modes=connection.allowed_user_auth_modes,
        config=config or {},
        last_synced_at=connection.last_synced_at.isoformat() if connection.last_synced_at else None,
        organization_id=str(connection.organization_id),
        table_count=len(connection.connection_tables) if connection.connection_tables else 0,
        domain_count=len(connection.data_sources) if connection.data_sources else 0,
        domain_names=[ds.name for ds in connection.data_sources] if connection.data_sources else [],
        has_credentials=bool(connection.credentials),
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

