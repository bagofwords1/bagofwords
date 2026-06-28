"""Per-agent tool overlay endpoints.

The page at ``/agents/[id]/tools`` reads/writes overlay rows from this
router. The runtime tool loader (and the YAML apply path) read the same
overlay table — ``data_source_connection_tool`` — and fall back to the
``ConnectionTool`` defaults when no override exists.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import current_user
from app.core.permissions_decorator import requires_resource_permission
from app.ee.audit.service import audit_service
from app.dependencies import get_async_db, get_current_organization
from app.models.connection import Connection
from app.models.connection_tool import ConnectionTool
from app.models.data_source import DataSource
from app.models.data_source_connection_tool import DataSourceConnectionTool
from app.models.organization import Organization
from app.models.user import User


router = APIRouter(tags=["data_source_tools"])


class AgentToolSchema(BaseModel):
    id: str  # connection_tool_id
    connection_id: str
    connection_name: str
    name: str
    description: Optional[str] = None
    # Effective state (overlay if present, else ConnectionTool default)
    is_enabled: bool
    policy: str
    # True when a per-agent overlay row exists
    has_overlay: bool
    # The connection-level defaults (so the UI can show "(default)" badges)
    default_is_enabled: bool
    default_policy: str

    class Config:
        from_attributes = True


class AgentToolUpdate(BaseModel):
    is_enabled: Optional[bool] = None
    policy: Optional[str] = None  # allow | confirm | deny


async def _load_agent(db: AsyncSession, data_source_id: str, organization: Organization) -> DataSource:
    q = await db.execute(
        select(DataSource)
        .options(selectinload(DataSource.connections))
        .where(
            DataSource.id == data_source_id,
            DataSource.organization_id == organization.id,
        )
    )
    ds = q.scalar_one_or_none()
    if ds is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return ds


@router.get("/data_sources/{data_source_id}/tools", response_model=List[AgentToolSchema])
@requires_resource_permission('data_source', 'view')
async def list_agent_tools(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
) -> List[AgentToolSchema]:
    ds = await _load_agent(db, data_source_id, organization)
    conn_ids = [str(c.id) for c in (ds.connections or [])]
    if not conn_ids:
        return []
    conn_by_id = {str(c.id): c for c in ds.connections}

    tools_q = await db.execute(
        select(ConnectionTool).where(ConnectionTool.connection_id.in_(conn_ids))
    )
    tools = list(tools_q.scalars().all())

    overlay_q = await db.execute(
        select(DataSourceConnectionTool).where(
            DataSourceConnectionTool.data_source_id == data_source_id
        )
    )
    overlay = {str(o.connection_tool_id): o for o in overlay_q.scalars().all()}

    out: List[AgentToolSchema] = []
    for t in tools:
        ov = overlay.get(str(t.id))
        if ov is not None:
            eff_enabled = ov.is_enabled
            eff_policy = ov.policy
            has_overlay = True
        else:
            eff_enabled = t.is_enabled
            eff_policy = t.policy
            has_overlay = False
        conn = conn_by_id.get(str(t.connection_id))
        out.append(
            AgentToolSchema(
                id=str(t.id),
                connection_id=str(t.connection_id),
                connection_name=conn.name if conn else "",
                name=t.name,
                description=t.description,
                is_enabled=eff_enabled,
                policy=eff_policy,
                has_overlay=has_overlay,
                default_is_enabled=t.is_enabled,
                default_policy=t.policy,
            )
        )
    return out


@router.put("/data_sources/{data_source_id}/tools/{tool_id}", response_model=AgentToolSchema)
@requires_resource_permission('data_source', 'manage')
async def update_agent_tool(
    data_source_id: str,
    tool_id: str,
    data: AgentToolUpdate,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
) -> AgentToolSchema:
    ds = await _load_agent(db, data_source_id, organization)
    conn_ids = [str(c.id) for c in (ds.connections or [])]

    # Validate the tool belongs to one of the agent's connections
    t_q = await db.execute(
        select(ConnectionTool).where(
            ConnectionTool.id == tool_id,
            ConnectionTool.connection_id.in_(conn_ids),
        )
    )
    tool = t_q.scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not linked to this agent")

    if data.policy is not None and data.policy not in {"allow", "confirm", "deny"}:
        raise HTTPException(status_code=400, detail="policy must be allow, confirm, or deny")

    # Upsert overlay
    ov_q = await db.execute(
        select(DataSourceConnectionTool).where(
            DataSourceConnectionTool.data_source_id == data_source_id,
            DataSourceConnectionTool.connection_tool_id == tool_id,
        )
    )
    overlay = ov_q.scalar_one_or_none()
    if overlay is None:
        overlay = DataSourceConnectionTool(
            data_source_id=data_source_id,
            connection_tool_id=tool_id,
            is_enabled=tool.is_enabled if data.is_enabled is None else data.is_enabled,
            policy=tool.policy if data.policy is None else data.policy,
        )
        db.add(overlay)
    else:
        if data.is_enabled is not None:
            overlay.is_enabled = data.is_enabled
        if data.policy is not None:
            overlay.policy = data.policy
        db.add(overlay)
    await db.commit()
    await db.refresh(overlay)

    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="data_source.tool_overlay_updated",
            user_id=current_user.id, resource_type="data_source", resource_id=str(data_source_id),
            details={"tool_id": str(tool_id), "is_enabled": overlay.is_enabled, "policy": overlay.policy},
            request=request,
        )
    except Exception:
        pass

    conn = next((c for c in ds.connections if str(c.id) == str(tool.connection_id)), None)
    return AgentToolSchema(
        id=str(tool.id),
        connection_id=str(tool.connection_id),
        connection_name=conn.name if conn else "",
        name=tool.name,
        description=tool.description,
        is_enabled=overlay.is_enabled,
        policy=overlay.policy,
        has_overlay=True,
        default_is_enabled=tool.is_enabled,
        default_policy=tool.policy,
    )


@router.delete("/data_sources/{data_source_id}/tools/{tool_id}", response_model=AgentToolSchema)
@requires_resource_permission('data_source', 'manage')
async def reset_agent_tool(
    data_source_id: str,
    tool_id: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
) -> AgentToolSchema:
    """Remove the per-agent overlay; the tool reverts to connection defaults."""
    ds = await _load_agent(db, data_source_id, organization)
    conn_ids = [str(c.id) for c in (ds.connections or [])]

    t_q = await db.execute(
        select(ConnectionTool).where(
            ConnectionTool.id == tool_id,
            ConnectionTool.connection_id.in_(conn_ids),
        )
    )
    tool = t_q.scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not linked to this agent")

    ov_q = await db.execute(
        select(DataSourceConnectionTool).where(
            DataSourceConnectionTool.data_source_id == data_source_id,
            DataSourceConnectionTool.connection_tool_id == tool_id,
        )
    )
    overlay = ov_q.scalar_one_or_none()
    if overlay is not None:
        await db.delete(overlay)
        await db.commit()

    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="data_source.tool_overlay_reset",
            user_id=current_user.id, resource_type="data_source", resource_id=str(data_source_id),
            details={"tool_id": str(tool_id)}, request=request,
        )
    except Exception:
        pass

    conn = next((c for c in ds.connections if str(c.id) == str(tool.connection_id)), None)
    return AgentToolSchema(
        id=str(tool.id),
        connection_id=str(tool.connection_id),
        connection_name=conn.name if conn else "",
        name=tool.name,
        description=tool.description,
        is_enabled=tool.is_enabled,
        policy=tool.policy,
        has_overlay=False,
        default_is_enabled=tool.is_enabled,
        default_policy=tool.policy,
    )
