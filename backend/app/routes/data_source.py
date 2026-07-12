from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Body, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db, release_request_db
from typing import Optional, List, Union

from app.ee.audit.service import audit_service

from app.models.user import User
from app.core.auth import current_user
from app.models.organization import Organization
from app.dependencies import get_current_organization
from app.services.data_source_service import DataSourceService
from app.schemas.data_source_schema import DataSourceCreate, DataSourceBase, DataSourceSchema, DataSourceUpdate, DataSourceMembershipCreate, DataSourceListItemSchema
from app.schemas.metadata_indexing_job_schema import MetadataIndexingJobSchema
from app.schemas.data_source_schema import DataSourceMembershipSchema
from app.schemas.datasource_table_schema import (
    DataSourceTableSchema,
    PaginatedTablesResponse,
    BulkUpdateTablesRequest,
    DeltaUpdateTablesRequest,
    DeltaUpdateTablesResponse,
)
from app.core.permissions_decorator import requires_permission, requires_resource_permission, check_resource_permissions
from app.models.data_source import DataSource

router = APIRouter(tags=["data_sources"])
data_source_service = DataSourceService()

@router.get("/available_data_sources", response_model=list[dict])
async def get_available_data_sources(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.get_available_data_sources(db, organization)

@router.get("/connectors/catalog", response_model=list[dict])
async def get_connectors_catalog(
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Curated catalog of pre-built MCP integrations (Monday, Notion, …) —
    the named presets on the registry's `mcp` entry. Admins add them from the
    Add Connection catalog; the DCR ones need no setup."""
    from app.schemas.data_source_registry import mcp_presets
    return mcp_presets()

@router.get("/connectors/custom-api-presets", response_model=list[dict])
async def get_custom_api_presets(
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Curated Custom API presets (X Write…) — ready-to-connect REST endpoints
    exposed as tools. The connect form pre-fills base_url / endpoints / OAuth
    defaults; the admin supplies the OAuth client id/secret."""
    from app.schemas.data_source_registry import custom_api_presets
    return custom_api_presets()

@router.get("/data_sources", response_model=list[DataSourceListItemSchema])
async def get_data_sources(
    show_all: bool = Query(False, description="Admin 'show all' view: include every data source in the org (private ones too). Only honored for callers with org-wide data-source governance (full_admin_access / manage_connections); ignored otherwise."),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    result = await data_source_service.get_data_sources(db, current_user, organization, show_all=show_all)
    await release_request_db(db)  # free the pooled connection before serialization (Cause A, Phase 1)
    return result

@router.get("/data_sources/active", response_model=list[DataSourceListItemSchema])
async def get_active_data_sources(
    include_unconnected: bool = Query(False, description="Include user_required data sources the user hasn't connected yet (returned with user_status so the client can offer a Connect action)"),
    show_all: bool = Query(False, description="Admin-only: include every agent in the org (not just the caller's memberships); admin-only entries are flagged with admin_only"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    result = await data_source_service.get_active_data_sources(db, organization, current_user, include_unconnected=include_unconnected, show_all=show_all)
    await release_request_db(db)  # free the pooled connection before serialization (Cause A, Phase 1)
    return result

@router.get("/data_sources/connected_channels", response_model=list[dict])
async def get_connected_channels(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """List the org's channels (Slack/Teams/WhatsApp/email/MCP) annotated with
    whether each is connected. Drives the per-agent channel-availability toggles
    in the new-agent and agent-settings UI."""
    return await data_source_service.get_connected_channels(db, organization)

@router.get("/data_sources/{data_source_id}", response_model=DataSourceSchema)
@requires_resource_permission('data_source', 'view')
async def get_data_source(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.get_data_source(db, data_source_id, organization, current_user)


@router.get("/data_sources/{data_source_type}/fields", response_model=dict)
async def get_data_source_fields(
    data_source_type: str,
    auth_policy: str = None,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    return await data_source_service.get_data_source_fields(db, data_source_type, organization, current_user, auth_policy=auth_policy)

@router.post("/data_sources", response_model=DataSourceSchema)
@requires_permission('create_data_source')
async def create_data_source(
    data_source: DataSourceCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    # Check resource-level permission on connection(s) being linked
    connection_ids = []
    if data_source.connection_ids:
        connection_ids = data_source.connection_ids
    elif data_source.connection_id:
        connection_ids = [data_source.connection_id]
    if connection_ids:
        # Building an agent on an existing connection requires per-connection
        # `create_data_sources` (connection admins & manage_connections pass via
        # implication). ALL-connections semantics: every attached connection
        # must permit it.
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "connection", connection_ids, "create_data_sources",
        )
    return await data_source_service.create_data_source(db, organization, current_user, data_source)

@router.delete("/data_sources/{data_source_id}")
@requires_resource_permission('data_source', 'manage')
async def delete_data_source(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.delete_data_source(db, data_source_id, organization, current_user)

@router.get("/data_sources/{data_source_id}/test_connection", response_model=dict)
@requires_resource_permission('data_source', 'view')
async def test_data_source_connection(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.test_data_source_connection(db, data_source_id, organization, current_user)

@router.post("/data_sources/test_connection", response_model=dict)
@requires_permission('create_data_source')
async def test_new_data_source_connection(
    data_source: DataSourceCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.test_new_data_source_connection(db=db, data=data_source, organization=organization, current_user=current_user)

@router.put("/data_sources/{data_source_id}", response_model=DataSourceSchema)
@requires_resource_permission('data_source', 'manage')
async def update_data_source(
    data_source_id: str,
    data_source: DataSourceUpdate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.update_data_source(db, data_source_id, organization, data_source, current_user)

@router.get("/data_sources/{data_source_id}/schema", response_model=list)
@requires_resource_permission('data_source', 'view')
async def get_data_source_schema(
    data_source_id: str,
    with_stats: bool = Query(False),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.get_data_source_schema(db, data_source_id, include_inactive=False, organization=organization, current_user=current_user, with_stats=with_stats)

@router.get("/data_sources/{data_source_id}/full_schema", response_model=Union[PaginatedTablesResponse, list])
@requires_resource_permission('data_source', 'view_schema')
async def get_data_source_full_schema(
    data_source_id: str,
    with_stats: bool = Query(False),
    # Pagination params (optional - if not provided, returns legacy list response)
    page: Optional[int] = Query(None, ge=1, description="Page number (1-indexed)"),
    page_size: Optional[int] = Query(None, ge=1, le=500, description="Items per page (max 500)"),
    schema_filter: Optional[str] = Query(None, description="Comma-separated schema names to filter"),
    connection_filter: Optional[str] = Query(None, description="Comma-separated connection IDs to filter"),
    search: Optional[str] = Query(None, description="Search tables by name"),
    sort_by: str = Query("name", description="Sort by: name, centrality_score, is_active, richness"),
    sort_dir: str = Query("asc", description="Sort direction: asc or desc"),
    selected_state: Optional[str] = Query(None, description="Filter by selection state: 'selected' or 'unselected'"),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    # If pagination params provided, use paginated response
    if page is not None or page_size is not None:
        # Default pagination values
        page = page or 1
        page_size = page_size or 100

        # Parse schema filter (comma-separated string to list)
        schema_filter_list = None
        if schema_filter:
            schema_filter_list = [s.strip() for s in schema_filter.split(",") if s.strip()]

        # Parse connection filter (comma-separated string to list)
        connection_filter_list = None
        if connection_filter:
            connection_filter_list = [c.strip() for c in connection_filter.split(",") if c.strip()]

        paginated = await data_source_service.get_data_source_schema_paginated(
            db=db,
            data_source_id=data_source_id,
            organization=organization,
            page=page,
            page_size=page_size,
            schema_filter=schema_filter_list,
            connection_filter=connection_filter_list,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
            include_inactive=True,
            selected_state=selected_state,
            with_stats=with_stats,
            current_user=current_user,
        )
        await release_request_db(db)  # free the pooled connection before serialization (Cause A, Phase 1)
        return paginated

    # Legacy behavior: return full list
    legacy = await data_source_service.get_data_source_schema(db, data_source_id, include_inactive=True, organization=organization, current_user=current_user, with_stats=with_stats)
    await release_request_db(db)  # free the pooled connection before serialization (Cause A, Phase 1)
    return legacy

@router.put("/data_sources/{data_source_id}/update_schema", response_model=DataSourceSchema)
@requires_resource_permission('data_source', 'manage')
async def update_table_status_in_schema(
    data_source_id: str,
    tables: list[DataSourceTableSchema],
    http_request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    result = await data_source_service.update_table_status_in_schema(db, data_source_id, tables, organization)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="data_source.tables_updated",
            user_id=current_user.id, resource_type="data_source", resource_id=str(data_source_id),
            details={"table_count": len(tables or [])}, request=http_request,
        )
    except Exception:
        pass
    return result


@router.post("/data_sources/{data_source_id}/bulk_update_tables", response_model=DeltaUpdateTablesResponse)
@requires_resource_permission('data_source', 'manage')
async def bulk_update_tables(
    data_source_id: str,
    request: BulkUpdateTablesRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """
    Bulk activate/deactivate tables matching filter criteria.

    - action: "activate" or "deactivate"
    - filter: {"schema": ["schema1", "schema2"], "search": "pattern"}
    """
    result = await data_source_service.bulk_update_tables_status(
        db=db,
        data_source_id=data_source_id,
        organization=organization,
        action=request.action,
        filter_params=request.filter,
        current_user=current_user,
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="data_source.tables_bulk_updated",
            user_id=current_user.id, resource_type="data_source", resource_id=str(data_source_id),
            details={"action": request.action, "filter": request.filter}, request=http_request,
        )
    except Exception:
        pass
    return result


@router.put("/data_sources/{data_source_id}/update_tables_status", response_model=DeltaUpdateTablesResponse)
@requires_resource_permission('data_source', 'manage')
async def update_tables_status_delta(
    data_source_id: str,
    request: DeltaUpdateTablesRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """
    Update table is_active status using delta (efficient for large table counts).

    - activate: list of table names to set is_active=True
    - deactivate: list of table names to set is_active=False
    """
    result = await data_source_service.update_tables_status_delta(
        db=db,
        data_source_id=data_source_id,
        organization=organization,
        activate=request.activate,
        deactivate=request.deactivate,
        current_user=current_user,
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="data_source.tables_updated",
            user_id=current_user.id, resource_type="data_source", resource_id=str(data_source_id),
            details={"activated": len(request.activate or []),
                     "deactivated": len(request.deactivate or [])}, request=http_request,
        )
    except Exception:
        pass
    return result


@router.get("/data_sources/{data_source_id}/generate_items", response_model=dict)
@requires_resource_permission('data_source', 'manage')
async def generate_data_source_items(
    data_source_id: str,
    item: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.generate_data_source_items(db, item, data_source_id, organization, current_user)

@router.post("/data_sources/{data_source_id}/llm_sync", response_model=dict)
@requires_resource_permission('data_source', 'manage')
async def llm_sync(
    data_source_id: str,
    http_request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    result = await data_source_service.llm_sync(db=db, data_source_id=data_source_id, organization=organization, current_user=current_user)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="data_source.llm_synced",
            user_id=current_user.id, resource_type="data_source", resource_id=str(data_source_id),
            request=http_request,
        )
    except Exception:
        pass
    return result

@router.get("/data_sources/{data_source_id}/refresh_schema", response_model=list)
@requires_resource_permission('data_source', 'view_schema')
async def refresh_data_source_schema(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.refresh_data_source_schema(db, data_source_id, organization, current_user)

@router.get("/data_sources/{data_source_id}/metadata_resources", response_model=MetadataIndexingJobSchema)
@requires_resource_permission('data_source', 'view')
async def get_metadata_resources(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.get_metadata_resources(db, data_source_id, organization, current_user)

@router.put("/data_sources/{data_source_id}/update_metadata_resources", response_model=MetadataIndexingJobSchema)
@requires_resource_permission('data_source', 'manage')
async def update_metadata_resources(
    data_source_id: str,
    http_request: Request,
    resources: list = Body(...),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Update the active status of metadata resources for a data source"""
    result = await data_source_service.update_resources_status(
        db=db,
        data_source_id=data_source_id,
        resources=resources,
        organization=organization,
        current_user=current_user
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="data_source.metadata_resources_updated",
            user_id=current_user.id, resource_type="data_source", resource_id=str(data_source_id),
            details={"resource_count": len(resources or [])}, request=http_request,
        )
    except Exception:
        pass
    return result


@router.get("/data_sources/{data_source_id}/members", response_model=list[DataSourceMembershipSchema])
@requires_resource_permission('data_source', 'view')
async def get_data_source_members(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.get_data_source_members(db, data_source_id, organization, current_user)

@router.post("/data_sources/{data_source_id}/members", response_model=DataSourceMembershipSchema)
@requires_resource_permission('data_source', 'manage')
async def add_data_source_member(
    data_source_id: str,
    member: DataSourceMembershipCreate,
    http_request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    result = await data_source_service.add_data_source_member(db, data_source_id, member, organization, current_user)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="data_source.member_added",
            user_id=current_user.id, resource_type="data_source", resource_id=str(data_source_id),
            details={"user_id": getattr(member, "user_id", None)}, request=http_request,
        )
    except Exception:
        pass
    return result

@router.delete("/data_sources/{data_source_id}/members/{user_id}", status_code=204)
@requires_resource_permission('data_source', 'manage')
async def remove_data_source_member(
    data_source_id: str,
    user_id: str,
    http_request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    result = await data_source_service.remove_data_source_member(db, data_source_id, user_id, organization, current_user)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="data_source.member_removed",
            user_id=current_user.id, resource_type="data_source", resource_id=str(data_source_id),
            details={"user_id": str(user_id)}, request=http_request,
        )
    except Exception:
        pass
    return result


# ==================== Domain-Connection Routes ====================

@router.get("/data_sources/{data_source_id}/connections")
@requires_resource_permission('data_source', 'manage')
async def get_domain_connections(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Get all connections linked to an agent."""
    connections = await data_source_service.get_domain_connections(db, data_source_id, organization)
    # Expose the non-secret config (credentials live encrypted + separate) so
    # the agent UI can show a file connection's scope (path/prefix, globs,
    # indexing) without a second round-trip. This endpoint already requires the
    # 'manage' permission on the data source.
    def _safe_config(conn):
        cfg = conn.config
        if isinstance(cfg, str):
            try:
                import json as _json
                cfg = _json.loads(cfg)
            except Exception:
                cfg = {}
        return cfg if isinstance(cfg, dict) else {}
    return [
        {
            "id": str(conn.id),
            "name": conn.name,
            "type": conn.type,
            "is_active": conn.is_active,
            "config": _safe_config(conn),
        }
        for conn in connections
    ]


@router.get("/data_sources/{data_source_id}/connections/{connection_id}/files")
@requires_resource_permission('data_source', 'view')
async def list_connection_files(
    data_source_id: str,
    connection_id: str,
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    """Browse a file connection's files — the SAME live path the agent's
    list_files tool uses (scoped by globs, bounded by the connection's cap).

    This is the single source of truth for "what files does this connection
    expose", so the UI browse never diverges from what the agent sees. It's
    live for cheap-to-list sources (network_dir/S3) and reflects the real
    source — so `none`-mode connections show their files (not an empty cache).
    """
    from app.models.connection import Connection
    from app.services.connection_service import ConnectionService
    conns = await data_source_service.get_domain_connections(db, data_source_id, organization)
    conn = next((c for c in conns if str(c.id) == str(connection_id)), None)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not attached to this agent")
    try:
        client = await ConnectionService().construct_client(db, conn, current_user)
        entries = await client.alist_files(recursive=True)
    except HTTPException as he:
        # Per-user OAuth sources raise 403 when the caller hasn't linked their
        # account. Surface that as a structured "connect required" state (not a
        # generic failure) so the UI can prompt sign-in instead of showing an
        # error. Any other HTTPException is a real error — re-raise it.
        detail = str(getattr(he, "detail", "") or "")
        if he.status_code == 403 and "connect" in detail.lower():
            return {
                "connection_id": str(connection_id),
                "connect_required": True,
                "reason": detail,
                "files": [], "total": 0, "offset": offset, "limit": limit, "has_more": False,
            }
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to list files: {e}")
    files = [e for e in (entries or []) if not e.get("is_folder")]
    total = len(files)
    page = files[max(0, offset): max(0, offset) + max(1, min(limit, 500))]
    return {
        "connection_id": str(connection_id),
        "files": [{"id": f.get("id"), "name": f.get("name"), "size": f.get("size"),
                   "modified_at": f.get("modified_at"), "mime_type": f.get("mime_type")} for f in page],
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < total,
    }


@router.post("/data_sources/{data_source_id}/connections/{connection_id}")
@requires_resource_permission('data_source', 'manage')
async def add_connection_to_domain(
    data_source_id: str,
    connection_id: str,
    http_request: Request,
    sync_tables: bool = True,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Add a connection to an agent (M:N relationship).

    Two independent capabilities are required, matching the "build an agent on a
    connection" model:
      - The `data_source:manage` decorator proves the caller owns/manages this
        agent.
      - Per-connection `create_data_sources` proves they may build agents on the
        connection being attached — the SAME check `create_data_source` runs when
        an agent is created directly on a connection. Connection admins /
        `manage_connections` pass via implication.
    """
    await check_resource_permissions(
        db, str(current_user.id), str(organization.id),
        "connection", [connection_id], "create_data_sources",
    )

    result = await data_source_service.add_connection_to_domain(
        db=db,
        data_source_id=data_source_id,
        connection_id=connection_id,
        organization=organization,
        current_user=current_user,
        sync_tables=sync_tables,
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="data_source.connection_linked",
            user_id=current_user.id, resource_type="data_source", resource_id=str(data_source_id),
            details={"connection_id": str(connection_id)}, request=http_request,
        )
    except Exception:
        pass
    return result


@router.delete("/data_sources/{data_source_id}/connections/{connection_id}")
@requires_resource_permission('data_source', 'manage')
async def remove_connection_from_domain(
    data_source_id: str,
    connection_id: str,
    http_request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Remove a connection from an agent."""
    result = await data_source_service.remove_connection_from_domain(
        db=db,
        data_source_id=data_source_id,
        connection_id=connection_id,
        organization=organization,
        current_user=current_user,
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="data_source.connection_unlinked",
            user_id=current_user.id, resource_type="data_source", resource_id=str(data_source_id),
            details={"connection_id": str(connection_id)}, request=http_request,
        )
    except Exception:
        pass
    return result