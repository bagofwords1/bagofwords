"""Integrations routes — the user-facing tool catalog.

Distinct from `/connections` (admin data-source CRUD): this surfaces connectors
whose `connect_audience` makes them self-serve (Gmail, Drive, Jira, Notion, MCP…),
plus the org's already-created integration connections annotated with the **current
user's** connected state. Powers the Integrations page and its connect flow.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.dependencies import get_async_db, get_current_organization
from app.models.connection import Connection
from app.models.connection_tool import ConnectionTool
from app.models.organization import Organization
from app.models.user import User
from app.models.user_connection_credentials import UserConnectionCredentials
from app.schemas.data_source_registry import get_entry, list_integration_entries

router = APIRouter(prefix="/integrations", tags=["integrations"])


class IntegrationCatalogItem(BaseModel):
    type: str
    title: str
    description: str
    ui_form: str
    connect_audience: str
    requires_license: Optional[str] = None


class IntegrationToolItem(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    is_enabled: bool          # admin default
    is_accessible: bool       # current user's overlay (defaults to admin default)


class IntegrationItem(BaseModel):
    connection_id: str
    name: str
    type: str
    title: str
    description: str
    auth_policy: str
    connected: bool           # current user usable (system cred or own user cred)
    needs_user_auth: bool     # user_required and the user hasn't connected yet
    tool_count: int
    tools: List[IntegrationToolItem] = []


@router.get("/catalog", response_model=List[IntegrationCatalogItem])
async def get_integration_catalog(
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """All self-serve connectors the Integrations page can offer."""
    return list_integration_entries()


@router.get("", response_model=List[IntegrationItem])
async def list_integrations(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """The org's integration connections, annotated with the current user's
    connected state + per-tool accessibility overlay."""
    conns = (await db.execute(
        select(Connection).where(
            Connection.organization_id == str(organization.id),
            Connection.is_active == True,  # noqa: E712
        )
    )).scalars().all()

    cred_conn_ids = set((await db.execute(
        select(UserConnectionCredentials.connection_id).where(
            UserConnectionCredentials.user_id == str(current_user.id),
            UserConnectionCredentials.is_active == True,  # noqa: E712
        )
    )).scalars().all())

    # Per-user tool overlays (tool_name -> is_accessible) for this user.
    from app.models.user_connection_tool import UserConnectionTool
    overlay_rows = (await db.execute(
        select(UserConnectionTool).where(
            UserConnectionTool.user_id == str(current_user.id),
        )
    )).scalars().all()
    overlay = {(o.connection_id, o.tool_name): o.is_accessible for o in overlay_rows}

    items: List[IntegrationItem] = []
    for conn in conns:
        try:
            entry = get_entry(conn.type)
        except Exception:
            continue
        if not entry.is_integration:
            continue

        connected = conn.auth_policy == "system_only" or str(conn.id) in cred_conn_ids
        needs_user_auth = conn.auth_policy == "user_required" and str(conn.id) not in cred_conn_ids

        tool_rows = (await db.execute(
            select(ConnectionTool).where(
                ConnectionTool.connection_id == conn.id,
                ConnectionTool.deleted_at.is_(None),
            )
        )).scalars().all()
        tools = [
            IntegrationToolItem(
                id=str(t.id),
                name=t.name,
                description=t.description,
                is_enabled=bool(t.is_enabled),
                is_accessible=overlay.get((str(conn.id), t.name), bool(t.is_enabled)),
            )
            for t in tool_rows
        ]
        items.append(IntegrationItem(
            connection_id=str(conn.id),
            name=conn.name,
            type=conn.type,
            title=entry.title,
            description=entry.description,
            auth_policy=conn.auth_policy,
            connected=connected,
            needs_user_auth=needs_user_auth,
            tool_count=len([t for t in tools if t.is_enabled]),
            tools=tools,
        ))
    return items


class ToolToggle(BaseModel):
    tool_name: str
    is_accessible: bool


@router.put("/{connection_id}/tools/{tool_name}")
async def toggle_user_tool(
    connection_id: str,
    tool_name: str,
    body: ToolToggle,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Per-user enable/disable of a single integration tool (UserConnectionTool)."""
    from app.models.user_connection_tool import UserConnectionTool

    ct = (await db.execute(
        select(ConnectionTool).where(
            ConnectionTool.connection_id == connection_id,
            ConnectionTool.name == tool_name,
        )
    )).scalar_one_or_none()

    row = (await db.execute(
        select(UserConnectionTool).where(
            UserConnectionTool.connection_id == connection_id,
            UserConnectionTool.user_id == str(current_user.id),
            UserConnectionTool.tool_name == tool_name,
        )
    )).scalar_one_or_none()
    if row is None:
        row = UserConnectionTool(
            connection_id=connection_id,
            user_id=str(current_user.id),
            tool_name=tool_name,
            connection_tool_id=str(ct.id) if ct else None,
        )
        db.add(row)
    row.is_accessible = body.is_accessible
    row.status = "accessible" if body.is_accessible else "inaccessible"
    await db.commit()
    return {"connection_id": connection_id, "tool_name": tool_name, "is_accessible": body.is_accessible}
