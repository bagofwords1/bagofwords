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
from app.services.tool_policy_service import (
    ToolPolicyService,
    VALID_TOOL_POLICIES,
    normalize_tool_policy,
    resolve_effective_policy,
)


router = APIRouter(tags=["data_source_tools"])


class AgentToolSchema(BaseModel):
    id: str  # connection_tool_id
    connection_id: str
    connection_name: str
    # Connection type (mcp | custom_api) so the UI can render transport-
    # specific details (HTTP method/path chips for custom_api tools).
    connection_type: Optional[str] = None
    name: str
    description: Optional[str] = None
    # The tool's declared parameter schema — drives the expanded
    # "Parameters" view on the tools page.
    input_schema: Optional[dict] = None
    # Provider extras (custom_api: {"method": "GET", "path": "/orders/{id}"}).
    metadata: Optional[dict] = None
    # Admin state (overlay if present, else ConnectionTool default)
    is_enabled: bool
    policy: str
    # True when a per-agent overlay row exists
    has_overlay: bool
    # The connection-level defaults (so the UI can show "(default)" badges)
    default_is_enabled: bool
    default_policy: str
    # The requesting user's own preference (null = inherit admin policy) and
    # the policy that would actually apply to THEIR runs.
    user_policy: Optional[str] = None
    effective_policy: str = "allow"

    class Config:
        from_attributes = True


def _tool_row(
    tool: ConnectionTool,
    conn: Optional[Connection],
    *,
    is_enabled: bool,
    policy: str,
    has_overlay: bool,
    user_policy: Optional[str],
) -> AgentToolSchema:
    """One place that shapes a ConnectionTool (+ overlay state) into the API row."""
    return AgentToolSchema(
        id=str(tool.id),
        connection_id=str(tool.connection_id),
        connection_name=conn.name if conn else "",
        connection_type=conn.type if conn else None,
        name=tool.name,
        description=tool.description,
        input_schema=tool.input_schema,
        metadata=tool.metadata_json,
        is_enabled=is_enabled,
        policy=normalize_tool_policy(policy),
        has_overlay=has_overlay,
        default_is_enabled=tool.is_enabled,
        default_policy=normalize_tool_policy(tool.policy),
        user_policy=user_policy,
        effective_policy=resolve_effective_policy(policy, user_policy),
    )


class AgentToolUpdate(BaseModel):
    is_enabled: Optional[bool] = None
    policy: Optional[str] = None  # allow | ask | deny | auto


class MyToolPolicyUpdate(BaseModel):
    policy: str  # allow | ask | deny | auto


# Bulk variants of the three single-tool writes. A connection can expose dozens
# of tools, and setting each one individually is both tedious and N round-trips.
MAX_BULK_TOOLS = 500


class AgentToolBatchUpdate(BaseModel):
    tool_ids: List[str]
    is_enabled: Optional[bool] = None
    policy: Optional[str] = None  # allow | ask | deny | auto


class AgentToolBatchReset(BaseModel):
    tool_ids: List[str]


class MyToolPolicyBatchUpdate(BaseModel):
    tool_ids: List[str]
    # None / "" clears the preference (revert to the admin policy).
    policy: Optional[str] = None


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

    user_prefs = await ToolPolicyService().get_user_preferences(
        db, str(current_user.id), [str(t.id) for t in tools]
    )

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
        user_policy = user_prefs.get(str(t.id))
        out.append(
            _tool_row(
                t, conn,
                is_enabled=eff_enabled,
                policy=eff_policy,
                has_overlay=has_overlay,
                user_policy=user_policy,
            )
        )
    return out


async def _load_agent_tools(
    db: AsyncSession, ds: DataSource, tool_ids: List[str]
) -> List[ConnectionTool]:
    """Resolve the requested tool ids, rejecting any that aren't this agent's.

    All-or-nothing: a stale id in the selection would otherwise be applied as a
    partial success, leaving the UI showing state the user didn't ask for.
    """
    wanted = list(dict.fromkeys(str(t) for t in tool_ids))  # de-dupe, keep order
    if not wanted:
        raise HTTPException(status_code=400, detail="tool_ids must not be empty")
    if len(wanted) > MAX_BULK_TOOLS:
        raise HTTPException(
            status_code=400, detail=f"at most {MAX_BULK_TOOLS} tools per request"
        )
    conn_ids = [str(c.id) for c in (ds.connections or [])]
    if not conn_ids:
        raise HTTPException(status_code=404, detail="Tool not linked to this agent")
    q = await db.execute(
        select(ConnectionTool).where(
            ConnectionTool.id.in_(wanted),
            ConnectionTool.connection_id.in_(conn_ids),
        )
    )
    found = {str(t.id): t for t in q.scalars().all()}
    missing = [i for i in wanted if i not in found]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"{len(missing)} tool(s) not linked to this agent: {missing[:5]}",
        )
    return [found[i] for i in wanted]


async def _agent_tools_response(
    db: AsyncSession, ds: DataSource, tools: List[ConnectionTool], current_user: User
) -> List[AgentToolSchema]:
    """Build the rows for several tools at once (two queries, not two per tool)."""
    tool_ids = [str(t.id) for t in tools]
    ov_q = await db.execute(
        select(DataSourceConnectionTool).where(
            DataSourceConnectionTool.data_source_id == str(ds.id),
            DataSourceConnectionTool.connection_tool_id.in_(tool_ids),
        )
    )
    overlays = {str(o.connection_tool_id): o for o in ov_q.scalars().all()}
    user_prefs = await ToolPolicyService().get_user_preferences(
        db, str(current_user.id), tool_ids
    )
    conn_by_id = {str(c.id): c for c in (ds.connections or [])}

    out: List[AgentToolSchema] = []
    for tool in tools:
        overlay = overlays.get(str(tool.id))
        admin_enabled = overlay.is_enabled if overlay else tool.is_enabled
        admin_policy = overlay.policy if overlay else tool.policy
        user_policy = user_prefs.get(str(tool.id))
        conn = conn_by_id.get(str(tool.connection_id))
        out.append(
            _tool_row(
                tool, conn,
                is_enabled=admin_enabled,
                policy=admin_policy,
                has_overlay=overlay is not None,
                user_policy=user_policy,
            )
        )
    return out


# NOTE: the /batch routes must be declared before /{tool_id}, or "batch" is
# captured as a tool id and every bulk call 404s.
@router.put("/data_sources/{data_source_id}/tools/batch", response_model=List[AgentToolSchema])
@requires_resource_permission('data_source', 'manage')
async def update_agent_tools_batch(
    data_source_id: str,
    data: AgentToolBatchUpdate,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
) -> List[AgentToolSchema]:
    """Enable/disable and/or set the admin policy on many tools at once."""
    if data.is_enabled is None and data.policy is None:
        raise HTTPException(
            status_code=400, detail="provide is_enabled and/or policy"
        )
    policy = None
    if data.policy is not None:
        policy = normalize_tool_policy(data.policy, default=None)
        if policy is None:
            raise HTTPException(
                status_code=400,
                detail=f"policy must be one of {sorted(VALID_TOOL_POLICIES)}",
            )

    ds = await _load_agent(db, data_source_id, organization)
    tools = await _load_agent_tools(db, ds, data.tool_ids)

    ov_q = await db.execute(
        select(DataSourceConnectionTool).where(
            DataSourceConnectionTool.data_source_id == data_source_id,
            DataSourceConnectionTool.connection_tool_id.in_([str(t.id) for t in tools]),
        )
    )
    overlays = {str(o.connection_tool_id): o for o in ov_q.scalars().all()}

    for tool in tools:
        overlay = overlays.get(str(tool.id))
        if overlay is None:
            # Seed the untouched side from the connection default so the new
            # overlay doesn't silently change it.
            db.add(
                DataSourceConnectionTool(
                    data_source_id=data_source_id,
                    connection_tool_id=str(tool.id),
                    is_enabled=tool.is_enabled if data.is_enabled is None else data.is_enabled,
                    policy=tool.policy if policy is None else policy,
                )
            )
        else:
            if data.is_enabled is not None:
                overlay.is_enabled = data.is_enabled
            if policy is not None:
                overlay.policy = policy
            db.add(overlay)
    await db.commit()

    try:
        await audit_service.log(
            db=db, organization_id=organization.id,
            action="data_source.tool_overlay_bulk_updated",
            user_id=current_user.id, resource_type="data_source",
            resource_id=str(data_source_id),
            details={
                "tool_ids": [str(t.id) for t in tools],
                "count": len(tools),
                "is_enabled": data.is_enabled,
                "policy": policy,
            },
            request=request,
        )
    except Exception:
        pass

    return await _agent_tools_response(db, ds, tools, current_user)


@router.post("/data_sources/{data_source_id}/tools/batch/reset", response_model=List[AgentToolSchema])
@requires_resource_permission('data_source', 'manage')
async def reset_agent_tools_batch(
    data_source_id: str,
    data: AgentToolBatchReset,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
) -> List[AgentToolSchema]:
    """Drop the per-agent overlay on many tools; they revert to connection defaults."""
    ds = await _load_agent(db, data_source_id, organization)
    tools = await _load_agent_tools(db, ds, data.tool_ids)

    ov_q = await db.execute(
        select(DataSourceConnectionTool).where(
            DataSourceConnectionTool.data_source_id == data_source_id,
            DataSourceConnectionTool.connection_tool_id.in_([str(t.id) for t in tools]),
        )
    )
    overlays = list(ov_q.scalars().all())
    for overlay in overlays:
        await db.delete(overlay)
    if overlays:
        await db.commit()

    try:
        await audit_service.log(
            db=db, organization_id=organization.id,
            action="data_source.tool_overlay_bulk_reset",
            user_id=current_user.id, resource_type="data_source",
            resource_id=str(data_source_id),
            details={"tool_ids": [str(t.id) for t in tools], "count": len(overlays)},
            request=request,
        )
    except Exception:
        pass

    return await _agent_tools_response(db, ds, tools, current_user)


@router.put("/data_sources/{data_source_id}/tools/batch/my_policy", response_model=List[AgentToolSchema])
@requires_resource_permission('data_source', 'view')
async def set_my_tool_policies_batch(
    data_source_id: str,
    data: MyToolPolicyBatchUpdate,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
) -> List[AgentToolSchema]:
    """Set (or clear, with an empty policy) the caller's own policy on many tools.

    Any member who can view the agent may do this for themselves; it only
    affects their own runs and can never relax an admin 'deny'.
    """
    normalized = None
    if data.policy:
        normalized = normalize_tool_policy(data.policy, default=None)
        if normalized is None:
            raise HTTPException(
                status_code=400,
                detail=f"policy must be one of {sorted(VALID_TOOL_POLICIES)}",
            )

    ds = await _load_agent(db, data_source_id, organization)
    tools = await _load_agent_tools(db, ds, data.tool_ids)

    policy_svc = ToolPolicyService()
    changed = 0
    for tool in tools:
        if normalized is None:
            if await policy_svc.clear_user_preference(db, str(current_user.id), str(tool.id)):
                changed += 1
        else:
            await policy_svc.set_user_preference(
                db, str(current_user.id), str(tool.id), normalized
            )
            changed += 1
    if changed:
        await db.commit()

    try:
        await audit_service.log(
            db=db, organization_id=organization.id,
            action="data_source.tool_user_policy_bulk_updated",
            user_id=current_user.id, resource_type="data_source",
            resource_id=str(data_source_id),
            details={
                "tool_ids": [str(t.id) for t in tools],
                "count": len(tools),
                "policy": normalized,
            },
            request=request,
        )
    except Exception:
        pass

    return await _agent_tools_response(db, ds, tools, current_user)


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

    if data.policy is not None:
        normalized = normalize_tool_policy(data.policy, default=None)
        if normalized is None:
            raise HTTPException(
                status_code=400,
                detail=f"policy must be one of {sorted(VALID_TOOL_POLICIES)}",
            )
        data.policy = normalized

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
    user_prefs = await ToolPolicyService().get_user_preferences(db, str(current_user.id), [str(tool.id)])
    user_policy = user_prefs.get(str(tool.id))
    return _tool_row(
        tool, conn,
        is_enabled=overlay.is_enabled,
        policy=overlay.policy,
        has_overlay=True,
        user_policy=user_policy,
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
    user_prefs = await ToolPolicyService().get_user_preferences(db, str(current_user.id), [str(tool.id)])
    user_policy = user_prefs.get(str(tool.id))
    return _tool_row(
        tool, conn,
        is_enabled=tool.is_enabled,
        policy=tool.policy,
        has_overlay=False,
        user_policy=user_policy,
    )


async def _load_agent_tool(
    db: AsyncSession, ds: DataSource, tool_id: str
) -> ConnectionTool:
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
    return tool


async def _my_policy_response(
    db: AsyncSession, ds: DataSource, tool: ConnectionTool, current_user: User
) -> AgentToolSchema:
    """Build the AgentToolSchema for a tool after a my-policy change."""
    rows = await _agent_tools_response(db, ds, [tool], current_user)
    return rows[0]


@router.put("/data_sources/{data_source_id}/tools/{tool_id}/my_policy", response_model=AgentToolSchema)
@requires_resource_permission('data_source', 'view')
async def set_my_tool_policy(
    data_source_id: str,
    tool_id: str,
    data: MyToolPolicyUpdate,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
) -> AgentToolSchema:
    """Set the requesting user's own policy for a tool (allow | ask | deny | auto).

    Any member who can view the agent may set their personal preference; it
    only affects their own runs and can never relax an admin 'deny'.
    """
    ds = await _load_agent(db, data_source_id, organization)
    tool = await _load_agent_tool(db, ds, tool_id)

    normalized = normalize_tool_policy(data.policy, default=None)
    if normalized is None:
        raise HTTPException(
            status_code=400,
            detail=f"policy must be one of {sorted(VALID_TOOL_POLICIES)}",
        )

    await ToolPolicyService().set_user_preference(db, str(current_user.id), str(tool.id), normalized)
    await db.commit()

    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="data_source.tool_user_policy_updated",
            user_id=current_user.id, resource_type="data_source", resource_id=str(data_source_id),
            details={"tool_id": str(tool_id), "policy": normalized},
            request=request,
        )
    except Exception:
        pass

    return await _my_policy_response(db, ds, tool, current_user)


@router.delete("/data_sources/{data_source_id}/tools/{tool_id}/my_policy", response_model=AgentToolSchema)
@requires_resource_permission('data_source', 'view')
async def reset_my_tool_policy(
    data_source_id: str,
    tool_id: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
) -> AgentToolSchema:
    """Remove the requesting user's own policy; their runs revert to the admin policy."""
    ds = await _load_agent(db, data_source_id, organization)
    tool = await _load_agent_tool(db, ds, tool_id)

    removed = await ToolPolicyService().clear_user_preference(db, str(current_user.id), str(tool.id))
    if removed:
        await db.commit()

    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="data_source.tool_user_policy_reset",
            user_id=current_user.id, resource_type="data_source", resource_id=str(data_source_id),
            details={"tool_id": str(tool_id)},
            request=request,
        )
    except Exception:
        pass

    return await _my_policy_response(db, ds, tool, current_user)
